"""Five-venue asynchronous executable-BBO collection.

Only public market-data WebSockets are used.  This module intentionally has no
authentication, order, account, or exchange-client trading methods.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import signal
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import websockets

from .config import ROOT, load_config

EXCHANGES = ("binance", "bitget", "gate", "hyperliquid", "okx")
DATA_ROOT = ROOT / "data" / "live_bbo"
RAW_ROOT = ROOT / "data" / "raw" / "live_bbo"
BBO_ROOT = DATA_ROOT / "bbo"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: Any, receive_ts: datetime) -> datetime:
    if value in (None, "", 0, "0"):
        return receive_ts
    number = float(value)
    unit = "ms" if number > 10**11 else "s"
    return pd.to_datetime(number, unit=unit, utc=True).to_pydatetime()


@dataclass(frozen=True)
class BBO:
    exchange: str
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    exchange_ts: datetime
    receive_ts: datetime
    sequence: int
    sequence_source: str
    connection_id: str

    def __post_init__(self):
        values = (self.bid, self.ask, self.bid_size, self.ask_size)
        if not all(pd.notna(x) and float(x) > 0 for x in values):
            raise ValueError("BBO prices and sizes must be positive")
        if self.bid > self.ask:
            raise ValueError("crossed BBO")

    def row(self) -> dict[str, Any]:
        row = asdict(self)
        row["exchange_ts"] = pd.Timestamp(self.exchange_ts)
        row["receive_ts"] = pd.Timestamp(self.receive_ts)
        return row


def _make(exchange: str, symbol: str, bid, ask, bid_size, ask_size, exchange_ts,
          native_sequence, receive_ts: datetime, local_sequence: int,
          connection_id: str) -> BBO:
    native = native_sequence not in (None, "")
    return BBO(exchange, symbol, float(bid), float(ask), float(bid_size), float(ask_size),
        _ts(exchange_ts, receive_ts), receive_ts, int(native_sequence if native else local_sequence),
        "native" if native else "connection_local", connection_id)


def parse_binance(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
                   connection_id: str) -> BBO | None:
    if message.get("e") != "bookTicker":
        return None
    return _make("binance", symbol, message["b"], message["a"], message["B"], message["A"],
        message.get("E") or message.get("T"), message.get("u"), receive_ts, local_sequence, connection_id)


def parse_bitget(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
                 connection_id: str) -> BBO | None:
    data = message.get("data") or []
    if not data or message.get("arg", {}).get("channel") != "ticker":
        return None
    item = data[0]
    return _make("bitget", symbol, item["bidPr"], item["askPr"], item["bidSz"], item["askSz"],
        item.get("ts") or message.get("ts"), None, receive_ts, local_sequence, connection_id)


def parse_gate(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
               connection_id: str) -> BBO | None:
    if message.get("channel") != "futures.book_ticker" or message.get("event") != "update":
        return None
    item = message.get("result") or {}
    return _make("gate", symbol, item["b"], item["a"], item["B"], item["A"],
        item.get("t") or message.get("time_ms") or message.get("time"),
        item.get("u") or item.get("id"), receive_ts, local_sequence, connection_id)


def parse_hyperliquid(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
                      connection_id: str) -> BBO | None:
    if message.get("channel") != "l2Book":
        return None
    item = message.get("data") or {}
    levels = item.get("levels") or [[], []]
    if len(levels) < 2 or not levels[0] or not levels[1]:
        return None
    bid, ask = levels[0][0], levels[1][0]
    return _make("hyperliquid", symbol, bid["px"], ask["px"], bid["sz"], ask["sz"],
        item.get("time"), None, receive_ts, local_sequence, connection_id)


def parse_okx(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
              connection_id: str) -> BBO | None:
    if message.get("arg", {}).get("channel") != "books5" or not message.get("data"):
        return None
    item = message["data"][0]
    if not item.get("bids") or not item.get("asks"):
        return None
    bid, ask = item["bids"][0], item["asks"][0]
    return _make("okx", symbol, bid[0], ask[0], bid[1], ask[1], item.get("ts"),
        item.get("seqId"), receive_ts, local_sequence, connection_id)


@dataclass(frozen=True)
class VenueAdapter:
    exchange: str
    url: str
    subscribe: Callable[[str], dict | None]
    parser: Callable[..., BBO | None]


def _none(_: str): return None
def _bitget_sub(symbol): return {"op":"subscribe","args":[{"instType":"USDT-FUTURES","channel":"ticker","instId":symbol}]}
def _gate_sub(symbol): return {"time":int(time.time()),"channel":"futures.book_ticker","event":"subscribe","payload":[symbol]}
def _hl_sub(symbol): return {"method":"subscribe","subscription":{"type":"l2Book","coin":symbol}}
def _okx_sub(symbol): return {"op":"subscribe","args":[{"channel":"books5","instId":symbol}]}


def adapters(symbols: dict[str, str]) -> dict[str, VenueAdapter]:
    return {
        "binance": VenueAdapter("binance", f"wss://fstream.binance.com/ws/{symbols['binance'].lower()}@bookTicker", _none, parse_binance),
        "bitget": VenueAdapter("bitget", "wss://ws.bitget.com/v2/ws/public", _bitget_sub, parse_bitget),
        "gate": VenueAdapter("gate", "wss://fx-ws.gateio.ws/v4/ws/usdt", _gate_sub, parse_gate),
        "hyperliquid": VenueAdapter("hyperliquid", "wss://api.hyperliquid.xyz/ws", _hl_sub, parse_hyperliquid),
        "okx": VenueAdapter("okx", "wss://ws.okx.com:8443/ws/v5/public", _okx_sub, parse_okx),
    }


class BBOStorage:
    """Archive exact server frames and write normalized immutable Parquet parts."""
    def __init__(self, flush_seconds=5, batch_rows=500, raw_root=RAW_ROOT, bbo_root=BBO_ROOT):
        self.flush_seconds = float(flush_seconds)
        self.batch_rows = int(batch_rows)
        self.raw_root, self.bbo_root = Path(raw_root), Path(bbo_root)
        self.queue: asyncio.Queue[BBO | None] = asyncio.Queue()
        self.raw_lock = asyncio.Lock()

    async def raw(self, exchange: str, receive_ts: datetime, connection_id: str, frame: str | bytes):
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8", errors="replace")
        path = self.raw_root / f"exchange={exchange}" / f"date={receive_ts:%Y-%m-%d}" / "messages.ndjson"
        record = json.dumps({"receive_ts":receive_ts.isoformat(), "connection_id":connection_id,
            "raw_message":frame}, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self.raw_lock:
            await asyncio.to_thread(self._append, path, record)

    @staticmethod
    def _append(path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    async def put(self, quote: BBO):
        await self.queue.put(quote)

    async def run(self):
        rows: list[BBO] = []
        while True:
            try:
                item = await asyncio.wait_for(self.queue.get(), timeout=self.flush_seconds)
            except asyncio.TimeoutError:
                item = False
            if item is None:
                if rows: await asyncio.to_thread(self._flush, rows)
                return
            if item is not False:
                rows.append(item)
            if rows and (item is False or len(rows) >= self.batch_rows):
                batch, rows = rows, []
                await asyncio.to_thread(self._flush, batch)

    def _flush(self, quotes: list[BBO]):
        frame = pd.DataFrame([x.row() for x in quotes])
        frame["date"] = pd.to_datetime(frame.receive_ts, utc=True).dt.strftime("%Y-%m-%d")
        frame["bucket"] = pd.to_datetime(frame.receive_ts, utc=True).dt.floor("5min")
        for (day, exchange, bucket), group in frame.groupby(["date", "exchange", "bucket"], sort=False):
            root = self.bbo_root / f"date={day}" / f"exchange={exchange}"
            root.mkdir(parents=True, exist_ok=True)
            # A stable five-minute part prevents an always-on collector from
            # creating one tiny file per flush. Rewrites are bounded to a
            # single small bucket; raw NDJSON remains the lossless journal.
            path = root / f"part-{pd.Timestamp(bucket).strftime('%H%M')}.parquet"
            incoming = group.drop(columns=["date", "bucket"])
            old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
            combined = pd.concat([old, incoming], ignore_index=True)
            combined = combined.sort_values("receive_ts").drop_duplicates(
                ["exchange", "connection_id", "sequence", "receive_ts"], keep="last")
            tmp = path.with_suffix(".parquet.tmp")
            combined.to_parquet(tmp, index=False)
            os.replace(tmp, path)


class WebSocketVenue:
    def __init__(self, adapter: VenueAdapter, symbol: str, storage: BBOStorage,
                 on_quote, on_status, settings: dict):
        self.adapter, self.symbol, self.storage = adapter, symbol, storage
        self.on_quote, self.on_status, self.settings = on_quote, on_status, settings

    async def run(self, stop: asyncio.Event):
        delay = float(self.settings.get("reconnect_min_seconds", 1))
        maximum = float(self.settings.get("reconnect_max_seconds", 30))
        log = logging.getLogger("live_bbo")
        while not stop.is_set():
            connection_id = uuid.uuid4().hex
            try:
                async with websockets.connect(self.adapter.url,
                    ping_interval=float(self.settings.get("heartbeat_seconds", 20)),
                    ping_timeout=float(self.settings.get("heartbeat_seconds", 20)),
                    close_timeout=5, max_queue=4096) as ws:
                    sub = self.adapter.subscribe(self.symbol)
                    if sub is not None: await ws.send(json.dumps(sub, separators=(",", ":")))
                    await self.on_status(self.adapter.exchange, True, connection_id)
                    delay = float(self.settings.get("reconnect_min_seconds", 1)); sequence = 0
                    async for frame in ws:
                        received = utcnow(); sequence += 1
                        await self.storage.raw(self.adapter.exchange, received, connection_id, frame)
                        if frame in ("pong", b"pong"): continue
                        try: message = json.loads(frame)
                        except (json.JSONDecodeError, TypeError): continue
                        quote = self.adapter.parser(message, self.symbol, received, sequence, connection_id)
                        if quote is not None:
                            await self.storage.put(quote)
                            await self.on_quote(quote)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s websocket disconnected: %s", self.adapter.exchange, exc)
            finally:
                await self.on_status(self.adapter.exchange, False, connection_id)
            if not stop.is_set():
                await asyncio.sleep(delay + random.random() * min(delay, 1.0))
                delay = min(maximum, delay * 2)


async def run_live_bbo():
    """Run the five collectors and paper engine until SIGINT/SIGTERM."""
    from .paper_trading import PaperEngine, generate_daily_report

    cfg = load_config(); settings = cfg.get("live_bbo", {}); symbols = cfg["symbols"]
    paper = PaperEngine(settings.get("paper", {}), settings, DATA_ROOT)
    storage = BBOStorage(settings.get("parquet_flush_seconds", 5),
        settings.get("parquet_batch_rows", 500))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    venues = [WebSocketVenue(spec, symbols[name], storage, paper.on_quote,
        paper.on_status, settings) for name, spec in adapters(symbols).items()]
    tasks = [asyncio.create_task(storage.run(), name="bbo-storage")]
    tasks += [asyncio.create_task(x.run(stop), name=f"bbo-{x.adapter.exchange}") for x in venues]
    reporter = asyncio.create_task(_daily_reporter(stop, paper, generate_daily_report), name="paper-report")
    try:
        await stop.wait()
    finally:
        for task in tasks[1:]: task.cancel()
        await asyncio.gather(*tasks[1:], return_exceptions=True)
        await storage.queue.put(None); await tasks[0]
        reporter.cancel(); await asyncio.gather(reporter, return_exceptions=True)
        paper.save(); generate_daily_report(paper, utcnow().date())


async def _daily_reporter(stop, engine, generate):
    while not stop.is_set():
        day = utcnow().date()
        # Refresh intraday as well as at rollover; the file is deterministic
        # and atomically rewritten by the report generator.
        generate(engine, day)
        try: await asyncio.wait_for(stop.wait(), timeout=60)
        except asyncio.TimeoutError: pass


def run():
    asyncio.run(run_live_bbo())
