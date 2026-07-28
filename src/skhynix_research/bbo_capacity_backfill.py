"""Evidence-only audit for legacy BBO capacity backfill safety."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .config import ROOT
from .live_bbo import (BBO_ROOT, CAPACITY_VALID, METADATA_HISTORY_ROOT,
    SIZE_UNIT_OK, normalize_bbo_schema, read_metadata_history)

REPORT_ROOT = ROOT / "reports_bbo_capacity_backfill"


def _audit_partition(path: Path, metadata: pd.DataFrame, max_age_hours: float) -> dict:
    raw=pd.read_parquet(path);frame=normalize_bbo_schema(raw)
    exchange=(str(raw.exchange.iloc[0]) if len(raw) and "exchange" in raw else path.parent.name.removeprefix("exchange="))
    day=path.parent.parent.name.removeprefix("date=");total=len(frame)
    already=int(frame.capacity_status.eq(CAPACITY_VALID).sum())
    legacy=frame[~frame.capacity_status.eq(CAPACITY_VALID)].copy()
    if legacy.empty:
        return {"date":day,"exchange":exchange,"rows":total,"already_valid_rows":already,
            "backfillable_rows":0,"non_backfillable_rows":0,"no_metadata_rows":0,
            "stability_unproven_rows":0,"size_unit_unknown_rows":0,
            "safe_backfill_pct":0.,"reasons":"ALREADY_CAPACITY_VALID"}
    if "receive_ts" not in legacy:
        return {"date":day,"exchange":exchange,"rows":total,"already_valid_rows":already,
            "backfillable_rows":0,"non_backfillable_rows":len(legacy),"no_metadata_rows":len(legacy),
            "stability_unproven_rows":0,"size_unit_unknown_rows":0,
            "safe_backfill_pct":0.,"reasons":"MISSING_RECEIVE_TIME"}
    meta=metadata[metadata.exchange.eq(exchange)].copy() if len(metadata) else pd.DataFrame()
    if meta.empty:
        return {"date":day,"exchange":exchange,"rows":total,"already_valid_rows":already,
            "backfillable_rows":0,"non_backfillable_rows":len(legacy),"no_metadata_rows":len(legacy),
            "stability_unproven_rows":0,"size_unit_unknown_rows":0,
            "safe_backfill_pct":0.,"reasons":"NO_CONTEMPORANEOUS_METADATA"}
    legacy["receive_ts"]=pd.to_datetime(legacy.receive_ts,utc=True)
    meta=meta.sort_values("effective_observed_at").copy()
    meta["effective_observed_at"]=pd.to_datetime(meta.effective_observed_at,utc=True)
    prev=pd.merge_asof(legacy[["receive_ts"]].sort_values("receive_ts"),meta,
        left_on="receive_ts",right_on="effective_observed_at",direction="backward")
    nxt=pd.merge_asof(legacy[["receive_ts"]].sort_values("receive_ts"),meta[[
        "effective_observed_at","contract_multiplier"]].rename(columns={
        "effective_observed_at":"next_metadata_at","contract_multiplier":"next_multiplier"}),
        left_on="receive_ts",right_on="next_metadata_at",direction="forward")
    merged=prev.join(nxt[["next_metadata_at","next_multiplier"]])
    age=(merged.receive_ts-merged.effective_observed_at).dt.total_seconds()/3600
    usable=merged.status.eq("VALID") & merged.native_size_unit.ne("UNKNOWN") & pd.to_numeric(
        merged.contract_multiplier,errors="coerce").gt(0) & age.between(0,max_age_hours)
    # A second snapshot with the same multiplier proves that the mapping did not
    # change across the quote. A lone/current snapshot is never extrapolated backward.
    stable=merged.next_metadata_at.notna() & np.isclose(pd.to_numeric(merged.contract_multiplier,
        errors="coerce"),pd.to_numeric(merged.next_multiplier,errors="coerce"),equal_nan=False)
    safe=usable & stable;backfillable=int(safe.sum());non=len(legacy)-backfillable
    no_metadata=(merged.effective_observed_at.isna() | ~age.between(0,max_age_hours))
    size_unknown=merged.effective_observed_at.notna() & ~merged.status.eq("VALID")
    stability_unproven=usable & ~stable
    reasons=[]
    if no_metadata.any():reasons.append("NO_CONTEMPORANEOUS_METADATA")
    if stability_unproven.any():reasons.append("MULTIPLIER_STABILITY_UNPROVEN")
    if size_unknown.any():reasons.append("SIZE_UNIT_UNKNOWN")
    if backfillable:reasons.append("SAFE_TIME_MATCHED_METADATA")
    return {"date":day,"exchange":exchange,"rows":total,"already_valid_rows":already,
        "backfillable_rows":backfillable,"non_backfillable_rows":non,
        "no_metadata_rows":int(no_metadata.sum()),
        "stability_unproven_rows":int(stability_unproven.sum()),
        "size_unit_unknown_rows":int(size_unknown.sum()),
        "safe_backfill_pct":100*backfillable/len(legacy) if len(legacy) else 0.,
        "reasons":";".join(reasons) or "CAPACITY_UNKNOWN"}


def audit_capacity_backfill(bbo_root: Path = BBO_ROOT, metadata_root: Path = METADATA_HISTORY_ROOT,
                            report_root: Path = REPORT_ROOT, max_age_hours: float = 6):
    files=sorted(Path(bbo_root).glob("date=*/exchange=*/*.parquet"));metadata=read_metadata_history(metadata_root)
    rows=[_audit_partition(path,metadata,max_age_hours) for path in files]
    frame=pd.DataFrame(rows)
    if len(frame):
        frame=(frame.groupby(["date","exchange"],as_index=False).agg({"rows":"sum",
            "already_valid_rows":"sum","backfillable_rows":"sum","non_backfillable_rows":"sum",
            "no_metadata_rows":"sum","stability_unproven_rows":"sum","size_unit_unknown_rows":"sum",
            "safe_backfill_pct":"mean","reasons":lambda x:";".join(sorted(set(";".join(x).split(";"))))}))
    else:
        frame=pd.DataFrame(columns=["date","exchange","rows","already_valid_rows",
            "backfillable_rows","non_backfillable_rows","no_metadata_rows",
            "stability_unproven_rows","size_unit_unknown_rows","safe_backfill_pct","reasons"])
    report_root=Path(report_root);report_root.mkdir(parents=True,exist_ok=True)
    frame.to_csv(report_root/"date_exchange_coverage.csv",index=False)
    total=int(frame.rows.sum()) if len(frame) else 0;back=int(frame.backfillable_rows.sum()) if len(frame) else 0
    non=int(frame.non_backfillable_rows.sum()) if len(frame) else 0
    reasons=({"NO_CONTEMPORANEOUS_METADATA":int(frame.no_metadata_rows.sum()),
        "MULTIPLIER_STABILITY_UNPROVEN":int(frame.stability_unproven_rows.sum()),
        "SIZE_UNIT_UNKNOWN":int(frame.size_unit_unknown_rows.sum())} if len(frame) else {})
    text=f"""# Legacy BBO capacity backfill capability audit

This audit is evidence-only. It does not mutate BBO parts and never applies current
metadata backward without a time match.

- Rows inspected: {total}
- Backfillable legacy rows: {back}
- Non-backfillable legacy rows: {non}
- Safe legacy backfill ratio: {(100*back/(back+non)) if back+non else 0:.4f}%
- Maximum accepted metadata age: {max_age_hours:g} hours
- Reasons: `{reasons}`

A legacy row is backfillable only when a valid prior metadata snapshot is within the
age bound and a following snapshot proves the same contract multiplier. All other
rows remain `CAPACITY_UNKNOWN`; price fields are never used to infer a multiplier.

See `date_exchange_coverage.csv` for UTC date/exchange coverage.
"""
    (report_root/"capability_audit.md").write_text(text)
    return frame
