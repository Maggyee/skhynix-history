from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import skhynix_research.gate_regime_15m as gr


def _prices(periods=8):
    rows=[]
    for typ in ("trade","mark","index"):
        for ex_i,ex in enumerate(("binance","bitget","gate","okx")):
            for i,t in enumerate(pd.date_range("2026-07-15T23:00Z",periods=periods,freq="15min")):
                close=100+i/10+ex_i+(2 if typ=="trade" else 1 if typ=="mark" else 0)
                rows.append({"exchange":ex,"symbol":"SKHYNIX","price_type":typ,"open_time":t,
                    "open":close,"high":close+1,"low":close-1,"close":close,"volume_base":i+1,
                    "native_interval":"15m","interval_minutes":15,"source_endpoint":"official/native",
                    "raw_file":f"{ex}-{typ}"})
    for i,t in enumerate(pd.date_range("2026-07-15T23:00Z",periods=periods,freq="15min")):
        close=104+i/10
        rows.append({"exchange":"hyperliquid","symbol":"SKHX","price_type":"trade","open_time":t,
            "open":close,"high":close+1,"low":close-1,"close":close,"volume_base":i+1,
            "native_interval":"15m","interval_minutes":15,"source_endpoint":"official/native","raw_file":"hl"})
    return pd.DataFrame(rows)


def test_strict_same_timestamp_alignment_and_no_fill():
    p=_prices(); missing=p[(p.exchange=="okx")&(p.price_type=="trade")].index[2]; p=p.drop(missing)
    wide=gr.strict_wide(p,"trade")
    assert pd.isna(wide.iloc[2].okx) and wide.index[2].minute==30


def test_gate_direction_sign_is_positive_when_gate_is_higher():
    trade,_,_=gr.build_core(_prices())
    assert (trade.gate_vs_binance_bps>0).all()


def test_market_median_never_contains_gate():
    wide=pd.DataFrame({"binance":[100.],"bitget":[102.],"okx":[104.],"gate":[999.]})
    assert gr.external_median(wide).iloc[0]==102
    with pytest.raises(ValueError): gr.external_median(wide,("binance","gate"))


def test_mark_median_does_not_mix_trade():
    p=_prices(); p.loc[(p.exchange=="binance")&(p.price_type=="trade"),"close"]=9999
    _,mark,_=gr.build_core(p)
    assert mark.market_mark_median.max()<200


def test_main_external_median_requires_strict_three_of_three():
    wide=pd.DataFrame({"binance":[100.,np.nan],"bitget":[102.,102.],"okx":[np.nan,104.]})
    assert gr.external_median(wide).isna().all()


def test_okx_missing_makes_main_result_nan_and_sensitivity_is_separate():
    wide=pd.DataFrame({"binance":[100.,100.],"bitget":[102.,102.],"okx":[104.,np.nan],"gate":[103.,103.]})
    main=gr.external_median(wide)
    sensitivity=gr.external_sensitivity(wide)
    assert main.iloc[0]==102 and pd.isna(main.iloc[1])
    assert sensitivity.index.tolist()==[1]
    assert sensitivity.external_scope.eq(gr.SENSITIVITY_EXTERNAL_SCOPE).all()
    assert sensitivity.available_2_of_3_external_median.iloc[0]==101


def test_decomposition_residual_is_exactly_reported():
    _,_,d=gr.build_core(_prices()); expected=(d.total_gate_trade_vs_market_bps-d.gate_trade_minus_gate_mark_bps-
        d.gate_mark_minus_gate_index_bps-d.gate_index_minus_market_bps)
    pd.testing.assert_series_equal(d.decomposition_residual_bps,expected,check_names=False)


def test_regime_boundaries_are_left_closed_right_open():
    assert gr.regime_for_time("2026-07-15T23:59:59Z")=="PRE_20260716"
    assert gr.regime_for_time("2026-07-16T00:00Z")=="GATE_REGIME_20260716_20260720"
    assert gr.regime_for_time("2026-07-20T00:00Z")=="POST_20260720"


def test_future_changes_do_not_change_past_causal_labels():
    prices=_prices(periods=140)
    original=gr.build_causal_regime_labels(prices)
    cutoff=pd.Timestamp("2026-07-17T00:00Z")
    changed=prices.copy()
    changed.loc[changed.open_time>cutoff,"close"]*=1.25
    changed.loc[changed.open_time>cutoff,"high"]=changed.loc[changed.open_time>cutoff,["high","close"]].max(axis=1)+1
    revised=gr.build_causal_regime_labels(changed)
    columns=["open_time","causal_regime","regime_reason","is_entry_allowed"]
    pd.testing.assert_frame_equal(
        original.loc[original.open_time<=cutoff,columns].reset_index(drop=True),
        revised.loc[revised.open_time<=cutoff,columns].reset_index(drop=True),
    )


def test_retrospective_change_points_cannot_enter_strategy_provider():
    changes=pd.DataFrame({"change_time":[pd.Timestamp("2026-07-16T00:00Z")],"confidence_metric":[10.]})
    with pytest.raises(ValueError,match="retrospective"):
        gr.causal_regime_for_time(changes,"2026-07-16T00:00Z")


def test_data_gap_and_following_warmup_are_stale_or_invalid():
    prices=_prices(periods=140)
    gap=pd.Timestamp("2026-07-17T00:00Z")
    prices=prices[~((prices.exchange=="okx")&(prices.price_type=="trade")&(prices.open_time==gap))]
    labels=gr.build_causal_regime_labels(prices).set_index("open_time")
    assert labels.loc[gap,"causal_regime"]=="STALE_OR_INVALID"
    assert labels.loc[gap,"regime_reason"]=="STRICT_EXTERNAL_3_OF_3_MISSING"
    assert labels.loc[gap+gr.BAR,"causal_regime"]=="STALE_OR_INVALID"
    assert labels.loc[gap+2*gr.BAR,"causal_regime"]=="NORMAL"


def test_causal_labels_only_use_current_and_past_rolling_windows():
    prices=_prices(periods=140)
    labels=gr.build_causal_regime_labels(prices).set_index("open_time")
    t=labels.index[110]
    past=prices[prices.open_time<=t]
    truncated=gr.build_causal_regime_labels(past).set_index("open_time")
    pd.testing.assert_series_equal(labels.loc[t],truncated.loc[t],check_names=False)


def test_data_gap_breaks_continuous_event():
    idx=pd.DatetimeIndex([pd.Timestamp("2026-07-01T00:00Z"),pd.Timestamp("2026-07-01T00:30Z")])
    trade=pd.DataFrame({"gate_premium_vs_market_median_bps":[120.,130.]},index=idx)
    ev=gr.continuous_events(trade)
    assert len(ev[ev.threshold_bps==100])==2


def test_event_duration_is_a_15_minute_multiple():
    trade,_,_=gr.build_core(_prices()); trade["gate_premium_vs_market_median_bps"]=210
    ev=gr.continuous_events(trade)
    assert len(ev) and (ev.duration_minutes%15==0).all()


def test_low_volume_groups_drop_nan():
    p=_prices(); p.loc[(p.exchange=="gate")&(p.price_type=="trade")].iloc[:2]
    idx=p[(p.exchange=="gate")&(p.price_type=="trade")].index[:2];p.loc[idx,"volume_base"]=np.nan
    trade,_,_=gr.build_core(p); _,summary=gr.liquidity_analysis(p,trade)
    assert summary["count"].sum()==len(trade)-2


def test_unsupported_causes_use_allowed_statuses():
    dq=pd.DataFrame({"conclusion":["DATA_ERROR_NOT_SUPPORTED"]}); liq=pd.DataFrame([{"p95_abs_premium_low_volume_bps":1,"p95_abs_premium_normal_volume_bps":2}])
    h=gr.hypothesis_table(dq,liq,pd.DataFrame(),pd.DataFrame(),pd.DataFrame())
    assert set(h.status)<=set(["SUPPORTED","PARTIALLY_SUPPORTED","NOT_SUPPORTED","INCONCLUSIVE"])
    assert h.loc[h.hypothesis.str.startswith("H3"),"status"].iloc[0]=="INCONCLUSIVE"


@pytest.fixture(scope="module")
def generated_bundle(tmp_path_factory):
    root=tmp_path_factory.mktemp("gate15");(root/"data/normalized").mkdir(parents=True);(root/"data/raw").mkdir(parents=True);(root/"reports_15m").mkdir()
    source=Path(gr.__file__).parents[2]
    shutil.copy2(source/"data/normalized/prices_15m.parquet",root/"data/normalized/prices_15m.parquet")
    shutil.copy2(source/"data/normalized/funding_events.parquet",root/"data/normalized/funding_events.parquet")
    sentinel=root/"data/raw/sentinel";sentinel.write_text("unchanged");(root/"reports_15m/quick_report_15m.html").write_text("<html><body><main></main></body></html>")
    first=gr.run(root); hashes1={p.relative_to(root):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/"reports_15m").rglob("*") if p.is_file()}
    second=gr.run(root); hashes2={p.relative_to(root):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/"reports_15m").rglob("*") if p.is_file()}
    yield root,first,second,hashes1,hashes2
    gr.ROOT=source;gr.R15=source/"reports_15m";gr.CHARTS=gr.R15/"charts"


def test_external_source_absence_still_generates_report(generated_bundle):
    root,*_=generated_bundle
    text=(root/"reports_15m/gate_regime_15m_diagnostics.md").read_text()
    assert "外部原因未验证" in text


def test_csv_and_report_core_numbers_match(generated_bundle):
    root,*_=generated_bundle;s=pd.read_csv(root/"reports_15m/gate_regime_15m_summary.csv");doc=(root/"reports_15m/gate_regime_15m_diagnostics.md").read_text()
    assert f"{s.p95_abs_bps.iloc[0]:.1f}" in doc


def test_quick_report_main_statistics_are_strict_three_of_three(generated_bundle):
    root,*_=generated_bundle
    text=(root/"reports_15m/quick_report_15m.html").read_text()
    assert gr.STRICT_EXTERNAL_SCOPE in text


def test_report_window_does_not_exceed_strict_external_window(generated_bundle):
    root,first,*_=generated_bundle
    labels=pd.read_csv(root/"reports_15m/gate_causal_regime_labels_15m.csv",parse_dates=["open_time"])
    strict=labels[labels.external_venue_count==3]
    assert first["price_start"]>=strict.open_time.min()
    assert first["price_end_exclusive"]<=strict.open_time.max()+gr.BAR


def test_causal_regime_output_schema_and_values(generated_bundle):
    root,*_=generated_bundle
    labels=pd.read_csv(root/"reports_15m/gate_causal_regime_labels_15m.csv")
    required={"open_time","gate_premium_vs_market_median_bps","external_venue_count",
        "rolling_median_4h_bps","rolling_median_12h_bps","rolling_median_24h_bps",
        "rolling_mad_24h_bps","same_sign_ratio_24h","causal_regime","regime_reason",
        "is_entry_allowed"}
    assert required<=set(labels.columns)
    assert set(labels.causal_regime)<=set(gr.CAUSAL_REGIMES)


def test_chart_bundle_is_generated(generated_bundle):
    root,*_=generated_bundle
    expected=["gate_premium_timeseries_15m.png","gate_regime_distribution_15m.png","gate_trade_mark_index_decomposition_15m.png",
        "gate_premium_vs_volume_15m.png","gate_premium_by_session_15m.png","gate_funding_vs_premium_15m.png"]
    assert all((root/"reports_15m/charts"/x).stat().st_size>1000 for x in expected)


def test_repeated_run_is_idempotent(generated_bundle):
    *_,h1,h2=generated_bundle
    assert h1==h2


def test_gate_diagnostic_does_not_import_joint_strategy_module():
    source=Path(gr.__file__).read_text()
    assert "fifteen_minute" not in source and "joint_strategy" not in source


def test_run_does_not_change_raw_data(generated_bundle):
    root,*_=generated_bundle
    assert (root/"data/raw/sentinel").read_text()=="unchanged"
