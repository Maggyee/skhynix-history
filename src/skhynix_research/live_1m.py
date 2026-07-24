from __future__ import annotations

import csv
import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np
import pandas as pd

from .config import ROOT, load_config
from .download import ENDPOINTS, candle, ms
from .http import CachedHTTP, pdnow


EXCHANGES = ("binance", "bitget", "gate", "hyperliquid", "okx")
BAR = pd.Timedelta(minutes=1)
DATA_ROOT = ROOT / "data" / "live_1m"
PRICES_ROOT = DATA_ROOT / "prices"
FUNDING_ROOT = DATA_ROOT / "funding"
RUNS_PATH = DATA_ROOT / "collection_runs.csv"
MONITOR_PATH = DATA_ROOT / "monitor.csv"
FUNDING_MONITOR_PATH = DATA_ROOT / "funding_monitor.csv"
STATUS_PATH = DATA_ROOT / "status.json"
LOCK_PATH = DATA_ROOT / "collector.lock"
RUN_COLUMNS = [
    "cycle_id", "cycle_started_at", "cycle_finished_at", "exchange", "price_type",
    "requested_start", "requested_end_exclusive", "rows_received", "rows_stored",
    "success", "error",
]


def closed_minute_window(now=None, lookback_minutes=5):
    """Return [start, end) containing only completely closed UTC minute bars."""
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    else:
        now = now.tz_convert("UTC")
    end = now.floor("min")
    return end - pd.Timedelta(minutes=lookback_minutes), end


def _native(row):
    row["close_time"] = pd.Timestamp(row["open_time"]) + BAR - pd.Timedelta(milliseconds=1)
    row["native_interval"] = "1m"
    row["interval_minutes"] = 1
    return row


def _capture(exchange, price_type, fetch):
    try:
        return fetch(), None
    except Exception as exc:  # one unavailable price type must not hide trade data
        logging.getLogger("live_1m").exception("%s %s collection failed", exchange, price_type)
        return [], str(exc)


def _binance(start, end, symbol):
    h = CachedHTTP("live_1m/binance", archive_ndjson=True); output = {}
    for typ, ep, key in [("trade", "/fapi/v1/klines", "symbol"), ("mark", "/fapi/v1/markPriceKlines", "symbol"), ("index", "/fapi/v1/indexPriceKlines", "pair")]:
        def fetch(ep=ep, key=key, typ=typ):
            data, raw = h.get(ENDPOINTS["binance"] + ep, {key:symbol, "interval":"1m", "startTime":ms(start), "endTime":ms(end)-1, "limit":1000})
            return [_native(candle("binance", symbol, typ, [*x[:6], x[7] if len(x)>7 else None, x[6]], ep, raw, "array")) for x in data]
        output[typ] = _capture("binance", typ, fetch)
    return output


def _bitget(start, end, symbol):
    h = CachedHTTP("live_1m/bitget", archive_ndjson=True); output = {}
    for typ, ptype in [("trade", "market"), ("mark", "mark"), ("index", "index")]:
        def fetch(typ=typ, ptype=ptype):
            ep = "/api/v3/market/history-candles"
            data, raw = h.get(ENDPOINTS["bitget"] + ep, {"category":"USDT-FUTURES", "symbol":symbol, "interval":"1m", "startTime":ms(start), "endTime":ms(end)-1, "limit":100, "type":ptype})
            rows = data.get("data", [])
            return [_native(candle("bitget", symbol, typ, [*x[:7], int(x[0])+59_999], ep, raw, "array")) for x in rows]
        output[typ] = _capture("bitget", typ, fetch)
    return output


def _gate(start, end, symbol):
    h = CachedHTTP("live_1m/gate", archive_ndjson=True); output = {}; ep = "/futures/usdt/candlesticks"
    for typ, prefix in [("trade", ""), ("mark", "mark_"), ("index", "index_")]:
        def fetch(typ=typ, prefix=prefix):
            data, raw = h.get(ENDPOINTS["gate"] + ep, {"contract":prefix+symbol, "from":int(start.timestamp()), "to":int(end.timestamp())-1, "interval":"1m"})
            return [_native(candle("gate", symbol, typ, x, ep, raw, "dict")) for x in data if isinstance(data, list)]
        output[typ] = _capture("gate", typ, fetch)
    return output


def _hyperliquid(start, end, symbol):
    h = CachedHTTP("live_1m/hyperliquid", archive_ndjson=True); ep = ENDPOINTS["hyperliquid"]
    def fetch():
        data, raw = h.post(ep, {"type":"candleSnapshot", "req":{"coin":symbol, "interval":"1m", "startTime":ms(start), "endTime":ms(end)-1}})
        return [_native(candle("hyperliquid", symbol, "trade", x, "POST /info candleSnapshot", raw, "dict")) for x in data]
    return {"trade": _capture("hyperliquid", "trade", fetch)}


def _okx(start, end, symbol):
    h = CachedHTTP("live_1m/okx", archive_ndjson=True); output = {}
    specs = [("trade", "/api/v5/market/history-candles", symbol), ("mark", "/api/v5/market/history-mark-price-candles", symbol), ("index", "/api/v5/market/history-index-candles", symbol.replace("-SWAP", ""))]
    for typ, ep, inst in specs:
        def fetch(typ=typ, ep=ep, inst=inst):
            data, raw = h.get(ENDPOINTS["okx"] + ep, {"instId":inst, "bar":"1m", "after":str(ms(end)), "before":str(ms(start)-1), "limit":100})
            rows = []
            for x in data.get("data", []):
                t = int(x[0]); confirmed = not (len(x) >= 9 and str(x[-1]) == "0")
                if ms(start) <= t < ms(end) and confirmed:
                    v = x[5] if typ == "trade" and len(x)>5 else None
                    q = x[7] if typ == "trade" and len(x)>7 else None
                    rows.append(_native(candle("okx", symbol, typ, [x[0],x[1],x[2],x[3],x[4],v,q,t+59_999], ep, raw, "array")))
            return rows
        output[typ] = _capture("okx", typ, fetch)
    return output


DOWNLOADERS = {"binance":_binance, "bitget":_bitget, "gate":_gate, "hyperliquid":_hyperliquid, "okx":_okx}


def _funding_row(exchange, symbol, funding_time, funding_rate, endpoint, raw_file):
    unit = "ms" if int(funding_time) > 10**12 else "s"
    return {"exchange":exchange, "symbol":symbol,
        "funding_time":pd.to_datetime(int(funding_time),unit=unit,utc=True),
        "funding_rate":float(funding_rate), "settlement_status":"realized",
        "source_endpoint":endpoint, "retrieved_at":pdnow(), "raw_file":raw_file}


def _funding_binance(start, end, symbol):
    h=CachedHTTP("live_1m/binance",archive_ndjson=True);ep="/fapi/v1/fundingRate"
    data,raw=h.get(ENDPOINTS["binance"]+ep,{"symbol":symbol,"startTime":ms(start),"endTime":ms(end)-1,"limit":1000})
    return [_funding_row("binance",symbol,x["fundingTime"],x["fundingRate"],ep,raw) for x in data]


def _funding_bitget(start, end, symbol):
    h=CachedHTTP("live_1m/bitget",archive_ndjson=True);ep="/api/v3/market/history-fund-rate"
    data,raw=h.get(ENDPOINTS["bitget"]+ep,{"category":"USDT-FUTURES","symbol":symbol,"limit":100})
    rows=data.get("data",{}).get("resultList",[])
    return [_funding_row("bitget",symbol,x["fundingRateTimestamp"],x["fundingRate"],ep,raw) for x in rows]


def _funding_gate(start, end, symbol):
    h=CachedHTTP("live_1m/gate",archive_ndjson=True);ep="/futures/usdt/funding_rate"
    data,raw=h.get(ENDPOINTS["gate"]+ep,{"contract":symbol,"from":int(start.timestamp()),"to":int(end.timestamp())-1,"limit":1000})
    return [_funding_row("gate",symbol,x["t"],x["r"],ep,raw) for x in data]


def _funding_hyperliquid(start, end, symbol):
    h=CachedHTTP("live_1m/hyperliquid",archive_ndjson=True);ep=ENDPOINTS["hyperliquid"]
    data,raw=h.post(ep,{"type":"fundingHistory","coin":symbol,"startTime":ms(start),"endTime":ms(end)-1})
    return [_funding_row("hyperliquid",symbol,x["time"],x["fundingRate"],"POST /info fundingHistory",raw) for x in data]


def _funding_okx(start, end, symbol):
    h=CachedHTTP("live_1m/okx",archive_ndjson=True);ep="/api/v5/public/funding-rate-history"
    data,raw=h.get(ENDPOINTS["okx"]+ep,{"instId":symbol,"limit":100})
    return [_funding_row("okx",symbol,x["fundingTime"],x.get("realizedRate") or x["fundingRate"],ep,raw) for x in data.get("data",[])]


FUNDING_DOWNLOADERS={"binance":_funding_binance,"bitget":_funding_bitget,"gate":_funding_gate,
    "hyperliquid":_funding_hyperliquid,"okx":_funding_okx}


def _partition_path(exchange, day):
    return PRICES_ROOT / f"date={day}" / f"exchange={exchange}" / "prices.parquet"


def _funding_partition_path(exchange, day):
    return FUNDING_ROOT / f"date={day}" / f"exchange={exchange}" / "funding.parquet"


def upsert_prices(rows):
    """Idempotently update only affected daily Parquet partitions."""
    incoming = pd.DataFrame(rows)
    if incoming.empty:
        return 0
    incoming["open_time"] = pd.to_datetime(incoming.open_time, utc=True)
    incoming["close_time"] = pd.to_datetime(incoming.close_time, utc=True)
    incoming["date"] = incoming.open_time.dt.strftime("%Y-%m-%d")
    stored = 0
    for (day, exchange), group in incoming.groupby(["date", "exchange"]):
        path = _partition_path(exchange, day); path.parent.mkdir(parents=True, exist_ok=True)
        group = group.drop(columns="date")
        old = pd.read_parquet(path) if path.exists() else pd.DataFrame()
        combined = pd.concat([old, group], ignore_index=True)
        combined = combined.sort_values(["price_type", "open_time", "retrieved_at"]).drop_duplicates(["exchange", "symbol", "price_type", "open_time"], keep="last")
        tmp = path.with_suffix(".parquet.tmp")
        combined.to_parquet(tmp, index=False)
        os.replace(tmp, path)
        stored += len(group)
    return stored


def read_prices(start=None):
    files = sorted(PRICES_ROOT.glob("date=*/exchange=*/prices.parquet"))
    if start is not None:
        first_day = pd.Timestamp(start).strftime("%Y-%m-%d")
        files = [path for path in files if path.parent.parent.name.removeprefix("date=") >= first_day]
    if not files:
        return pd.DataFrame()
    out = pd.concat([pd.read_parquet(path) for path in files], ignore_index=True)
    if start is not None:
        out = out[pd.to_datetime(out.open_time, utc=True) >= pd.Timestamp(start)]
    return out


def upsert_funding(rows):
    incoming=pd.DataFrame(rows)
    if incoming.empty:return 0
    incoming["funding_time"]=pd.to_datetime(incoming.funding_time,utc=True)
    incoming["date"]=incoming.funding_time.dt.strftime("%Y-%m-%d")
    stored=0
    for (day,exchange),group in incoming.groupby(["date","exchange"]):
        path=_funding_partition_path(exchange,day);path.parent.mkdir(parents=True,exist_ok=True)
        group=group.drop(columns="date");old=pd.read_parquet(path) if path.exists() else pd.DataFrame()
        combined=pd.concat([old,group],ignore_index=True)
        combined=combined.sort_values(["funding_time","retrieved_at"]).drop_duplicates(["exchange","symbol","funding_time"],keep="last")
        tmp=path.with_suffix(".parquet.tmp");combined.to_parquet(tmp,index=False);os.replace(tmp,path);stored+=len(group)
    return stored


def read_funding(start=None):
    files=sorted(FUNDING_ROOT.glob("date=*/exchange=*/funding.parquet"))
    if start is not None:
        first_day=pd.Timestamp(start).strftime("%Y-%m-%d")
        files=[path for path in files if path.parent.parent.name.removeprefix("date=")>=first_day]
    if not files:return pd.DataFrame()
    out=pd.concat([pd.read_parquet(path) for path in files],ignore_index=True)
    if start is not None:out=out[pd.to_datetime(out.funding_time,utc=True)>=pd.Timestamp(start)]
    return out


def _append_runs(rows):
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    exists = RUNS_PATH.exists()
    with RUNS_PATH.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RUN_COLUMNS)
        if not exists: writer.writeheader()
        writer.writerows([{k: row.get(k) for k in RUN_COLUMNS} for row in rows])


def build_monitor(now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None: now = now.tz_localize("UTC")
    # Exchanges publish a just-closed bar asynchronously. During the first 20
    # seconds, assess the preceding bar so an in-progress cycle cannot create a
    # false alert; the five-exchange cycle itself normally completes in <10s.
    assessment_end = now.floor("min") if now.second >= 20 else now.floor("min") - BAR
    latest_expected = assessment_end - BAR
    monitor_start = now.floor("min") - pd.Timedelta(hours=24)
    prices = read_prices(monitor_start); rows = []
    runs = pd.read_csv(RUNS_PATH) if RUNS_PATH.exists() else pd.DataFrame()
    for exchange in EXCHANGES:
        types = ["trade"] if exchange == "hyperliquid" else ["trade", "mark", "index"]
        for typ in types:
            group = prices[(prices.exchange==exchange)&(prices.price_type==typ)].copy() if len(prices) else pd.DataFrame()
            first = pd.to_datetime(group.open_time, utc=True).min() if len(group) else pd.NaT
            last = pd.to_datetime(group.open_time, utc=True).max() if len(group) else pd.NaT
            expected = int((last-first)/BAR)+1 if pd.notna(first) and pd.notna(last) else 0
            missing = max(0, expected-len(group))
            recent = runs[(runs.exchange==exchange)&(runs.price_type==typ)].tail(1) if len(runs) else pd.DataFrame()
            value = recent.iloc[0].success if len(recent) else False
            last_success = value if isinstance(value, (bool, np.bool_)) else str(value).lower() == "true"
            lag = (latest_expected-last)/BAR if pd.notna(last) else np.nan
            healthy = last_success and pd.notna(last) and lag <= 1 and missing == 0
            rows.append({"exchange":exchange, "price_type":typ, "monitor_window_start":monitor_start, "first_open_time":first, "last_open_time":last,
                "row_count":len(group), "expected_count":expected, "coverage_pct":100*len(group)/expected if expected else 0,
                "missing_bar_count":missing, "latest_expected_open_time":latest_expected, "latest_lag_minutes":float(lag) if pd.notna(lag) else np.nan,
                "last_cycle_success":last_success, "status":"HEALTHY" if healthy else "CHECK"})
    monitor = pd.DataFrame(rows)
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    monitor_tmp = MONITOR_PATH.with_suffix(".csv.tmp"); monitor.to_csv(monitor_tmp, index=False); os.replace(monitor_tmp, MONITOR_PATH)
    trade = monitor[monitor.price_type=="trade"]
    funding=read_funding();funding_rows=[]
    for exchange in EXCHANGES:
        group=funding[funding.exchange==exchange].copy() if len(funding) else pd.DataFrame()
        recent=runs[(runs.exchange==exchange)&(runs.price_type=="funding_event")].tail(1) if len(runs) else pd.DataFrame()
        value=recent.iloc[0].success if len(recent) else False
        success=value if isinstance(value,(bool,np.bool_)) else str(value).lower()=="true"
        last_event=pd.to_datetime(group.funding_time,utc=True).max() if len(group) else pd.NaT
        funding_rows.append({"exchange":exchange,"first_funding_time":pd.to_datetime(group.funding_time,utc=True).min() if len(group) else pd.NaT,
            "last_funding_time":last_event,"event_count":len(group),"last_poll_at":recent.iloc[0].cycle_finished_at if len(recent) else None,
            "last_poll_success":success,"status":"HEALTHY" if success else "CHECK"})
    funding_monitor=pd.DataFrame(funding_rows)
    funding_tmp=FUNDING_MONITOR_PATH.with_suffix(".csv.tmp");funding_monitor.to_csv(funding_tmp,index=False);os.replace(funding_tmp,FUNDING_MONITOR_PATH)
    funding_healthy=int(funding_monitor.status.eq("HEALTHY").sum())
    def display_path(path):
        try: return str(path.relative_to(ROOT))
        except ValueError: return str(path)
    status = {"updated_at":pdnow(), "healthy":bool(len(trade)==len(EXCHANGES) and trade.status.eq("HEALTHY").all() and funding_healthy==len(EXCHANGES)),
        "healthy_trade_exchanges":int(trade.status.eq("HEALTHY").sum()), "expected_trade_exchanges":len(EXCHANGES),
        "healthy_funding_exchanges":funding_healthy,"expected_funding_exchanges":len(EXCHANGES),
        "monitor_file":display_path(MONITOR_PATH),"funding_monitor_file":display_path(FUNDING_MONITOR_PATH),
        "dataset":display_path(PRICES_ROOT),"funding_dataset":display_path(FUNDING_ROOT)}
    status_tmp = STATUS_PATH.with_suffix(".json.tmp"); status_tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2)); os.replace(status_tmp, STATUS_PATH)
    return monitor, status


@contextmanager
def collector_lock():
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another live 1m collector is already running") from exc
        handle.write(str(os.getpid())); handle.flush()
        yield


def _collect_once(lookback_minutes=5, now=None, funding_lookback_hours=24):
    cycle_started = pd.Timestamp.now(tz="UTC"); cycle_id = cycle_started.isoformat()
    start, end = closed_minute_window(now or cycle_started, lookback_minutes)
    symbols = load_config()["symbols"]; run_rows = []
    for exchange in EXCHANGES:
        results = DOWNLOADERS[exchange](start, end, symbols[exchange])
        for typ, (rows, error) in results.items():
            valid = [row for row in rows if start <= pd.Timestamp(row["open_time"]) < end]
            stored = upsert_prices(valid)
            run_rows.append({"cycle_id":cycle_id, "cycle_started_at":cycle_started.isoformat(), "cycle_finished_at":pdnow(),
                "exchange":exchange, "price_type":typ, "requested_start":start.isoformat(), "requested_end_exclusive":end.isoformat(),
                "rows_received":len(valid), "rows_stored":stored, "success":error is None and len(valid)>0, "error":error or ("no closed bars returned" if not valid else "")})
        funding_start=end-pd.Timedelta(hours=funding_lookback_hours)
        funding_rows,error=_capture(exchange,"funding_event",lambda exchange=exchange:FUNDING_DOWNLOADERS[exchange](funding_start,end,symbols[exchange]))
        valid_funding=[row for row in funding_rows if funding_start<=pd.Timestamp(row["funding_time"])<end]
        funding_stored=upsert_funding(valid_funding)
        run_rows.append({"cycle_id":cycle_id,"cycle_started_at":cycle_started.isoformat(),"cycle_finished_at":pdnow(),
            "exchange":exchange,"price_type":"funding_event","requested_start":funding_start.isoformat(),"requested_end_exclusive":end.isoformat(),
            "rows_received":len(valid_funding),"rows_stored":funding_stored,"success":error is None,"error":error or ""})
    _append_runs(run_rows)
    monitor, status = build_monitor(now or cycle_started)
    return pd.DataFrame(run_rows), monitor, status


def collect_once(lookback_minutes=5, now=None, funding_lookback_hours=24):
    with collector_lock():
        return _collect_once(lookback_minutes, now, funding_lookback_hours)


def run_forever(lookback_minutes=5, poll_seconds=60, grace_seconds=8, funding_lookback_hours=24):
    log = logging.getLogger("live_1m")
    with collector_lock():
        while True:
            try:
                runs, _, status = _collect_once(lookback_minutes,funding_lookback_hours=funding_lookback_hours)
                log.info("cycle rows=%d trade_health=%d/%d funding_health=%d/%d",int(runs.rows_received.sum()),status["healthy_trade_exchanges"],len(EXCHANGES),status["healthy_funding_exchanges"],len(EXCHANGES))
            except Exception:
                log.exception("collector cycle failed")
            now = time.time()
            next_boundary = (int(now // poll_seconds) + 1) * poll_seconds + grace_seconds
            time.sleep(max(1, next_boundary-now))
