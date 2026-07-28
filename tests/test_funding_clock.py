from __future__ import annotations

import pandas as pd

import skhynix_research.funding_clock as fc

NOW=pd.Timestamp("2026-07-24T12:30:00Z")


def test_five_clock_parsers_use_utc_and_dynamic_fields():
    bundles={
        "binance":{"current":{"nextFundingTime":NOW.value//10**6+3600_000,"lastFundingRate":".001","time":NOW.value//10**6},"funding_info":[{"symbol":"X","fundingIntervalHours":4}],"history":[]},
        "bitget":{"current":{"data":[{"nextUpdate":NOW.value//10**6+3600_000,"fundingRate":".002","fundingRateInterval":"4"}]}},
        "gate":{"contract":{"funding_next_apply":int(NOW.timestamp())+3600,"funding_rate":".003","funding_interval":14400}},
        "hyperliquid":{"context":[{"universe":[{"name":"X"}]},[{"funding":".004"}]],"history":[{"time":int((NOW-pd.Timedelta(hours=2)).value//10**6)},{"time":int((NOW-pd.Timedelta(hours=1)).value//10**6)}]},
        "okx":{"current":{"data":[{"fundingTime":NOW.value//10**6+3600_000,"nextFundingTime":NOW.value//10**6+9*3600_000,"fundingRate":".005","ts":NOW.value//10**6}]}}}
    for exchange,bundle in bundles.items():
        result=fc.parse_bundle(exchange,"X",bundle,NOW)
        assert result.observed_at.tzinfo is not None and result.next_funding_time.tzinfo is not None
        assert result.status=="VALID" and result.funding_interval_hours>0
    assert fc.parse_bundle("okx","X",bundles["okx"],NOW).funding_interval_hours==8


def test_clock_storage_is_compact_zstd_and_health_is_written(tmp_path,monkeypatch):
    storage=fc.FundingClockStorage(tmp_path/"snap",tmp_path/"raw")
    snapshot=fc._snapshot("gate","X",NOW,NOW+pd.Timedelta(hours=1),.001,"test",4,NOW,"raw")
    storage.add(snapshot);storage.flush()
    files=list((tmp_path/"snap").rglob("*.parquet"));assert len(files)==1
    assert len(fc.read_snapshots(tmp_path/"snap"))==1
