"""Paper-only execution state driven by public executable BBOs.

There are deliberately no credentials, accounts, private channels, or order
methods in this module.  Every fill is a simulation at the observed BBO.
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .config import ROOT, load_config
from .gate_regime_15m import causal_regime_for_time
from .live_bbo import (BBO, BBOStorage, CAPACITY_VALID, CollectorMonitor, EXCHANGES, SIZE_UNIT_OK,
    WebSocketVenue, adapters, fetch_product_metadata, refresh_product_metadata)

ALLOWED_ENTRY_REGIMES = frozenset({"NORMAL", "TRANSIENT_DISLOCATION"})
PRICE_ONLY_BEFORE_FUNDING = "PRICE_ONLY_BEFORE_FUNDING"
SETTLED_FUNDING_INCLUDED = "SETTLED_FUNDING_INCLUDED"


def _utc(value=None) -> datetime:
    value = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    return (value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")).to_pydatetime()


class CausalRegimeProvider:
    """Exact causal-label lookup; retrospective/manual date presets are rejected."""

    def __init__(self, labels: pd.DataFrame):
        required = {"open_time", "causal_regime"}
        if not required <= set(labels.columns):
            raise ValueError("causal regime label table required")
        self.labels = labels[list(required)].copy()
        self.labels["open_time"] = pd.to_datetime(self.labels.open_time, utc=True)
        if not set(self.labels.causal_regime) <= {
                "NORMAL", "TRANSIENT_DISLOCATION", "STRUCTURAL_PREMIUM", "STALE_OR_INVALID"}:
            raise ValueError("unknown causal regime")

    @classmethod
    def from_csv(cls, path: Path):
        return cls(pd.read_csv(path))

    def __call__(self, value) -> str:
        bar = pd.Timestamp(value)
        bar = (bar.tz_localize("UTC") if bar.tzinfo is None else bar.tz_convert("UTC")).floor("15min")
        return self._lookup(bar.isoformat())

    @lru_cache(maxsize=256)
    def _lookup(self, bar_iso: str) -> str:
        # The Gate strategy-facing provider remains the single lookup policy;
        # cache only the immutable result for one completed 15-minute key.
        return causal_regime_for_time(self.labels, bar_iso)


@dataclass(frozen=True)
class FundingEvent:
    event_id: str
    exchange: str
    settled_at: str
    rate: float
    mark_price: float
    source: str = "PUBLIC_SETTLED_FUNDING"


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
    raw_entry_edge_bps: float
    estimated_total_cost_bps: float
    net_entry_edge_bps: float
    long_entry_sequence: int
    short_entry_sequence: int
    entry_long_fee_usd: float
    entry_short_fee_usd: float
    funding_pnl_usd: float = 0.0
    funding_event_ids: list[str] = field(default_factory=list)


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
    raw_entry_edge_bps: float
    estimated_total_cost_bps: float
    net_entry_edge_bps: float
    exit_spread_bps: float
    gross_price_pnl_usd: float
    funding_pnl_usd: float
    entry_long_fee_usd: float
    entry_short_fee_usd: float
    exit_long_fee_usd: float
    exit_short_fee_usd: float
    fee_pnl_usd: float
    slippage_assumption_usd: float
    net_pnl_usd: float
    pnl_scope: str
    holding_seconds: float
    close_reason: str
    funding_event_ids: list[str] = field(default_factory=list)


@dataclass
class Confirmation:
    long_exchange: str
    short_exchange: str
    threshold_bps: float
    regime: str
    started_at: datetime

    @property
    def key(self):
        return self.long_exchange, self.short_exchange, self.threshold_bps, self.regime


class PaperEngine:
    def __init__(self, paper: dict, live: dict, root: Path,
                 regime_provider: Callable[[datetime], str]):
        self.paper, self.live, self.root = paper, live, Path(root)
        allowed = set(paper.get("allowed_regimes", []))
        if not allowed or not allowed <= ALLOWED_ENTRY_REGIMES:
            raise ValueError("allowed_regimes must contain causal entry labels only")
        self.regime_provider = regime_provider
        self.quotes: dict[str, BBO] = {}
        self.connected = {x: False for x in EXCHANGES}
        self.connection_ids: dict[str, str] = {}
        self.positions: list[Position] = []
        self.trades: list[Trade] = []
        self.blocked_counts: dict[str, int] = {}
        self.processed_funding_event_ids: set[str] = set()
        self.confirmation: Confirmation | None = None
        self.ledger_path = self.root / "paper_bbo" / "ledger.json"
        self._load()

    def _load(self):
        if not self.ledger_path.exists():
            return
        data = json.loads(self.ledger_path.read_text())
        self.positions = [Position(**x) for x in data.get("positions", [])]
        self.trades = [Trade(**x) for x in data.get("trades", [])]
        self.blocked_counts = data.get("blocked_counts", {})
        self.processed_funding_event_ids = set(data.get("processed_funding_event_ids", []))

    def save(self):
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"paper_only": True, "updated_at": _utc().isoformat(),
            "positions": [asdict(x) for x in self.positions],
            "trades": [asdict(x) for x in self.trades],
            "blocked_counts": self.blocked_counts,
            "processed_funding_event_ids": sorted(self.processed_funding_event_ids)}
        tmp = self.ledger_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        os.replace(tmp, self.ledger_path)

    async def on_status(self, exchange: str, connected: bool, connection_id: str):
        if connected:
            self.connected[exchange] = True
            self.connection_ids[exchange] = connection_id
        elif self.connection_ids.get(exchange) == connection_id:
            self.connected[exchange] = False
            self.quotes.pop(exchange, None)
            if self.confirmation and exchange in self.confirmation.key[:2]:
                self._reset_confirmation("confirmation_leg_disconnected")

    async def on_quote(self, quote: BBO):
        if (not self.connected.get(quote.exchange)
                or self.connection_ids.get(quote.exchange) != quote.connection_id):
            return
        old = self.quotes.get(quote.exchange)
        if old and old.connection_id == quote.connection_id and quote.sequence <= old.sequence:
            self._block("non_monotonic_sequence")
            return
        self.quotes[quote.exchange] = quote
        self.evaluate(quote.receive_ts)

    def funding_event_relevant(self, exchange: str, settled_at) -> bool:
        settled = _utc(settled_at)
        for position in self.positions:
            if _utc(position.opened_at) < settled and exchange in {
                    position.long_exchange, position.short_exchange}:
                return True
        for trade in self.trades:
            if (_utc(trade.opened_at) < settled < _utc(trade.closed_at)
                    and exchange in {trade.long_exchange, trade.short_exchange}):
                return True
        return False

    @staticmethod
    def _funding_delta(exchange: str, long_exchange: str, short_exchange: str,
                       quantity: float, mark_price: float, rate: float) -> float:
        if exchange == long_exchange:return -quantity * mark_price * rate
        if exchange == short_exchange:return quantity * mark_price * rate
        return 0.

    def on_funding_event(self, event: FundingEvent) -> bool:
        if event.event_id in self.processed_funding_event_ids:
            return False
        if event.exchange not in EXCHANGES or not np.isfinite(event.rate):
            raise ValueError("invalid settled funding event")
        settled = _utc(event.settled_at)
        relevant = self.funding_event_relevant(event.exchange, settled)
        if relevant and (not np.isfinite(event.mark_price) or event.mark_price <= 0):
            raise ValueError("relevant funding event requires positive settlement mark")
        for position in self.positions:
            if not _utc(position.opened_at) < settled:
                continue
            delta = self._funding_delta(event.exchange, position.long_exchange,
                position.short_exchange, position.quantity, event.mark_price, event.rate)
            if delta or event.exchange in {position.long_exchange, position.short_exchange}:
                position.funding_pnl_usd += delta
                position.funding_event_ids.append(event.event_id)
        for trade in self.trades:
            if not _utc(trade.opened_at) < settled < _utc(trade.closed_at):continue
            delta = self._funding_delta(event.exchange, trade.long_exchange,
                trade.short_exchange, trade.quantity, event.mark_price, event.rate)
            if delta or event.exchange in {trade.long_exchange, trade.short_exchange}:
                trade.funding_pnl_usd += delta;trade.net_pnl_usd += delta
                trade.pnl_scope = SETTLED_FUNDING_INCLUDED;trade.funding_event_ids.append(event.event_id)
        self.processed_funding_event_ids.add(event.event_id)
        self.save()
        return True

    def _block(self, reason: str):
        self.blocked_counts[reason] = self.blocked_counts.get(reason, 0) + 1

    def _reset_confirmation(self, reason: str | None = None):
        if self.confirmation is not None and reason:
            self._block(reason)
        self.confirmation = None

    def pair(self, long_exchange: str, short_exchange: str, now=None):
        now = _utc(now); reasons = []
        if long_exchange == short_exchange:
            reasons.append("same_venue")
        for venue in (long_exchange, short_exchange):
            if not self.connected.get(venue): reasons.append(f"disconnected:{venue}")
            if venue not in self.quotes: reasons.append(f"missing_leg:{venue}")
        if reasons:
            return None, reasons
        long, short = self.quotes[long_exchange], self.quotes[short_exchange]
        stale = float(self.live.get("stale_after_ms", 3000))
        for quote in (long, short):
            receive_age = (now - quote.receive_ts).total_seconds() * 1000
            exchange_age = (now - quote.exchange_ts).total_seconds() * 1000
            if receive_age > stale: reasons.append(f"stale_receive:{quote.exchange}")
            if exchange_age > stale: reasons.append(f"stale_exchange:{quote.exchange}")
            if receive_age < -1000 or exchange_age < -1000:
                reasons.append(f"future_timestamp:{quote.exchange}")
        skew = abs((long.exchange_ts - short.exchange_ts).total_seconds() * 1000)
        if skew > float(self.live.get("max_cross_exchange_skew_ms", 1500)):
            reasons.append("cross_exchange_time_skew")
        return (long, short), reasons

    def _fee_bps(self, exchange: str) -> float:
        value = float(self.paper.get("taker_fee_bps", {}).get(exchange, np.nan))
        if not np.isfinite(value) or value <= 0:
            raise ValueError(f"positive conservative taker fee required for {exchange}")
        return value

    def _estimated_total_cost_bps(self, long_exchange: str, short_exchange: str) -> float:
        return (2 * self._fee_bps(long_exchange) + 2 * self._fee_bps(short_exchange)
            + 4 * float(self.paper.get("slippage_bps_per_fill", 0))
            + float(self.paper.get("safety_buffer_bps", 0)))

    def _capacity(self, long: BBO, short: BBO, quantity: float, entering=True) -> list[str]:
        reasons = []
        for quote in (long, short):
            if quote.size_unit_status != SIZE_UNIT_OK:
                reasons.append(f"size_unit_unknown:{quote.exchange}")
            elif quote.capacity_status != CAPACITY_VALID:
                reasons.append(f"capacity_unknown:{quote.exchange}")
        if reasons:
            return reasons
        long_qty = (long.normalized_underlying_ask_qty if entering
                    else long.normalized_underlying_bid_qty)
        short_qty = (short.normalized_underlying_bid_qty if entering
                     else short.normalized_underlying_ask_qty)
        long_usd = long.ask_notional_usd if entering else long.bid_notional_usd
        short_usd = short.bid_notional_usd if entering else short.ask_notional_usd
        long_px = long.ask if entering else long.bid
        short_px = short.bid if entering else short.ask
        if not np.isfinite(long_qty) or long_qty < quantity or long_usd < quantity * long_px:
            reasons.append("insufficient_normalized_long_capacity")
        if not np.isfinite(short_qty) or short_qty < quantity or short_usd < quantity * short_px:
            reasons.append("insufficient_normalized_short_capacity")
        return reasons

    def _configured_pairs(self):
        pairs = []
        for item in self.paper.get("pairs", []):
            left, right = item.split("/", 1)
            if "gate" not in {left, right} or left not in EXCHANGES or right not in EXCHANGES:
                raise ValueError("paper pairs must be valid Gate pairs")
            pairs.extend(((left, right), (right, left)))
        return pairs

    def evaluate(self, now=None):
        now = _utc(now)
        if self.positions:
            self._try_close(self.positions[0], now)
            return
        if int(self.paper.get("max_open_positions", 1)) < 1:
            self._reset_confirmation()
            return
        regime = self.regime_provider(now)
        if regime not in set(self.paper["allowed_regimes"]):
            self._block(f"regime_filtered:{regime}")
            self._reset_confirmation("confirmation_regime_changed")
            return
        thresholds = sorted(float(x) for x in self.paper.get("entry_thresholds_bps", ()))
        if thresholds != [100.0, 150.0, 200.0]:
            self._block("unfrozen_threshold_config")
            self._reset_confirmation()
            return
        gross_cap = float(self.paper.get("gross_notional_usd", 1000))
        if gross_cap <= 0 or gross_cap > 1000:
            self._block("phase_one_notional_guard")
            self._reset_confirmation()
            return
        candidates = []
        for long_exchange, short_exchange in self._configured_pairs():
            pair, reasons = self.pair(long_exchange, short_exchange, now)
            if reasons:
                continue
            long, short = pair
            quantity = gross_cap / (long.ask + short.bid)
            reasons = self._capacity(long, short, quantity, entering=True)
            if reasons:
                for reason in set(reasons): self._block(reason)
                continue
            raw = (short.bid - long.ask) / long.ask * 10_000
            cost = self._estimated_total_cost_bps(long.exchange, short.exchange)
            net = raw - cost
            crossed = [x for x in thresholds if net >= x]
            if crossed:
                candidates.append((net, max(crossed), raw, cost, quantity, long, short))
        if not candidates:
            self._reset_confirmation("confirmation_condition_failed")
            return
        net, threshold, raw, cost, quantity, long, short = max(candidates, key=lambda x: x[0])
        key = (long.exchange, short.exchange, threshold, regime)
        if self.confirmation is None or self.confirmation.key != key:
            self.confirmation = Confirmation(*key, started_at=now)
            return
        if (now - self.confirmation.started_at).total_seconds() < float(
                self.paper.get("confirmation_seconds", 5)):
            return
        entry_long_fee = quantity * long.ask * self._fee_bps(long.exchange) / 10_000
        entry_short_fee = quantity * short.bid * self._fee_bps(short.exchange) / 10_000
        self.positions.append(Position(uuid.uuid4().hex, now.isoformat(), regime, threshold,
            long.exchange, short.exchange, quantity, quantity * (long.ask + short.bid),
            long.ask, short.bid, raw, cost, net, long.sequence, short.sequence,
            entry_long_fee, entry_short_fee))
        self.confirmation = None
        self.save()

    def _try_close(self, position: Position, now: datetime):
        pair, reasons = self.pair(position.long_exchange, position.short_exchange, now)
        if reasons:
            for reason in set(reasons): self._block(reason)
            return
        long, short = pair
        reasons = self._capacity(long, short, position.quantity, entering=False)
        if reasons:
            for reason in set(reasons): self._block(f"exit_{reason}")
            return
        exit_spread = (short.ask - long.bid) / long.bid * 10_000
        held = (now - _utc(position.opened_at)).total_seconds()
        reason = None
        if exit_spread <= float(self.paper.get("exit_spread_bps", 20)):
            reason = "CONVERGENCE"
        elif held >= float(self.paper.get("max_holding_seconds", 86400)):
            reason = "MAX_HOLD"
        if reason is None:
            return
        gross = position.quantity * ((long.bid - position.long_entry_ask)
                                     + (position.short_entry_bid - short.ask))
        exit_long_fee = position.quantity * long.bid * self._fee_bps(long.exchange) / 10_000
        exit_short_fee = position.quantity * short.ask * self._fee_bps(short.exchange) / 10_000
        fee_pnl = -(position.entry_long_fee_usd + position.entry_short_fee_usd
                    + exit_long_fee + exit_short_fee)
        slippage = position.quantity * (position.long_entry_ask + position.short_entry_bid
            + long.bid + short.ask) * float(self.paper.get("slippage_bps_per_fill", 0)) / 10_000
        net = gross + position.funding_pnl_usd + fee_pnl - slippage
        scope = SETTLED_FUNDING_INCLUDED if position.funding_event_ids else PRICE_ONLY_BEFORE_FUNDING
        self.trades.append(Trade(position.position_id, position.opened_at, now.isoformat(),
            position.regime, position.threshold_bps, position.long_exchange, position.short_exchange,
            position.quantity, position.gross_notional_usd, position.long_entry_ask,
            position.short_entry_bid, long.bid, short.ask, position.raw_entry_edge_bps,
            position.estimated_total_cost_bps, position.net_entry_edge_bps, exit_spread, gross,
            position.funding_pnl_usd, position.entry_long_fee_usd,
            position.entry_short_fee_usd, exit_long_fee, exit_short_fee, fee_pnl, slippage,
            net, scope, held, reason, list(position.funding_event_ids)))
        self.positions.remove(position)
        self.save()


class PaperMonitor:
    """Fan out collector observations without coupling collection to strategy code."""

    def __init__(self, collector: CollectorMonitor, engine: PaperEngine):
        self.collector, self.engine, self.counters = collector, engine, collector.counters

    async def on_status(self, exchange, connected, connection_id):
        await self.collector.on_status(exchange, connected, connection_id)
        await self.engine.on_status(exchange, connected, connection_id)

    def on_message(self, *args, **kwargs):
        return self.collector.on_message(*args, **kwargs)

    async def on_quote(self, quote):
        await self.collector.on_quote(quote)
        await self.engine.on_quote(quote)


def generate_daily_report(engine: PaperEngine, day: date | str) -> tuple[Path, Path]:
    day = pd.Timestamp(day).date(); root = engine.root / "paper_bbo" / "reports"
    root.mkdir(parents=True, exist_ok=True)
    trades = pd.DataFrame([asdict(x) for x in engine.trades])
    if len(trades):
        daily = trades[pd.to_datetime(trades.closed_at, utc=True).dt.date == day].copy()
    else:
        daily = pd.DataFrame(columns=Trade.__dataclass_fields__)
    csv_path = root / f"paper_trades_{day}.csv"; tmp = csv_path.with_suffix(".csv.tmp")
    daily.to_csv(tmp, index=False); os.replace(tmp, csv_path)
    pnl = float(daily.net_pnl_usd.sum()) if len(daily) else 0.0
    scopes = sorted(set(daily.pnl_scope)) if len(daily) else [PRICE_ONLY_BEFORE_FUNDING]
    report = f"""# BBO paper report — {day}

- Mode: **PAPER ONLY**; public market data and simulated BBO fills only
- Entry threshold field: `net_entry_edge_bps`
- Gross notional cap: `${float(engine.paper.get('gross_notional_usd', 1000)):,.0f}` across both legs
- Closed trades: {len(daily)}; realized paper PnL: `${pnl:.4f}`
- Open positions: {len(engine.positions)}
- Funding scope: `{', '.join(scopes)}`
- Conservative taker fees (bps/fill): `{json.dumps(engine.paper['taker_fee_bps'], sort_keys=True)}`
- Slippage assumption: `{float(engine.paper.get('slippage_bps_per_fill', 0)):.2f}` bps/fill
- Safety buffer: `{float(engine.paper.get('safety_buffer_bps', 0)):.2f}` bps
- Blocked counts: `{json.dumps(engine.blocked_counts, sort_keys=True)}`

`PRICE_ONLY_BEFORE_FUNDING` is not a complete arbitrage-net-return claim. A trade is
labelled `SETTLED_FUNDING_INCLUDED` only after public settled funding events have
been ingested and persisted for its open holding interval.
"""
    md_path = root / f"paper_report_{day}.md"; tmp = md_path.with_suffix(".md.tmp")
    tmp.write_text(report); os.replace(tmp, md_path)
    return md_path, csv_path


async def run_paper_bbo(duration_seconds: float | None = None):
    from .funding_service import FundingSettlementService
    from .runtime_health import generate_runtime_health, monitor_runtime
    cfg = load_config(); live = cfg["live_bbo"]; paper = cfg["paper_bbo"]
    provider = CausalRegimeProvider.from_csv(ROOT / "reports_15m/gate_causal_regime_labels_15m.csv")
    engine = PaperEngine(paper, live, ROOT / "data", provider)
    metadata = await asyncio.to_thread(fetch_product_metadata, cfg["symbols"])
    collector = CollectorMonitor(live, metadata); monitor = PaperMonitor(collector, engine)
    storage = BBOStorage(live.get("parquet_flush_seconds", 5),
        live.get("parquet_batch_rows", 500), settings=live)
    funding_cfg=cfg.get("funding_settlement_service",{})
    funding=FundingSettlementService(engine,cfg["symbols"],
        poll_seconds=funding_cfg.get("poll_seconds",60),
        backfill_hours=funding_cfg.get("startup_backfill_hours",48))
    stop = asyncio.Event(); loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    venues = [WebSocketVenue(spec, cfg["symbols"][name], storage, metadata, monitor, live)
              for name, spec in adapters(cfg["symbols"]).items()]
    storage_tasks = [asyncio.create_task(storage.run(),name="paper-bbo-storage"),
        asyncio.create_task(storage.run_raw(),name="paper-bbo-raw-storage")]
    venue_tasks = [asyncio.create_task(venue.run(stop),name=f"paper-bbo-{venue.adapter.exchange}") for venue in venues]
    metadata_task=asyncio.create_task(refresh_product_metadata(cfg["symbols"],metadata,stop,
        float(live.get("metadata_refresh_seconds",3600))),name="paper-metadata-refresh")
    funding_task=asyncio.create_task(funding.run(stop),name="paper-funding-settlements")
    health_task=asyncio.create_task(monitor_runtime(stop,collector,funding,storage,
        float(funding_cfg.get("health_interval_seconds",30))),name="paper-runtime-health")
    timer = asyncio.create_task(asyncio.sleep(duration_seconds)) if duration_seconds is not None else None
    try:
        if timer is None: await stop.wait()
        else: await timer; stop.set()
    finally:
        stop.set()
        for task in venue_tasks+[metadata_task]: task.cancel()
        await asyncio.gather(*venue_tasks,metadata_task,return_exceptions=True)
        await asyncio.gather(funding_task,health_task,return_exceptions=True)
        await storage.queue.put(None); await storage.raw_queue.put(None)
        await asyncio.gather(*storage_tasks)
        await funding.shutdown();await asyncio.to_thread(storage.maintenance)
        collector.save(); engine.save(); generate_daily_report(engine, _utc().date())
        _,health=generate_runtime_health(collector,funding,storage)
        print(json.dumps(health["funding_service"],ensure_ascii=False))
    return engine


def run(duration_seconds: float | None = None):
    return asyncio.run(run_paper_bbo(duration_seconds))
