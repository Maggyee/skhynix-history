from __future__ import annotations

from pathlib import Path

import pandas as pd

import skhynix_research.bbo_capacity_backfill as audit


def _legacy(root: Path):
    path=root/"date=2026-07-24"/"exchange=gate"/"part-1200.parquet"
    path.parent.mkdir(parents=True)
    pd.DataFrame({"exchange":["gate"],"symbol":["X"],"bid":[100.],"ask":[101.],
        "bid_size":[10.],"ask_size":[10.],"receive_ts":["2026-07-24T12:00:00Z"],
        "exchange_ts":["2026-07-24T12:00:00Z"],"connection_id":["c"],"sequence":[1]}).to_parquet(path,index=False)


def _metadata(root: Path, second_multiplier=.01):
    path=root/"date=2026-07-24"/"exchange=gate"/"snapshots.parquet";path.parent.mkdir(parents=True)
    pd.DataFrame({"exchange":["gate","gate"],"symbol":["X","X"],
        "effective_observed_at":pd.to_datetime(["2026-07-24T11:00Z","2026-07-24T13:00Z"],utc=True),
        "native_size_unit":["CONTRACT","CONTRACT"],"contract_multiplier":[.01,second_multiplier],
        "underlying_asset":["X","X"],"quote_asset":["USDT","USDT"],"source":["test","test"],
        "raw_file":["a","b"],"status":["VALID","VALID"],"metadata_snapshot_id":["a","b"]}).to_parquet(path,index=False)


def test_only_bracketed_stable_metadata_is_backfillable(tmp_path):
    _legacy(tmp_path/"bbo");_metadata(tmp_path/"meta")
    frame=audit.audit_capacity_backfill(tmp_path/"bbo",tmp_path/"meta",tmp_path/"report",max_age_hours=6)
    assert frame.backfillable_rows.sum()==1 and frame.non_backfillable_rows.sum()==0
    assert (tmp_path/"report/capability_audit.md").exists()


def test_changed_multiplier_or_missing_history_stays_unknown(tmp_path):
    _legacy(tmp_path/"bbo");_metadata(tmp_path/"changed",second_multiplier=.02)
    changed=audit.audit_capacity_backfill(tmp_path/"bbo",tmp_path/"changed",tmp_path/"r1")
    missing=audit.audit_capacity_backfill(tmp_path/"bbo",tmp_path/"none",tmp_path/"r2")
    assert changed.backfillable_rows.sum()==0 and "MULTIPLIER_STABILITY_UNPROVEN" in changed.reasons.iloc[0]
    assert missing.non_backfillable_rows.sum()==1 and missing.reasons.iloc[0]=="NO_CONTEMPORANEOUS_METADATA"
