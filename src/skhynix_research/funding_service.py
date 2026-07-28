"""Idempotent public settled-funding poller for the paper engine."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .config import ROOT
from .live_1m import DOWNLOADERS, FUNDING_DOWNLOADERS
from .paper_trading import FundingEvent, PaperEngine, _utc

SERVICE_ROOT = ROOT / "data/paper_bbo/funding_service"


def funding_event_id(exchange: str, symbol: str, settled_at: Any) -> str:
    stamp = pd.Timestamp(_utc(settled_at)).isoformat()
    return hashlib.sha256(f"{exchange}|{symbol}|{stamp}".encode()).hexdigest()[:24]


def fetch_settled_funding(exchange: str, symbol: str, start: Any, end: Any) -> list[dict[str, Any]]:
    rows = FUNDING_DOWNLOADERS[exchange](pd.Timestamp(start), pd.Timestamp(end), symbol)
    return [{"exchange":exchange, "symbol":symbol,
        "settled_at":pd.Timestamp(x["funding_time"]), "rate":float(x["funding_rate"]),
        "source":x.get("source_endpoint","PUBLIC_SETTLED_FUNDING"),
        "raw_file":x.get("raw_file","")} for x in rows]


class FundingSettlementService:
    def __init__(self, engine: PaperEngine, symbols: dict[str, str], root: Path = SERVICE_ROOT,
                 poll_seconds: float = 60, backfill_hours: float = 48,
                 fetcher: Callable[..., list[dict[str, Any]]] = fetch_settled_funding,
                 mark_provider: Callable[[dict[str, Any]], float] | None = None,
                 now: Callable[[], Any] | None = None):
        self.engine=engine;self.symbols=symbols;self.root=Path(root)
        self.poll_seconds=float(poll_seconds);self.backfill_hours=float(backfill_hours)
        self.fetcher=fetcher;self.mark_provider=mark_provider or self._public_settlement_mark
        self.now=now or (lambda:pd.Timestamp.now(tz="UTC"))
        self.state_path=self.root/"state.json";self.health_path=self.root/"health.json"
        self.processed_ids:set[str]=set();self.cursors:dict[str,str]={};self.pending=[]
        self.poll_count=0;self.injected_count=0;self.duplicate_rejected_count=0
        self.session_injected_count=0;self.session_duplicate_rejected_count=0
        self.error_count=0;self.last_successful_poll_at=None;self.last_settlement_event_at=None
        self.last_errors:dict[str,str]={};self._load()

    def _load(self):
        if not self.state_path.exists():return
        state=json.loads(self.state_path.read_text())
        self.processed_ids=set(state.get("processed_event_ids",[]));self.cursors=state.get("cursors",{})
        self.injected_count=int(state.get("injected_count",0))
        self.duplicate_rejected_count=int(state.get("duplicate_rejected_count",0))
        self.last_successful_poll_at=state.get("last_successful_poll_at")
        self.last_settlement_event_at=state.get("last_settlement_event_at")

    def _engine_mark(self, row: dict[str,Any]) -> float:
        quote=self.engine.quotes.get(row["exchange"])
        settled=pd.Timestamp(_utc(row["settled_at"]))
        if quote is None:return np.nan
        age=abs((pd.Timestamp(quote.exchange_ts)-settled).total_seconds())
        return (quote.bid+quote.ask)/2 if age<=300 else np.nan

    def _public_settlement_mark(self, row: dict[str,Any]) -> float:
        """Use a contemporaneous BBO, then only already-closed public 1m bars."""
        current=self._engine_mark(row)
        if np.isfinite(current):return current
        settled=pd.Timestamp(_utc(row["settled_at"]));start=settled-pd.Timedelta(minutes=3)
        result=DOWNLOADERS[row["exchange"]](start,settled+pd.Timedelta(minutes=1),row["symbol"])
        for price_type in ("mark","trade"):
            rows,error=result.get(price_type,([],"unavailable"))
            if error or not rows:continue
            frame=pd.DataFrame(rows);frame["close_time"]=pd.to_datetime(frame.close_time,utc=True)
            causal=frame[frame.close_time<settled].sort_values("close_time")
            if len(causal):
                value=float(causal.close.iloc[-1])
                if np.isfinite(value) and value>0:return value
        return np.nan

    def _save_events(self):
        if not self.pending:return
        frame=pd.DataFrame(self.pending)
        frame["settled_at"]=pd.to_datetime(frame.settled_at,utc=True,format="mixed")
        frame["date"]=frame.settled_at.dt.strftime("%Y-%m-%d")
        for (day,exchange),group in frame.groupby(["date","exchange"],sort=False):
            root=self.root/"events"/f"date={day}"/f"exchange={exchange}";root.mkdir(parents=True,exist_ok=True)
            path=root/"events.parquet";old=pd.read_parquet(path) if path.exists() else pd.DataFrame()
            combined=pd.concat([old,group.drop(columns="date")],ignore_index=True)
            combined=combined.sort_values(["settled_at","retrieved_at"]).drop_duplicates("event_id",keep="last")
            tmp=path.with_suffix(".parquet.tmp");combined.to_parquet(tmp,index=False,compression="zstd");os.replace(tmp,path)
        self.pending=[]

    def save(self):
        self.root.mkdir(parents=True,exist_ok=True);self._save_events()
        payload={"updated_at":pd.Timestamp(self.now()).isoformat(),
            "processed_event_ids":sorted(self.processed_ids),"cursors":self.cursors,
            "poll_count":self.poll_count,"injected_count":self.injected_count,
            "duplicate_rejected_count":self.duplicate_rejected_count,"error_count":self.error_count,
            "last_successful_poll_at":self.last_successful_poll_at,
            "last_settlement_event_at":self.last_settlement_event_at,"last_errors":self.last_errors}
        tmp=self.state_path.with_suffix(".json.tmp");tmp.write_text(json.dumps(payload,indent=2));os.replace(tmp,self.state_path)
        health={**payload,"processed_event_ids":len(self.processed_ids),
            "status":"HEALTHY" if self.last_successful_poll_at and not self.last_errors else "DEGRADED"}
        tmp=self.health_path.with_suffix(".json.tmp");tmp.write_text(json.dumps(health,indent=2));os.replace(tmp,self.health_path)

    def _event(self, row: dict[str,Any]) -> FundingEvent:
        event_id=funding_event_id(row["exchange"],row["symbol"],row["settled_at"])
        relevant=self.engine.funding_event_relevant(row["exchange"],row["settled_at"])
        mark=float(row.get("mark_price",np.nan))
        if relevant and not np.isfinite(mark):mark=float(self.mark_provider(row))
        return FundingEvent(event_id,row["exchange"],pd.Timestamp(_utc(row["settled_at"])).isoformat(),
            float(row["rate"]),mark,row.get("source","PUBLIC_SETTLED_FUNDING"))

    def poll_once(self) -> dict[str,Any]:
        now=pd.Timestamp(self.now());now=now.tz_localize("UTC") if now.tzinfo is None else now.tz_convert("UTC")
        self.poll_count+=1;self.last_errors={};successful=0
        for exchange,symbol in self.symbols.items():
            default_start=now-pd.Timedelta(hours=self.backfill_hours)
            cursor=pd.Timestamp(self.cursors[exchange]) if exchange in self.cursors else default_start
            cursor=cursor.tz_localize("UTC") if cursor.tzinfo is None else cursor.tz_convert("UTC")
            start=max(default_start,cursor-pd.Timedelta(minutes=5))
            try:rows=self.fetcher(exchange,symbol,start,now+pd.Timedelta(seconds=1))
            except Exception as exc:
                self.error_count+=1;self.last_errors[exchange]=f"{type(exc).__name__}:{exc}";continue
            successful+=1
            for row in sorted(rows,key=lambda x:pd.Timestamp(x["settled_at"])):
                settled=pd.Timestamp(row["settled_at"]);settled=settled.tz_localize("UTC") if settled.tzinfo is None else settled.tz_convert("UTC")
                if settled<start or settled>now:continue
                event=self._event(row)
                if event.event_id in self.processed_ids:
                    self.duplicate_rejected_count+=1;self.session_duplicate_rejected_count+=1;continue
                try:accepted=self.engine.on_funding_event(event)
                except ValueError as exc:
                    self.error_count+=1;self.last_errors[exchange]=f"EVENT_REJECTED:{exc}";break
                self.processed_ids.add(event.event_id)
                if accepted:
                    self.injected_count+=1;self.session_injected_count+=1
                else:
                    self.duplicate_rejected_count+=1;self.session_duplicate_rejected_count+=1
                self.last_settlement_event_at=settled.isoformat()
                self.pending.append({**asdict(event),"symbol":symbol,"raw_file":row.get("raw_file",""),
                    "retrieved_at":now,"injection_status":"INJECTED" if accepted else "ENGINE_DUPLICATE"})
                self.cursors[exchange]=settled.isoformat()
        if successful==len(self.symbols):self.last_successful_poll_at=now.isoformat()
        self.save();return {"successful_exchanges":successful,"expected_exchanges":len(self.symbols),
            "injected_count":self.injected_count,"duplicate_rejected_count":self.duplicate_rejected_count,
            "session_injected_count":self.session_injected_count,
            "session_duplicate_rejected_count":self.session_duplicate_rejected_count,
            "last_settlement_event_at":self.last_settlement_event_at,"errors":self.last_errors}

    async def run(self, stop: asyncio.Event):
        while not stop.is_set():
            try:await asyncio.to_thread(self.poll_once)
            except Exception as exc:
                self.error_count+=1;self.last_errors["service"]=f"{type(exc).__name__}:{exc}"
                await asyncio.to_thread(self.save)
            try:await asyncio.wait_for(stop.wait(),timeout=self.poll_seconds)
            except asyncio.TimeoutError:pass

    async def shutdown(self):
        await asyncio.to_thread(self.save);self.engine.save()
