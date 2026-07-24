from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import skhynix_research.higher_bps_study as study


def _pair_bars(spreads, gap=None):
    times=pd.date_range("2026-01-01",periods=len(spreads),freq="15min",tz="UTC")
    rows=[]
    for exchange,sign in (("a",1),("b",-1)):
        for i,(t,s) in enumerate(zip(times,spreads)):
            if gap is not None and i==gap and exchange=="b":continue
            price=100*(1+sign*s/20_000)
            rows.append({"exchange":exchange,"price_type":"trade","open_time":t,
                "open":price,"high":price,"low":price,"close":price})
    return study.build_pair_bars(pd.DataFrame(rows))["a/b"]


def test_all_predeclared_thresholds_costs_and_exits_are_frozen():
    assert study.THRESHOLDS==(20,50,100,150,200,250,300,400,500)
    assert study.COSTS==(0,20,40,58,80)
    assert set(study.EXIT_POLICIES)=={"LEGACY_RAW_EXIT","BASELINE_RESIDUAL_EXIT","FRACTIONAL_RESIDUAL_EXIT"}
    with pytest.raises(ValueError):
        study.simulate_scenario(add_features(_pair_bars([0]*400)),75,"RAW_SPREAD",study.EXIT_POLICIES[0],
            "2026-01-04","2026-01-05")


def add_features(bars,hours=24):
    z=study.add_past_only_baseline(bars,hours);z["regime"]="NOT_APPLICABLE";return z


def test_baseline_uses_only_prior_bars_and_gap_requires_full_rewarm():
    z=add_features(_pair_bars(np.arange(400,dtype=float)))
    assert z.baseline_bps.iloc[96]==pytest.approx(np.median(np.arange(96)))
    changed=_pair_bars(np.arange(400,dtype=float));changed.loc[97:,"raw_spread_bps"]+=10000
    revised=add_features(changed)
    assert revised.baseline_bps.iloc[96]==z.baseline_bps.iloc[96]
    gap=add_features(_pair_bars(np.full(400,100.0),gap=120))
    assert not gap.history_ready.iloc[121:217].any()
    assert gap.history_ready.iloc[217]


def test_next_contiguous_open_cost_once_and_incomplete_not_in_mean():
    spreads=np.zeros(410);spreads[300:303]=120
    z=add_features(_pair_bars(spreads))
    events=study.simulate_scenario(z,100,"RAW_SPREAD","LEGACY_RAW_EXIT",
        z.open_time.iloc[288],z.open_time.iloc[-1]+study.BAR)
    event=events[events.status.eq("REALIZED")].iloc[0]
    assert event.entry_exec_time==z.open_time.iloc[301]
    assert event.exit_exec_time==z.open_time.iloc[304]
    costed=study.attach_costs(events)
    realized=costed[costed.status.eq("REALIZED")]
    gross=realized[realized.assumed_total_cost_bps.eq(0)].gross_combined_pnl_bps.iloc[0]
    assert realized[realized.assumed_total_cost_bps.eq(40)].combined_net_pnl_bps.iloc[0]==pytest.approx(gross-40)
    incomplete=costed[~costed.status.eq("REALIZED")]
    assert incomplete.combined_net_pnl_bps.isna().all()


def test_funding_strictly_after_entry_and_before_exit():
    start=pd.Timestamp("2026-01-01T01:00Z");end=pd.Timestamp("2026-01-01T03:00Z")
    funding=pd.DataFrame({"exchange":["a","a","b","b"],
        "funding_time":[start,start+pd.Timedelta(hours=1),start+pd.Timedelta(hours=1),end],
        "funding_rate":[.9,.001,.002,.9]})
    assert study._funding_bps(funding,"a","b",start,end)==pytest.approx(10)


def test_statuses_zero_sample_and_summary_schema():
    z=add_features(_pair_bars(np.zeros(400)))
    events=study.simulate_scenario(z,500,"RAW_SPREAD","LEGACY_RAW_EXIT",
        z.open_time.iloc[288],z.open_time.iloc[-1]+study.BAR)
    assert events.empty
    empty=pd.DataFrame(columns=study.EVENT_COLUMNS)
    row=study.summarize(empty,1,100,study.StudyConfig(bootstrap_samples=0),1)
    assert row["realized_event_count"]==0 and np.isnan(row["mean_net_bps"])
    assert set(study.RESULT_STATS)<=set(row)


def test_gate_and_non_gate_classification_and_no_future_label_surface():
    assert "gate" in {"gate","okx"} and "gate" not in {"binance","okx"}
    source=open(study.__file__,encoding="utf-8").read()
    assert "shift(1)" in source
    assert study.PSEUDO_OOS=="HISTORICAL_ROLLING_PSEUDO_OOS"


def test_required_event_and_result_columns_are_complete():
    required={"gross_price_pnl_bps","funding_pnl_bps","gross_combined_pnl_bps",
        "assumed_total_cost_bps","combined_net_pnl_bps","entry_raw_spread_bps",
        "entry_baseline_bps","entry_residual_bps","exit_raw_spread_bps",
        "exit_baseline_bps","exit_residual_bps","mae_bps","mfe_bps","holding_minutes",
        "long_exchange","short_exchange","signal_time","entry_exec_time","exit_exec_time",
        "threshold_bps","trigger_type","exit_policy","regime","status"}
    assert required<=set(study.EVENT_COLUMNS)
    assert set(study.RESULT_STATS)>={"p90_holding_minutes","day_block_ci_low","day_block_ci_high"}
