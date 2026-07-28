"""Public five-venue funding clock snapshots; no account or order interfaces."""
from __future__ import annotations

import gzip
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from .config import ROOT, load_config

EXCHANGES = ("binance", "bitget", "gate", "hyperliquid", "okx")
DATA_ROOT = ROOT / "data/live_funding_clock"
SNAPSHOT_ROOT = DATA_ROOT / "snapshots"
RAW_ROOT = ROOT / "data/raw/live_funding_clock"
HEALTH_ROOT = DATA_ROOT / "health"


def utc(value: Any = None) -> pd.Timestamp:
    value = pd.Timestamp.now(tz="UTC") if value is None else pd.Timestamp(value)
    return value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")


def epoch(value: Any) -> pd.Timestamp:
    if isinstance(value, (datetime, pd.Timestamp, np.datetime64)): return utc(value)
    number = int(float(value)); return pd.to_datetime(number, unit="ms" if abs(number)>10**11 else "s", utc=True)


def finite(value: Any) -> float:
    try:
        result = float(value); return result if np.isfinite(result) else np.nan
    except (TypeError, ValueError): return np.nan


def infer_interval(times: list[Any], before: Any) -> float:
    values = pd.Series([epoch(x) for x in times]).sort_values()
    values = values[values <= utc(before)]
    diffs = values.diff().dt.total_seconds().div(3600)
    diffs = diffs[diffs.between(.25, 24)]
    return float(diffs.tail(10).median()) if len(diffs) else np.nan


@dataclass(frozen=True)
class FundingClockSnapshot:
    exchange: str
    symbol: str
    observed_at: pd.Timestamp
    next_funding_time: pd.Timestamp | pd.NaT
    seconds_to_funding: float
    predicted_funding_rate: float
    predicted_rate_source: str
    funding_interval_hours: float
    source_event_time: pd.Timestamp | pd.NaT
    retrieved_at: pd.Timestamp
    status: str
    raw_file: str
    next_time_source: str = "PUBLIC_NATIVE_FIELD"


def _snapshot(exchange, symbol, observed_at, next_time, rate, rate_source, interval,
              event_time, raw_file, next_source="PUBLIC_NATIVE_FIELD"):
    observed = utc(observed_at); nxt = pd.NaT if next_time is None else utc(next_time)
    seconds = (nxt-observed).total_seconds() if pd.notna(nxt) else np.nan
    interval = finite(interval); rate = finite(rate)
    event_time = pd.NaT if event_time is None else utc(event_time)
    missing = []
    if pd.isna(nxt): missing.append("NEXT_TIME")
    if not np.isfinite(rate): missing.append("PREDICTION")
    if not np.isfinite(interval): missing.append("INTERVAL")
    status = "VALID" if not missing else "MISSING_"+"_AND_".join(missing)
    return FundingClockSnapshot(exchange, symbol, observed, nxt, seconds, rate,
        rate_source, interval, event_time, utc(), status, raw_file, next_source)


def parse_bundle(exchange: str, symbol: str, bundle: dict, observed_at: Any, raw_file=""):
    if exchange == "binance":
        current=bundle["current"]; history=bundle.get("history",[])
        info=next((x for x in bundle.get("funding_info",[]) if x.get("symbol")==symbol),{})
        interval=finite(info.get("fundingIntervalHours"))
        if not np.isfinite(interval): interval=infer_interval([x["fundingTime"] for x in history],observed_at)
        return _snapshot(exchange,symbol,observed_at,epoch(current["nextFundingTime"]),
            current.get("lastFundingRate"),"premiumIndex.lastFundingRate",interval,
            epoch(current.get("time",utc(observed_at).timestamp()*1000)),raw_file)
    if exchange == "bitget":
        current=(bundle["current"].get("data") or [])[0]
        return _snapshot(exchange,symbol,observed_at,epoch(current["nextUpdate"]),
            current.get("fundingRate"),"current-fund-rate.fundingRate",
            current.get("fundingRateInterval"),observed_at,raw_file)
    if exchange == "gate":
        current=bundle["contract"]
        return _snapshot(exchange,symbol,observed_at,epoch(current["funding_next_apply"]),
            current.get("funding_rate"),"contract.funding_rate",
            finite(current.get("funding_interval"))/3600,observed_at,raw_file)
    if exchange == "hyperliquid":
        meta,contexts=bundle["context"]
        index=next(i for i,x in enumerate(meta["universe"]) if x["name"]==symbol)
        times=[x["time"] for x in bundle.get("history",[])]; interval=infer_interval(times,observed_at)
        latest=max((epoch(x) for x in times if epoch(x)<=utc(observed_at)),default=None)
        nxt = latest + pd.Timedelta(hours=interval) if latest is not None and np.isfinite(interval) else None
        while nxt is not None and nxt <= utc(observed_at): nxt += pd.Timedelta(hours=interval)
        return _snapshot(exchange,symbol,observed_at,nxt,contexts[index].get("funding"),
            "metaAndAssetCtxs.assetCtx.funding",interval,latest,raw_file,
            "DERIVED_FROM_PUBLIC_SETTLED_HISTORY")
    if exchange == "okx":
        current=(bundle["current"].get("data") or [])[0]
        current_time=epoch(current["fundingTime"]); following=epoch(current["nextFundingTime"])
        return _snapshot(exchange,symbol,observed_at,current_time,current.get("fundingRate"),
            "funding-rate.fundingRate",(following-current_time).total_seconds()/3600,
            epoch(current.get("ts",current["fundingTime"])),raw_file)
    raise ValueError(exchange)


def fetch_bundle(client: httpx.Client, exchange: str, symbol: str, observed_at: Any):
    now_ms=int(utc(observed_at).timestamp()*1000)
    if exchange=="binance":
        base="https://fapi.binance.com"; return {
            "current":client.get(base+"/fapi/v1/premiumIndex",params={"symbol":symbol}).json(),
            "funding_info":client.get(base+"/fapi/v1/fundingInfo").json(),
            "history":client.get(base+"/fapi/v1/fundingRate",params={"symbol":symbol,"limit":20}).json()}
    if exchange=="bitget":
        base="https://api.bitget.com"; return {"current":client.get(
            base+"/api/v3/market/current-fund-rate",params={"category":"USDT-FUTURES","symbol":symbol}).json()}
    if exchange=="gate":
        return {"contract":client.get(f"https://api.gateio.ws/api/v4/futures/usdt/contracts/{symbol}").json()}
    if exchange=="hyperliquid":
        url="https://api.hyperliquid.xyz/info"; body={"type":"metaAndAssetCtxs"}
        if ":" in symbol: body["dex"]=symbol.split(":",1)[0]
        return {"context":client.post(url,json=body).json(),"history":client.post(url,json={
            "type":"fundingHistory","coin":symbol,"startTime":now_ms-48*3600*1000,"endTime":now_ms}).json()}
    if exchange=="okx":
        return {"current":client.get("https://www.okx.com/api/v5/public/funding-rate",params={"instId":symbol}).json()}
    raise ValueError(exchange)


class FundingClockStorage:
    def __init__(self, snapshot_root=SNAPSHOT_ROOT, raw_root=RAW_ROOT):
        self.snapshot_root=Path(snapshot_root); self.raw_root=Path(raw_root); self.rows=[]

    def raw(self, exchange, observed_at, payload):
        day=utc(observed_at).strftime("%Y-%m-%d")
        path=self.raw_root/f"exchange={exchange}"/f"date={day}"/"responses.ndjson.gz"
        path.parent.mkdir(parents=True,exist_ok=True)
        with gzip.open(path,"at",encoding="utf-8") as handle:
            handle.write(json.dumps({"observed_at":utc(observed_at).isoformat(),"response":payload},ensure_ascii=False)+"\n")
        return path

    def add(self, snapshot): self.rows.append(asdict(snapshot))

    def flush(self):
        if not self.rows:return
        frame=pd.DataFrame(self.rows);self.rows=[]
        frame["observed_at"]=pd.to_datetime(frame.observed_at,utc=True)
        frame["date"]=frame.observed_at.dt.strftime("%Y-%m-%d")
        for (day,exchange),group in frame.groupby(["date","exchange"],sort=False):
            root=self.snapshot_root/f"date={day}"/f"exchange={exchange}";root.mkdir(parents=True,exist_ok=True)
            path=root/"snapshots.parquet"; old=pd.read_parquet(path) if path.exists() else pd.DataFrame()
            combined=pd.concat([old,group.drop(columns="date")],ignore_index=True)
            combined=combined.sort_values(["observed_at","retrieved_at"]).drop_duplicates(
                ["exchange","symbol","observed_at"],keep="last")
            tmp=path.with_suffix(".parquet.tmp");combined.to_parquet(tmp,index=False,compression="zstd");os.replace(tmp,path)


def read_snapshots(root=SNAPSHOT_ROOT):
    files=sorted(Path(root).glob("date=*/exchange=*/snapshots.parquet"))
    return pd.concat((pd.read_parquet(x) for x in files),ignore_index=True) if files else pd.DataFrame()


def write_health(frame, root=HEALTH_ROOT):
    root=Path(root);root.mkdir(parents=True,exist_ok=True); rows=[]; stored=read_snapshots()
    for exchange in EXCHANGES:
        latest=frame[frame.exchange==exchange].tail(1) if len(frame) else pd.DataFrame()
        row=latest.iloc[0] if len(latest) else None
        rows.append({"exchange":exchange,"checked_at":utc(),"latest_observed_at":row.observed_at if row is not None else pd.NaT,
            "next_funding_time":row.next_funding_time if row is not None else pd.NaT,
            "snapshot_count":int((stored.exchange==exchange).sum()) if len(stored) else 0,
            "status":row.status if row is not None else "NO_SNAPSHOT"})
    health=pd.DataFrame(rows);health.to_csv(root/"latest.csv",index=False)
    status={"checked_at":utc().isoformat(),"healthy":bool(health.status.eq("VALID").all()),
        "valid_exchanges":int(health.status.eq("VALID").sum()),"expected_exchanges":5}
    (root/"status.json").write_text(json.dumps(status,indent=2));return health,status


def collect_once(storage=None,client=None,observed_at=None):
    observed=utc(observed_at);storage=storage or FundingClockStorage();own=client is None
    client=client or httpx.Client(timeout=25,headers={"User-Agent":"skhynix-public-research/0.1"})
    rows=[];symbols=load_config()["symbols"]
    try:
        for exchange in EXCHANGES:
            try:
                bundle=fetch_bundle(client,exchange,symbols[exchange],observed);raw=storage.raw(exchange,observed,bundle)
                relative=str(raw.relative_to(ROOT)) if raw.is_relative_to(ROOT) else str(raw)
                snapshot=parse_bundle(exchange,symbols[exchange],bundle,observed,relative)
            except Exception as exc:
                snapshot=_snapshot(exchange,symbols[exchange],observed,None,None,"UNAVAILABLE",None,None,"","UNAVAILABLE")
                snapshot=FundingClockSnapshot(**{**asdict(snapshot),"status":f"ERROR:{type(exc).__name__}:{exc}"})
            storage.add(snapshot);rows.append(asdict(snapshot))
    finally:
        if own:client.close()
    storage.flush();frame=pd.DataFrame(rows);write_health(frame);return frame


def monitor():
    frame=read_snapshots()
    latest=frame.sort_values("observed_at").groupby("exchange",as_index=False).tail(1) if len(frame) else frame
    return write_health(latest)
