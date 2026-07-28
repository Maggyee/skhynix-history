"""Unified public-data and paper-funding runtime health output."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pandas as pd

from .config import ROOT

REPORT_ROOT = ROOT / "reports_runtime_health"


def _funding_clock_status() -> dict[str,Any]:
    path=ROOT/"data/live_funding_clock/health/status.json"
    if not path.exists():return {"healthy":False,"valid_exchanges":0,"expected_exchanges":5}
    try:return json.loads(path.read_text())
    except (json.JSONDecodeError,OSError):return {"healthy":False,"valid_exchanges":0,"expected_exchanges":5}


def generate_runtime_health(collector, funding_service, storage, root: Path = REPORT_ROOT):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    frame=collector.snapshot().copy();clock=_funding_clock_status();disk=storage.disk_status()
    frame["funding_clock_healthy"]=bool(clock.get("healthy",False))
    frame["funding_clock_valid_exchanges"]=int(clock.get("valid_exchanges",0))
    frame["last_settlement_event_at"]=funding_service.last_settlement_event_at
    frame["paper_funding_injected_count"]=funding_service.injected_count
    frame["duplicate_event_rejected_count"]=funding_service.duplicate_rejected_count
    frame["session_funding_injected_count"]=funding_service.session_injected_count
    frame["session_duplicate_rejected_count"]=funding_service.session_duplicate_rejected_count
    for key,value in disk.items():frame[key]=value
    frame["checked_at"]=pd.Timestamp.now(tz="UTC")
    tmp=root/"latest.csv.tmp";frame.to_csv(tmp,index=False);os.replace(tmp,root/"latest.csv")
    payload={"checked_at":pd.Timestamp.now(tz="UTC").isoformat(),
        "bbo":frame.to_dict(orient="records"),"funding_clock":clock,
        "funding_service":{"status":"HEALTHY" if funding_service.last_successful_poll_at else "STARTING",
            "last_successful_poll_at":funding_service.last_successful_poll_at,
            "last_settlement_event_at":funding_service.last_settlement_event_at,
            "injected_count":funding_service.injected_count,
            "duplicate_event_rejected_count":funding_service.duplicate_rejected_count,
            "session_injected_count":funding_service.session_injected_count,
            "session_duplicate_rejected_count":funding_service.session_duplicate_rejected_count},"disk":disk}
    tmp=root/"latest.json.tmp";tmp.write_text(json.dumps(payload,indent=2,default=str));os.replace(tmp,root/"latest.json")
    lines=["# Runtime health","",f"Updated: {payload['checked_at']}","",
        f"- Funding clock healthy: {clock.get('healthy',False)} ({clock.get('valid_exchanges',0)}/5)",
        f"- Last settled funding event: {funding_service.last_settlement_event_at}",
        f"- Paper funding injections: {funding_service.injected_count}",
        f"- Duplicate funding events rejected: {funding_service.duplicate_rejected_count}",
        f"- This runtime: injected {funding_service.session_injected_count}; duplicates rejected {funding_service.session_duplicate_rejected_count}",
        f"- Disk/write status: {disk['write_status']}; free {disk['disk_free_percent']:.2f}%",
        f"- Runtime storage growth: {disk['growth_bytes']} bytes","",
        "|exchange|messages|median latency ms|stale ratio|capacity valid ratio|metadata age seconds|",
        "|---|---:|---:|---:|---:|---:|"]
    for row in frame.itertuples():
        lines.append(f"|{row.exchange}|{row.message_count}|{row.median_receive_latency_ms}|{row.stale_ratio}|{row.capacity_valid_ratio}|{row.metadata_age_seconds}|")
    tmp=root/"report.md.tmp";tmp.write_text("\n".join(lines)+"\n");os.replace(tmp,root/"report.md")
    return frame,payload


async def monitor_runtime(stop: asyncio.Event, collector, funding_service, storage,
                          interval_seconds: float = 30):
    while not stop.is_set():
        await asyncio.to_thread(generate_runtime_health,collector,funding_service,storage)
        try:await asyncio.wait_for(stop.wait(),timeout=max(5.,interval_seconds))
        except asyncio.TimeoutError:pass
