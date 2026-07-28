from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

import skhynix_research.live_bbo as lb
import skhynix_research.runtime_health as rh


def test_daily_compaction_downsamples_but_preserves_candidate_window(tmp_path):
    root=tmp_path/"date=2026-07-20"/"exchange=gate";root.mkdir(parents=True)
    times=pd.date_range("2026-07-20T00:00:00Z",periods=4,freq="100ms")
    frame=pd.DataFrame({"exchange":"gate","symbol":"X","bid":100.,"ask":101.,
        "bid_size":1.,"ask_size":1.,"exchange_ts":times,"receive_ts":times,
        "sequence":range(4),"connection_id":"c"})
    frame.iloc[:2].to_parquet(root/"part-a.parquet",index=False);frame.iloc[2:].to_parquet(root/"part-b.parquet",index=False)
    protected=tmp_path/"windows.parquet"
    pd.DataFrame({"exchange":["gate"],"window_start":[times[0]],"window_end":[times[1]]}).to_parquet(protected,index=False)
    rows=lb.compact_daily_bbo(root,1000,protected)
    result=pd.read_parquet(root/"bbo.parquet")
    assert rows==3 and len(result)==3 and set(times[:2])<=set(pd.to_datetime(result.receive_ts,utc=True))
    assert len(list(root.glob("*.parquet")))==1


def test_disk_watermark_blocks_writes(tmp_path):
    storage=lb.BBOStorage(raw_root=tmp_path/"raw",bbo_root=tmp_path/"bbo",
        settings={"disk_min_free_gb":10**9,"disk_min_free_percent":0})
    assert storage.disk_status()["write_status"]=="DISK_WATERMARK_BLOCKED"


def test_runtime_health_contains_required_dimensions(tmp_path,monkeypatch):
    metadata={x:lb.ProductMetadata(x,"X","BASE_ASSET",1,"X","USDT",lb.SIZE_UNIT_OK,"",lb.utcnow(),f"id-{x}") for x in lb.EXCHANGES}
    collector=lb.CollectorMonitor({},metadata)
    funding=SimpleNamespace(last_settlement_event_at=None,injected_count=2,
        duplicate_rejected_count=3,session_injected_count=1,session_duplicate_rejected_count=2,
        last_successful_poll_at="2026-07-24T00:00:00Z")
    storage=lb.BBOStorage(raw_root=tmp_path/"raw",bbo_root=tmp_path/"bbo")
    frame,payload=rh.generate_runtime_health(collector,funding,storage,tmp_path/"report")
    assert {"message_count","stale_ratio","capacity_valid_ratio","metadata_age_seconds",
        "funding_clock_healthy","paper_funding_injected_count","disk_free_bytes","write_status"}<=set(frame)
    assert payload["funding_service"]["injected_count"]==2
    assert (tmp_path/"report/report.md").exists() and json.loads((tmp_path/"report/latest.json").read_text())
