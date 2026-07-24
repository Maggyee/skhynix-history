"""Stateful paper-only execution simulator driven by executable BBOs."""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .gate_regime_15m import regime_for_time
from .live_bbo import BBO, EXCHANGES


def _utc(value=None) -> datetime:
    if value is None: return datetime.now(timezone.utc)
    value = pd.Timestamp(value)
    return (value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")).to_pydatetime()


@dataclass
class Position:
    position_id: str
    opened_at: str
    regime: str
    threshold_bps: float
    long_exchange: str
    short_exchange: str
    quantity: float
    gross_notional_usd: float
    long_entry_ask: float
    short_entry_bid: float
    entry_spread_bps: float
    long_entry_sequence: int
    short_entry_sequence: int


@dataclass
class Trade:
    position_id: str
    opened_at: str
    closed_at: str
    regime: str
    threshold_bps: float
    long_exchange: str
    short_exchange: str
    quantity: float
    gross_notional_usd: float
    long_entry_ask: float
    short_entry_bid: float
    long_exit_bid: float
    short_exit_ask: float
    entry_spread_bps: float
    exit_spread_bps: float
    gross_pnl_usd: float
    fees_usd: float
    net_pnl_usd: float
    holding_seconds: float
    close_reason: str


class PaperEngine:
    def __init__(self, paper: dict, live: dict, root: Path):
        self.paper, self.live, self.root = paper, live, Path(root)
        self.quotes: dict[str, BBO] = {}
        self.connected = {x: False for x in EXCHANGES}
        self.connection_ids: dict[str, str] = {}
        self.positions: list[Position] = []
        self.trades: list[Trade] = []
        self.blocked_counts: dict[str, int] = {}
        self.ledger_path = self.root / "paper" / "ledger.json"
        self._load()

    def _load(self):
        if not self.ledger_path.exists(): return
        data = json.loads(self.ledger_path.read_text())
        self.positions = [Position(**x) for x in data.get("positions", [])]
        self.trades = [Trade(**x) for x in data.get("trades", [])]
        self.blocked_counts = data.get("blocked_counts", {})

    def save(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"paper_only":True, "updated_at":_utc().isoformat(),
            "positions":[asdict(x) for x in self.positions],
            "trades":[asdict(x) for x in self.trades], "blocked_counts":self.blocked_counts}
        tmp = self.ledger_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2)); os.replace(tmp, self.ledger_path)

    async def on_status(self, exchange: str, connected: bool, connection_id: str):
        if connected:
            self.connected[exchange] = True; self.connection_ids[exchange] = connection_id
        elif self.connection_ids.get(exchange) == connection_id:
            self.connected[exchange] = False
            self.quotes.pop(exchange, None)  # reconnect must deliver a fresh BBO

    async def on_quote(self, quote: BBO):
        if not self.connected.get(quote.exchange) or self.connection_ids.get(quote.exchange) != quote.connection_id:
            return
        old = self.quotes.get(quote.exchange)
        if old and old.connection_id == quote.connection_id and quote.sequence <= old.sequence:
            self._block("non_monotonic_sequence"); return
        self.quotes[quote.exchange] = quote
        self.evaluate(quote.receive_ts)

    def _block(self, reason: str):
        self.blocked_counts[reason] = self.blocked_counts.get(reason, 0) + 1

    def pair(self, long_exchange: str, short_exchange: str, now=None):
        now = _utc(now); reasons = []
        if long_exchange == short_exchange: reasons.append("same_venue")
        for venue in (long_exchange, short_exchange):
            if not self.connected.get(venue): reasons.append(f"disconnected:{venue}")
            if venue not in self.quotes: reasons.append(f"missing_leg:{venue}")
        if reasons: return None, reasons
        long, short = self.quotes[long_exchange], self.quotes[short_exchange]
        stale = float(self.live.get("stale_after_ms", 3000))
        for quote in (long, short):
            receive_age = (now - quote.receive_ts).total_seconds() * 1000
            exchange_age = (now - quote.exchange_ts).total_seconds() * 1000
            if receive_age > stale: reasons.append(f"stale_receive:{quote.exchange}")
            if exchange_age > stale: reasons.append(f"stale_exchange:{quote.exchange}")
            if receive_age < -1000 or exchange_age < -1000: reasons.append(f"future_timestamp:{quote.exchange}")
        skew = abs((long.exchange_ts - short.exchange_ts).total_seconds() * 1000)
        if skew > float(self.live.get("max_cross_exchange_skew_ms", 1500)):
            reasons.append("cross_exchange_time_skew")
        return (long, short), reasons

    def evaluate(self, now=None):
        now = _utc(now)
        if self.positions:
            self._try_close(self.positions[0], now)
            return
        if int(self.paper.get("max_open_positions", 1)) < 1: return
        # Entry is fail-closed on the complete five-venue collector. Existing
        # positions only need their two executable exit legs and remain open
        # when either is unavailable.
        for venue in EXCHANGES:
            if not self.connected.get(venue): self._block(f"disconnected:{venue}"); return
            quote = self.quotes.get(venue)
            if quote is None: self._block(f"missing_leg:{venue}"); return
            receive_age = (now-quote.receive_ts).total_seconds()*1000
            exchange_age = (now-quote.exchange_ts).total_seconds()*1000
            stale = float(self.live.get("stale_after_ms",3000))
            if receive_age > stale or exchange_age > stale:
                self._block(f"stale_global:{venue}"); return
            if receive_age < -1000 or exchange_age < -1000:
                self._block(f"future_timestamp:{venue}"); return
        regime = regime_for_time(now)
        if regime not in set(self.paper.get("allowed_regimes", [])):
            self._block("regime_filtered"); return
        thresholds = sorted(float(x) for x in self.paper.get("entry_thresholds_bps", (100,150,200)))
        if thresholds != [100.0, 150.0, 200.0]:
            self._block("unfrozen_threshold_config"); return
        candidates = []
        for long_ex in EXCHANGES:
            for short_ex in EXCHANGES:
                pair, reasons = self.pair(long_ex, short_ex, now)
                if reasons:
                    for reason in set(reasons): self._block(reason)
                    continue
                long, short = pair
                spread = (short.bid - long.ask) / long.ask * 10_000
                crossed = [x for x in thresholds if spread >= x]
                if crossed: candidates.append((spread, max(crossed), long, short))
        if not candidates: return
        spread, threshold, long, short = max(candidates, key=lambda x:x[0])
        gross_cap = float(self.paper.get("gross_notional_usd", 1000))
        if gross_cap > 1000 or gross_cap <= 0:
            self._block("phase_one_notional_guard"); return
        quantity = gross_cap / (long.ask + short.bid)
        if long.ask_size < quantity or short.bid_size < quantity:
            self._block("insufficient_bbo_size"); return
        self.positions.append(Position(uuid.uuid4().hex, now.isoformat(), regime, threshold,
            long.exchange, short.exchange, quantity, quantity*(long.ask+short.bid),
            long.ask, short.bid, spread, long.sequence, short.sequence))
        self.save()

    def _try_close(self, position: Position, now: datetime):
        pair, reasons = self.pair(position.long_exchange, position.short_exchange, now)
        if reasons:
            for reason in set(reasons): self._block(reason)
            return
        long, short = pair
        if long.bid_size < position.quantity or short.ask_size < position.quantity:
            self._block("insufficient_exit_bbo_size"); return
        exit_spread = (short.ask - long.bid) / long.bid * 10_000
        held = (now - _utc(position.opened_at)).total_seconds()
        reason = None
        if exit_spread <= float(self.paper.get("exit_spread_bps", 20)): reason = "CONVERGENCE"
        elif held >= float(self.paper.get("max_holding_seconds", 86400)): reason = "MAX_HOLD"
        if reason is None: return
        gross = position.quantity * ((long.bid-position.long_entry_ask) + (position.short_entry_bid-short.ask))
        fee_rate = float(self.paper.get("fee_bps_per_leg", 0)) / 10_000
        fees = position.quantity * (position.long_entry_ask+position.short_entry_bid+long.bid+short.ask) * fee_rate
        self.trades.append(Trade(position.position_id, position.opened_at, now.isoformat(), position.regime,
            position.threshold_bps, position.long_exchange, position.short_exchange, position.quantity,
            position.gross_notional_usd, position.long_entry_ask, position.short_entry_bid, long.bid,
            short.ask, position.entry_spread_bps, exit_spread, gross, fees, gross-fees, held, reason))
        self.positions.remove(position); self.save()


def generate_daily_report(engine: PaperEngine, day: date | str) -> tuple[Path, Path]:
    day = pd.Timestamp(day).date(); root = engine.root / "paper" / "reports"
    root.mkdir(parents=True, exist_ok=True)
    trades = pd.DataFrame([asdict(x) for x in engine.trades])
    if len(trades):
        closed = pd.to_datetime(trades.closed_at, utc=True).dt.date
        daily = trades[closed == day].copy()
    else: daily = pd.DataFrame(columns=[x.name for x in Trade.__dataclass_fields__.values()])
    csv_path = root / f"paper_trades_{day}.csv"; csv_tmp = csv_path.with_suffix(".csv.tmp")
    daily.to_csv(csv_tmp, index=False); os.replace(csv_tmp, csv_path)
    pnl = float(daily.net_pnl_usd.sum()) if len(daily) else 0.0
    wins = int((daily.net_pnl_usd > 0).sum()) if len(daily) else 0
    rows = "\n".join(f"| {x.position_id[:8]} | {x.long_exchange} | {x.short_exchange} | {x.entry_spread_bps:.2f} | {x.exit_spread_bps:.2f} | ${x.net_pnl_usd:.4f} | {x.close_reason} |" for x in daily.itertuples()) or "| — | — | — | — | — | — | 当日无平仓 |"
    status = ", ".join(f"{x}={'UP' if engine.connected[x] else 'DOWN'}" for x in EXCHANGES)
    report = f"""# BBO 纸面交易日报 — {day}

- 模式：**PAPER ONLY**（代码中无真实下单、认证或账户接口）
- 组合名义上限：`${float(engine.paper.get('gross_notional_usd',1000)):,.0f}`（两腿合计）
- 平仓笔数：{len(daily)}；胜率：{(100*wins/len(daily) if len(daily) else 0):.1f}%
- 当日已实现净 PnL：`${pnl:.4f}`
- 当前未平仓：{len(engine.positions)}
- 生成时连接状态：{status}
- 被风控阻止计数：`{json.dumps(engine.blocked_counts,ensure_ascii=False,sort_keys=True)}`

| ID | Long | Short | Entry bps | Exit bps | Net PnL | 原因 |
|---|---|---|---:|---:|---:|---|
{rows}

开仓价格使用 long ask / short bid；平仓价格使用 long bid / short ask。断连、缺腿、BBO 陈旧、跨所时间差超限或深度不足时不生成新纸面信号。
"""
    md_path = root / f"paper_report_{day}.md"; tmp = md_path.with_suffix(".md.tmp")
    tmp.write_text(report); os.replace(tmp, md_path)
    return md_path, csv_path
