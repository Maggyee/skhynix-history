"""Five-venue asynchronous executable-BBO collection.

Only public market-data WebSockets are used.  This module intentionally has no
authentication, order, account, or exchange-client trading methods.
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import logging
import os
import random
import shutil
import signal
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import httpx
import numpy as np
import websockets

from .config import ROOT, load_config

EXCHANGES = ("binance", "bitget", "gate", "hyperliquid", "okx")
DATA_ROOT = ROOT / "data" / "live_bbo"
RAW_ROOT = ROOT / "data" / "raw" / "live_bbo"
BBO_ROOT = DATA_ROOT / "bbo"
METADATA_ROOT = DATA_ROOT / "metadata"
METADATA_HISTORY_ROOT = DATA_ROOT / "metadata_history"
HEALTH_ROOT = DATA_ROOT / "health"
SCHEMA_VERSION = 2
SIZE_UNIT_OK = "SIZE_UNIT_OK"
SIZE_UNIT_UNKNOWN = "SIZE_UNIT_UNKNOWN"
CAPACITY_VALID = "CAPACITY_VALID"
CAPACITY_UNKNOWN = "CAPACITY_UNKNOWN"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ts(value: Any, receive_ts: datetime) -> datetime:
    if value in (None, "", 0, "0"):
        return receive_ts
    number = float(value)
    unit = "ms" if number > 10**11 else "s"
    return pd.to_datetime(number, unit=unit, utc=True).to_pydatetime()


@dataclass(frozen=True)
class ProductMetadata:
    exchange: str
    symbol: str
    native_size_unit: str
    contract_multiplier: float
    underlying_asset: str | None
    quote_asset: str | None
    size_unit_status: str
    raw_file: str
    snapshot_at: datetime
    metadata_snapshot_id: str = ""
    source: str = "PUBLIC_PRODUCT_METADATA"
    status: str = "VALID"

    @property
    def usable(self) -> bool:
        return (self.size_unit_status == SIZE_UNIT_OK and pd.notna(self.contract_multiplier)
                and self.contract_multiplier > 0)

    @property
    def effective_observed_at(self) -> datetime:
        return self.snapshot_at


def _metadata_source(exchange: str) -> str:
    return {
        "binance":"GET /fapi/v1/exchangeInfo",
        "bitget":"GET /api/v2/mix/market/contracts",
        "gate":"GET /api/v4/futures/usdt/contracts/{symbol}",
        "hyperliquid":"POST /info metaAndAssetCtxs",
        "okx":"GET /api/v5/public/instruments",
    }[exchange]


def _metadata_id(exchange: str, symbol: str, observed_at: datetime, raw_file: str,
                 unit: str, multiplier: float) -> str:
    value = f"{exchange}|{symbol}|{observed_at.isoformat()}|{raw_file}|{unit}|{multiplier}"
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def parse_product_metadata(exchange: str, symbol: str, payload: Any,
                           raw_file: str = "") -> ProductMetadata:
    """Parse public product metadata; fail closed when size units are ambiguous."""
    now = utcnow(); unit = "UNKNOWN"; multiplier = float("nan")
    underlying = quote = None
    try:
        if exchange == "binance":
            item = next(x for x in payload["symbols"] if x["symbol"] == symbol)
            unit, multiplier = "BASE_ASSET", 1.0
            underlying, quote = item.get("baseAsset"), item.get("quoteAsset")
        elif exchange == "bitget":
            item = (payload.get("data") or [])[0]
            multiplier = float(item["sizeMultiplier"])
            unit = "CONTRACT"
            underlying = item.get("baseCoin"); quote = item.get("quoteCoin")
        elif exchange == "gate":
            item = payload
            multiplier = float(item["quanto_multiplier"])
            unit = "CONTRACT"
            underlying = item.get("underlying", "").split("_")[0] or None
            quote = item.get("settle") or "USDT"
        elif exchange == "okx":
            item = (payload.get("data") or [])[0]
            ct_val = float(item["ctVal"]); ct_mult = float(item.get("ctMult") or 1)
            underlying = item.get("ctValCcy") or item.get("baseCcy")
            quote = item.get("settleCcy") or item.get("quoteCcy")
            if not underlying or underlying.upper() in {"USD", "USDT", "USDC"}:
                raise ValueError("OKX ctValCcy is not an underlying unit")
            multiplier, unit = ct_val * ct_mult, "CONTRACT"
        elif exchange == "hyperliquid":
            universe = payload[0]["universe"] if isinstance(payload, list) else payload["universe"]
            coin = symbol.split(":")[-1]
            item = next(x for x in universe if x["name"] in {symbol, coin})
            unit, multiplier = "UNDERLYING_ASSET", 1.0
            underlying, quote = item.get("name"), "USDC"
        else:
            raise ValueError("unsupported exchange")
        if not np.isfinite(multiplier) or multiplier <= 0:
            raise ValueError("invalid multiplier")
        status = SIZE_UNIT_OK
    except (KeyError, IndexError, StopIteration, TypeError, ValueError):
        status = SIZE_UNIT_UNKNOWN; multiplier = float("nan")
    snapshot_id = _metadata_id(exchange, symbol, now, raw_file, unit, multiplier)
    return ProductMetadata(exchange, symbol, unit, multiplier, underlying, quote,
        status, raw_file, now, snapshot_id, _metadata_source(exchange),
        "VALID" if status == SIZE_UNIT_OK else SIZE_UNIT_UNKNOWN)


def _metadata_request(client: httpx.Client, exchange: str, symbol: str):
    if exchange == "binance":
        return client.get("https://fapi.binance.com/fapi/v1/exchangeInfo").json()
    if exchange == "bitget":
        return client.get("https://api.bitget.com/api/v2/mix/market/contracts",
                          params={"productType":"USDT-FUTURES","symbol":symbol}).json()
    if exchange == "gate":
        return client.get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{symbol}").json()
    if exchange == "okx":
        return client.get("https://www.okx.com/api/v5/public/instruments",
                          params={"instType":"SWAP","instId":symbol}).json()
    if exchange == "hyperliquid":
        body = {"type":"metaAndAssetCtxs"}
        if ":" in symbol: body["dex"] = symbol.split(":", 1)[0]
        return client.post("https://api.hyperliquid.xyz/info", json=body).json()
    raise ValueError(exchange)


def _metadata_history_row(meta: ProductMetadata) -> dict[str, Any]:
    return {"exchange":meta.exchange, "symbol":meta.symbol,
        "effective_observed_at":pd.Timestamp(meta.effective_observed_at),
        "native_size_unit":meta.native_size_unit,
        "contract_multiplier":meta.contract_multiplier,
        "underlying_asset":meta.underlying_asset, "quote_asset":meta.quote_asset,
        "source":meta.source, "raw_file":meta.raw_file, "status":meta.status,
        "metadata_snapshot_id":meta.metadata_snapshot_id}


def _persist_metadata_history(metas: list[ProductMetadata], history_root: Path):
    frame = pd.DataFrame([_metadata_history_row(x) for x in metas])
    frame["effective_observed_at"] = pd.to_datetime(frame.effective_observed_at, utc=True)
    frame["date"] = frame.effective_observed_at.dt.strftime("%Y-%m-%d")
    for (day, exchange), group in frame.groupby(["date", "exchange"], sort=False):
        root = history_root / f"date={day}" / f"exchange={exchange}"
        root.mkdir(parents=True, exist_ok=True); path = root / "snapshots.parquet"
        incoming = group.drop(columns="date")
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
        combined = pd.concat([old, incoming], ignore_index=True).drop_duplicates(
            "metadata_snapshot_id", keep="last").sort_values("effective_observed_at")
        tmp = path.with_suffix(".parquet.tmp")
        combined.to_parquet(tmp, index=False, compression="zstd"); os.replace(tmp, path)


def read_metadata_history(root: Path = METADATA_HISTORY_ROOT) -> pd.DataFrame:
    files = sorted(Path(root).glob("date=*/exchange=*/snapshots.parquet"))
    columns = list(_metadata_history_row(ProductMetadata("", "", "UNKNOWN", float("nan"),
        None, None, SIZE_UNIT_UNKNOWN, "", utcnow())).keys())
    if not files: return pd.DataFrame(columns=columns)
    frame = pd.concat((pd.read_parquet(x) for x in files), ignore_index=True)
    frame["effective_observed_at"] = pd.to_datetime(frame.effective_observed_at, utc=True)
    return frame.sort_values(["exchange", "symbol", "effective_observed_at"])


def fetch_product_metadata(symbols: dict[str, str], root: Path = METADATA_ROOT,
                           client: httpx.Client | None = None,
                           history_root: Path | None = None) -> dict[str, ProductMetadata]:
    root = Path(root); root.mkdir(parents=True, exist_ok=True); own = client is None
    history_root = Path(history_root) if history_root is not None else (
        METADATA_HISTORY_ROOT if root == METADATA_ROOT else root / "history")
    client = client or httpx.Client(timeout=20, headers={"User-Agent":"skhynix-public-research/0.1"})
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ"); rows = {}; snapshots = []
    try:
        for exchange in EXCHANGES:
            raw_file = root / "raw" / f"date={utcnow():%Y-%m-%d}" / f"exchange={exchange}" / f"{stamp}.json"
            raw_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                payload = _metadata_request(client, exchange, symbols[exchange])
                raw_file.write_text(json.dumps(payload, ensure_ascii=False))
            except Exception as exc:
                payload = {"metadata_fetch_error": str(exc)}
                raw_file.write_text(json.dumps(payload, ensure_ascii=False))
            meta = parse_product_metadata(exchange, symbols[exchange], payload, str(raw_file))
            rows[exchange] = meta; snapshots.append(asdict(meta))
    finally:
        if own: client.close()
    frame = pd.DataFrame(snapshots)
    frame.to_parquet(root / "latest.parquet", index=False, compression="zstd")
    (root / "latest.json").write_text(frame.to_json(orient="records", date_format="iso", indent=2))
    _persist_metadata_history(list(rows.values()), history_root)
    return rows


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
    native_size_unit: str = "UNKNOWN"
    contract_multiplier: float = float("nan")
    normalized_underlying_bid_qty: float = float("nan")
    normalized_underlying_ask_qty: float = float("nan")
    bid_notional_usd: float = float("nan")
    ask_notional_usd: float = float("nan")
    size_unit_status: str = SIZE_UNIT_UNKNOWN
    raw_message_id: str = ""
    schema_version: int = SCHEMA_VERSION
    metadata_snapshot_id: str = ""
    capacity_status: str = CAPACITY_UNKNOWN
    capacity_error_reason: str = SIZE_UNIT_UNKNOWN

    def __post_init__(self):
        values = (self.bid, self.ask, self.bid_size, self.ask_size)
        if not all(pd.notna(x) and float(x) > 0 for x in values):
            raise ValueError("BBO prices and sizes must be positive")
        if self.bid > self.ask:
            raise ValueError("crossed BBO")

    def row(self) -> dict[str, Any]:
        row = asdict(self)
        row["native_bid_size"] = row["bid_size"]
        row["native_ask_size"] = row["ask_size"]
        row["exchange_ts"] = pd.Timestamp(self.exchange_ts)
        row["receive_ts"] = pd.Timestamp(self.receive_ts)
        return row


def normalize_bbo_schema(frame: pd.DataFrame) -> pd.DataFrame:
    """Read v1/v2 parts together; untraceable legacy capacity always fails closed."""
    result = frame.copy()
    aliases = {"native_bid_size":"bid_size", "native_ask_size":"ask_size"}
    for target, source in aliases.items():
        if target not in result: result[target] = result[source] if source in result else np.nan
    defaults = {"schema_version":1, "metadata_snapshot_id":"",
        "native_size_unit":"UNKNOWN", "contract_multiplier":np.nan,
        "normalized_underlying_bid_qty":np.nan, "normalized_underlying_ask_qty":np.nan,
        "bid_notional_usd":np.nan, "ask_notional_usd":np.nan,
        "size_unit_status":SIZE_UNIT_UNKNOWN, "capacity_status":CAPACITY_UNKNOWN,
        "capacity_error_reason":"LEGACY_SCHEMA_MISSING_TRACEABLE_METADATA"}
    for column, value in defaults.items():
        if column not in result: result[column] = value
    version = pd.to_numeric(result.schema_version, errors="coerce").fillna(1)
    traceable = result.metadata_snapshot_id.fillna("").astype(str).str.len().gt(0)
    finite = pd.to_numeric(result.bid_notional_usd, errors="coerce").notna() & pd.to_numeric(
        result.ask_notional_usd, errors="coerce").notna()
    valid = version.ge(SCHEMA_VERSION) & traceable & finite & result.size_unit_status.eq(SIZE_UNIT_OK)
    result.loc[valid, "capacity_status"] = CAPACITY_VALID
    result.loc[valid, "capacity_error_reason"] = ""
    result.loc[~valid, "capacity_status"] = CAPACITY_UNKNOWN
    result.loc[~valid & result.size_unit_status.eq(SIZE_UNIT_UNKNOWN), "capacity_error_reason"] = SIZE_UNIT_UNKNOWN
    return result


def read_bbo(root: Path = BBO_ROOT, start: Any = None, end: Any = None) -> pd.DataFrame:
    files = sorted(Path(root).glob("date=*/exchange=*/*.parquet"))
    if not files: return normalize_bbo_schema(pd.DataFrame())
    frame = normalize_bbo_schema(pd.concat((pd.read_parquet(x) for x in files), ignore_index=True))
    if "receive_ts" in frame:
        frame["receive_ts"] = pd.to_datetime(frame.receive_ts, utc=True)
        if start is not None: frame = frame[frame.receive_ts >= pd.Timestamp(start)]
        if end is not None: frame = frame[frame.receive_ts < pd.Timestamp(end)]
    return frame


def _make(exchange: str, symbol: str, bid, ask, bid_size, ask_size, exchange_ts,
          native_sequence, receive_ts: datetime, local_sequence: int,
          connection_id: str, metadata: ProductMetadata | None = None) -> BBO:
    native = native_sequence not in (None, "")
    bid, ask, bid_size, ask_size = map(float, (bid, ask, bid_size, ask_size))
    usable = metadata is not None and metadata.usable
    multiplier = metadata.contract_multiplier if usable else float("nan")
    bid_qty = bid_size * multiplier if usable else float("nan")
    ask_qty = ask_size * multiplier if usable else float("nan")
    sequence = int(native_sequence if native else local_sequence)
    capacity_status = CAPACITY_VALID if usable else CAPACITY_UNKNOWN
    reason = "" if usable else SIZE_UNIT_UNKNOWN
    return BBO(exchange, symbol, bid, ask, bid_size, ask_size,
        _ts(exchange_ts, receive_ts), receive_ts, sequence,
        "native" if native else "connection_local", connection_id,
        metadata.native_size_unit if metadata else "UNKNOWN", multiplier,
        bid_qty, ask_qty, bid_qty * bid, ask_qty * ask,
        metadata.size_unit_status if metadata else SIZE_UNIT_UNKNOWN,
        f"{connection_id}:{sequence}", SCHEMA_VERSION,
        metadata.metadata_snapshot_id if metadata else "", capacity_status, reason)


def parse_binance(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
                   connection_id: str, metadata: ProductMetadata | None = None) -> BBO | None:
    if message.get("e") != "bookTicker":
        return None
    return _make("binance", symbol, message["b"], message["a"], message["B"], message["A"],
        message.get("E") or message.get("T"), message.get("u"), receive_ts, local_sequence, connection_id, metadata)


def parse_bitget(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
                 connection_id: str, metadata: ProductMetadata | None = None) -> BBO | None:
    data = message.get("data") or []
    if not data or message.get("arg", {}).get("channel") != "ticker":
        return None
    item = data[0]
    return _make("bitget", symbol, item["bidPr"], item["askPr"], item["bidSz"], item["askSz"],
        item.get("ts") or message.get("ts"), None, receive_ts, local_sequence, connection_id, metadata)


def parse_gate(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
               connection_id: str, metadata: ProductMetadata | None = None) -> BBO | None:
    if message.get("channel") != "futures.book_ticker" or message.get("event") != "update":
        return None
    item = message.get("result") or {}
    return _make("gate", symbol, item["b"], item["a"], item["B"], item["A"],
        item.get("t") or message.get("time_ms") or message.get("time"),
        item.get("u") or item.get("id"), receive_ts, local_sequence, connection_id, metadata)


def parse_hyperliquid(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
                      connection_id: str, metadata: ProductMetadata | None = None) -> BBO | None:
    if message.get("channel") != "l2Book":
        return None
    item = message.get("data") or {}
    levels = item.get("levels") or [[], []]
    if len(levels) < 2 or not levels[0] or not levels[1]:
        return None
    bid, ask = levels[0][0], levels[1][0]
    return _make("hyperliquid", symbol, bid["px"], ask["px"], bid["sz"], ask["sz"],
        item.get("time"), None, receive_ts, local_sequence, connection_id, metadata)


def parse_okx(message: dict, symbol: str, receive_ts: datetime, local_sequence: int,
              connection_id: str, metadata: ProductMetadata | None = None) -> BBO | None:
    if message.get("arg", {}).get("channel") != "books5" or not message.get("data"):
        return None
    item = message["data"][0]
    if not item.get("bids") or not item.get("asks"):
        return None
    bid, ask = item["bids"][0], item["asks"][0]
    return _make("okx", symbol, bid[0], ask[0], bid[1], ask[1], item.get("ts"),
        item.get("seqId"), receive_ts, local_sequence, connection_id, metadata)


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
    def __init__(self, flush_seconds=5, batch_rows=500, raw_root=RAW_ROOT, bbo_root=BBO_ROOT,
                 settings: dict | None = None):
        self.flush_seconds = float(flush_seconds)
        self.batch_rows = int(batch_rows)
        self.raw_root, self.bbo_root = Path(raw_root), Path(bbo_root)
        self.settings = settings or {}
        self.queue: asyncio.Queue[BBO | None] = asyncio.Queue()
        self.raw_queue: asyncio.Queue[tuple[Path, str] | None] = asyncio.Queue(maxsize=10000)
        self.raw_dropped_count = 0
        self.write_blocked_count = 0
        self.bytes_at_start = self._tree_bytes(self.raw_root) + self._tree_bytes(self.bbo_root)

    @staticmethod
    def _tree_bytes(root: Path) -> int:
        return sum(x.stat().st_size for x in Path(root).rglob("*") if x.is_file()) if Path(root).exists() else 0

    def disk_status(self) -> dict[str, Any]:
        anchor = self.bbo_root.parent if self.bbo_root.parent.exists() else ROOT
        usage = shutil.disk_usage(anchor)
        free_pct = usage.free / usage.total * 100 if usage.total else 0
        allowed = (usage.free >= float(self.settings.get("disk_min_free_gb", 1)) * 1024**3
            and free_pct >= float(self.settings.get("disk_min_free_percent", 5)))
        return {"disk_total_bytes":usage.total, "disk_free_bytes":usage.free,
            "disk_free_percent":free_pct, "write_status":"WRITABLE" if allowed else "DISK_WATERMARK_BLOCKED",
            "write_blocked_count":self.write_blocked_count,
            "growth_bytes":self._tree_bytes(self.raw_root)+self._tree_bytes(self.bbo_root)-self.bytes_at_start}

    def _write_allowed(self) -> bool:
        allowed = self.disk_status()["write_status"] == "WRITABLE"
        if not allowed: self.write_blocked_count += 1
        return allowed

    async def raw(self, exchange: str, receive_ts: datetime, connection_id: str, frame: str | bytes):
        if isinstance(frame, bytes):
            frame = frame.decode("utf-8", errors="replace")
        path = self.raw_root / f"exchange={exchange}" / f"date={receive_ts:%Y-%m-%d}" / "messages.ndjson.gz"
        record = json.dumps({"receive_ts":receive_ts.isoformat(), "connection_id":connection_id,
            "raw_message":frame}, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            self.raw_queue.put_nowait((path, record))
        except asyncio.QueueFull:
            self.raw_dropped_count += 1

    @staticmethod
    def _append(path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(path, "at", encoding="utf-8") as handle:
            handle.write(text)

    async def run_raw(self):
        """Dedicated batched writer; quote parsing never waits for disk I/O."""
        pending: list[tuple[Path, str]] = []
        while True:
            try:
                item = await asyncio.wait_for(self.raw_queue.get(), timeout=.25)
            except asyncio.TimeoutError:
                item = False
            if item is None:
                if pending: await asyncio.to_thread(self._flush_raw, pending)
                return
            if item is not False: pending.append(item)
            if pending and (item is False or len(pending) >= 100):
                batch, pending = pending, []
                await asyncio.to_thread(self._flush_raw, batch)

    def _flush_raw(self, records: list[tuple[Path, str]]):
        if not self._write_allowed(): return
        grouped: dict[Path, list[str]] = defaultdict(list)
        for path, text in records: grouped[path].append(text)
        for path, texts in grouped.items(): self._append(path, "".join(texts))

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
        if not self._write_allowed(): return
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
            combined.to_parquet(tmp, index=False, compression="zstd")
            os.replace(tmp, path)

    def maintenance(self, now: Any = None) -> dict[str, Any]:
        return apply_storage_controls(self.bbo_root, self.raw_root, self.settings, now=now)


def _protected_mask(frame: pd.DataFrame, exchange: str, windows_path: Path) -> pd.Series:
    mask = pd.Series(False, index=frame.index)
    if not windows_path.exists() or frame.empty: return mask
    windows = pd.read_parquet(windows_path)
    start_col = "window_start" if "window_start" in windows else "start"
    end_col = "window_end" if "window_end" in windows else "end"
    if start_col not in windows or end_col not in windows: return mask
    if "exchange" in windows: windows = windows[(windows.exchange == exchange) | windows.exchange.isna()]
    ts = pd.to_datetime(frame.receive_ts, utc=True)
    for row in windows.itertuples():
        start = pd.Timestamp(getattr(row, start_col)); end = pd.Timestamp(getattr(row, end_col))
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        mask |= ts.between(start, end, inclusive="both")
    return mask


def compact_daily_bbo(day_root: Path, downsample_ms: int | None = None,
                      protected_windows: Path | None = None) -> int:
    """Merge one UTC exchange partition; protected candidate windows stay full fidelity."""
    day_root = Path(day_root); files = sorted(day_root.glob("*.parquet"))
    if not files: return 0
    frame = normalize_bbo_schema(pd.concat((pd.read_parquet(x) for x in files), ignore_index=True))
    frame["receive_ts"] = pd.to_datetime(frame.receive_ts, utc=True)
    frame = frame.sort_values("receive_ts").drop_duplicates(
        ["exchange", "connection_id", "sequence", "receive_ts"], keep="last")
    if downsample_ms in {250, 1000} and len(frame):
        exchange = day_root.name.removeprefix("exchange=")
        protect = _protected_mask(frame, exchange, protected_windows or DATA_ROOT/"candidate_event_windows.parquet")
        reduced = frame[~protect].copy()
        reduced["_sample"] = reduced.receive_ts.dt.floor(f"{downsample_ms}ms")
        reduced = reduced.drop_duplicates(["exchange", "_sample"], keep="last").drop(columns="_sample")
        frame = pd.concat([frame[protect], reduced], ignore_index=True).sort_values("receive_ts")
    target = day_root / "bbo.parquet"; tmp = target.with_suffix(".parquet.tmp")
    frame.to_parquet(tmp, index=False, compression="zstd"); os.replace(tmp, target)
    for path in files:
        if path != target: path.unlink()
    return len(frame)


def apply_storage_controls(bbo_root: Path = BBO_ROOT, raw_root: Path = RAW_ROOT,
                           settings: dict | None = None, now: Any = None) -> dict[str, Any]:
    settings = settings or {}; now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    now = now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
    recent_days = int(settings.get("recent_full_retention_days", 2))
    downsample_ms = int(settings.get("historical_downsample_ms", 1000))
    compacted = 0; rows = 0
    for root in Path(bbo_root).glob("date=*/exchange=*"):
        day = pd.Timestamp(root.parent.name.removeprefix("date="), tz="UTC")
        sample = None if (now.normalize()-day).days < recent_days else downsample_ms
        rows += compact_daily_bbo(root, sample); compacted += 1
    raw_retention = int(settings.get("raw_retention_days", 7)); deleted = 0
    for root in Path(raw_root).glob("exchange=*/date=*"):
        day = pd.Timestamp(root.name.removeprefix("date="), tz="UTC")
        if (now.normalize()-day).days > raw_retention:
            shutil.rmtree(root); deleted += 1
    return {"compacted_partitions":compacted, "retained_rows":rows,
        "deleted_raw_partitions":deleted, "historical_downsample_ms":downsample_ms,
        "recent_full_retention_days":recent_days, "raw_retention_days":raw_retention}


class CollectorMonitor:
    def __init__(self, settings: dict, metadata: dict[str, ProductMetadata]):
        self.settings, self.metadata = settings, metadata
        self.counters = {x: defaultdict(int) for x in EXCHANGES}
        self.latencies = {x: deque(maxlen=100000) for x in EXCHANGES}
        self.connected = {x: False for x in EXCHANGES}
        self.connection_ids = {}

    async def on_status(self, exchange: str, connected: bool, connection_id: str):
        if connected:
            if self.counters[exchange]["connection_count"]:
                self.counters[exchange]["reconnect_count"] += 1
            self.connected[exchange] = True; self.connection_ids[exchange] = connection_id
            self.counters[exchange]["connection_count"] += 1
        elif self.connection_ids.get(exchange) == connection_id:
            self.connected[exchange] = False

    def on_message(self, exchange: str, message: dict | None = None, parse_error=False):
        self.counters[exchange]["message_count"] += 1
        if parse_error: self.counters[exchange]["parse_error_count"] += 1
        if not isinstance(message, dict): return
        if (message.get("event") == "subscribe" or message.get("op") == "subscribe"
                or message.get("channel") == "subscriptionResponse"
                or message.get("result") is None and "id" in message):
            self.counters[exchange]["subscription_ack_count"] += 1
        if (message.get("event") in {"ping", "pong"} or message.get("op") in {"ping", "pong"}
                or message.get("channel") in {"ping", "pong"}):
            self.counters[exchange]["heartbeat_count"] += 1

    async def on_quote(self, quote: BBO):
        c = self.counters[quote.exchange]; c["parsed_bbo_count"] += 1
        if quote.capacity_status == CAPACITY_VALID: c["capacity_valid_count"] += 1
        latency = (quote.receive_ts - quote.exchange_ts).total_seconds() * 1000
        if np.isfinite(latency): self.latencies[quote.exchange].append(latency)
        stale = float(self.settings.get("stale_after_ms", 3000))
        if latency > stale: c["stale_count"] += 1

    def snapshot(self) -> pd.DataFrame:
        rows = []
        for exchange in EXCHANGES:
            c = self.counters[exchange]; lat = np.asarray(self.latencies[exchange], dtype=float)
            parsed = int(c["parsed_bbo_count"])
            rows.append({
                "exchange": exchange, "message_count": int(c["message_count"]),
                "parsed_bbo_count": parsed, "parse_error_count": int(c["parse_error_count"]),
                "reconnect_count": int(c["reconnect_count"]),
                "subscription_ack_count": int(c["subscription_ack_count"]),
                "heartbeat_count": int(c["heartbeat_count"]),
                "median_receive_latency_ms": float(np.median(lat)) if len(lat) else np.nan,
                "p99_receive_latency_ms": float(np.quantile(lat,.99)) if len(lat) else np.nan,
                "stale_ratio": c["stale_count"] / parsed if parsed else np.nan,
                "capacity_valid_ratio": c["capacity_valid_count"] / parsed if parsed else np.nan,
                "size_unit_status": self.metadata[exchange].size_unit_status,
                "capacity_status": CAPACITY_VALID if self.metadata[exchange].usable else CAPACITY_UNKNOWN,
                "native_size_unit": self.metadata[exchange].native_size_unit,
                "contract_multiplier": self.metadata[exchange].contract_multiplier,
                "metadata_snapshot_id":self.metadata[exchange].metadata_snapshot_id,
                "metadata_age_seconds":max(0., (utcnow()-self.metadata[exchange].effective_observed_at).total_seconds()),
                "connected_at_shutdown": self.connected[exchange],
            })
        return pd.DataFrame(rows)

    def save(self, root: Path = HEALTH_ROOT) -> Path:
        root = Path(root); root.mkdir(parents=True, exist_ok=True)
        frame = self.snapshot(); path = root / "latest.csv"; frame.to_csv(path, index=False)
        return path


class WebSocketVenue:
    def __init__(self, adapter: VenueAdapter, symbol: str, storage: BBOStorage,
                 metadata: ProductMetadata | dict[str, ProductMetadata], monitor: CollectorMonitor, settings: dict):
        self.adapter, self.symbol, self.storage = adapter, symbol, storage
        self.metadata_registry = (metadata if isinstance(metadata, dict)
                                  else {adapter.exchange:metadata})
        self.monitor, self.settings = monitor, settings

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
                    await self.monitor.on_status(self.adapter.exchange, True, connection_id)
                    delay = float(self.settings.get("reconnect_min_seconds", 1)); sequence = 0
                    async for frame in ws:
                        received = utcnow(); sequence += 1
                        await self.storage.raw(self.adapter.exchange, received, connection_id, frame)
                        if frame in ("pong", b"pong"):
                            self.monitor.on_message(self.adapter.exchange, {"event":"pong"}); continue
                        try: message = json.loads(frame)
                        except (json.JSONDecodeError, TypeError):
                            self.monitor.on_message(self.adapter.exchange, parse_error=True); continue
                        self.monitor.on_message(self.adapter.exchange, message)
                        try:
                            quote = self.adapter.parser(message, self.symbol, received, sequence,
                                connection_id, self.metadata_registry.get(self.adapter.exchange))
                        except (KeyError, IndexError, TypeError, ValueError):
                            self.monitor.counters[self.adapter.exchange]["parse_error_count"] += 1
                            continue
                        if quote is not None:
                            await self.storage.put(quote)
                            await self.monitor.on_quote(quote)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s websocket disconnected: %s", self.adapter.exchange, exc)
            finally:
                await self.monitor.on_status(self.adapter.exchange, False, connection_id)
            if not stop.is_set():
                await asyncio.sleep(delay + random.random() * min(delay, 1.0))
                delay = min(maximum, delay * 2)


async def run_live_bbo(duration_seconds: float | None = None):
    """Run five public collectors; no strategy or position state exists here."""
    cfg = load_config(); settings = cfg.get("live_bbo", {}); symbols = cfg["symbols"]
    metadata = await asyncio.to_thread(fetch_product_metadata, symbols)
    monitor = CollectorMonitor(settings, metadata)
    storage = BBOStorage(settings.get("parquet_flush_seconds", 5),
        settings.get("parquet_batch_rows", 500), settings=settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try: loop.add_signal_handler(sig, stop.set)
        except NotImplementedError: pass
    venues = [WebSocketVenue(spec, symbols[name], storage, metadata, monitor, settings)
              for name, spec in adapters(symbols).items()]
    tasks = [asyncio.create_task(storage.run(), name="bbo-storage"),
             asyncio.create_task(storage.run_raw(), name="bbo-raw-storage")]
    tasks += [asyncio.create_task(x.run(stop), name=f"bbo-{x.adapter.exchange}") for x in venues]
    tasks.append(asyncio.create_task(refresh_product_metadata(symbols, metadata, stop,
        float(settings.get("metadata_refresh_seconds", 3600))), name="bbo-metadata-refresh"))
    timer = (asyncio.create_task(asyncio.sleep(duration_seconds), name="collector-duration")
             if duration_seconds is not None else None)
    try:
        if timer is None: await stop.wait()
        else:
            await timer; stop.set()
    finally:
        for task in tasks[2:]: task.cancel()
        await asyncio.gather(*tasks[2:], return_exceptions=True)
        await storage.queue.put(None); await storage.raw_queue.put(None)
        await asyncio.gather(tasks[0], tasks[1])
        await asyncio.to_thread(storage.maintenance)
        path = monitor.save()
        print(monitor.snapshot().to_string(index=False)); print(f"health={path}")
    return monitor.snapshot()


def run(duration_seconds: float | None = None):
    return asyncio.run(run_live_bbo(duration_seconds))


async def refresh_product_metadata(symbols: dict[str, str], registry: dict[str, ProductMetadata],
                                   stop: asyncio.Event, interval_seconds: float = 3600):
    """Refresh the mutable registry; every quote records the snapshot effective then."""
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(60., interval_seconds))
        except asyncio.TimeoutError:
            refreshed = await asyncio.to_thread(fetch_product_metadata, symbols)
            registry.update(refreshed)
