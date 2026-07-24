import json

import numpy as np
import pandas as pd
import pytest

import skhynix_research.high_threshold_walk_forward as wf


def _prices(spreads):
    times = pd.date_range("2026-01-01", periods=len(spreads), freq="15min", tz="UTC")
    rows = []
    for exchange, sign in [("a", 1), ("b", -1)]:
        for t, spread in zip(times, spreads):
            price = 100 * (1 + sign * spread / 20_000)
            rows.append({"exchange": exchange, "price_type": "trade", "open_time": t,
                         "open": price, "high": price, "low": price, "close": price})
    return pd.DataFrame(rows)


def test_fixed_surface_and_forbidden_cost_threshold():
    assert wf.EXECUTION_MODEL == "NEXT_BAR_OPEN_PROXY"
    assert wf.THRESHOLDS == (100, 150, 200)
    assert wf.COSTS == (20, 40, 80)
    assert wf.ALLOWED_ENTRY_REGIMES == {"NORMAL", "TRANSIENT_DISLOCATION"}
    with pytest.raises(ValueError):
        wf._validate_threshold(50)
    with pytest.raises(ValueError):
        wf._validate_cost(10)


def test_single_position_next_open_and_regime_gate():
    # One spike remains active for three closes.  It must produce one position,
    # with the next bar's open used for both entry and exit.
    bars = wf.build_pair_bars(_prices([0, 0, 120, 130, 140, 0, 0]))["a/b"]
    params = wf.RegimeParameters(lookback_bars=2, persistence_ratio=1.0,
                                 structural_abs_bps=10_000, transient_abs_bps=50)
    events, diag = wf.simulate_period(bars, params, 100, 20,
                                      bars.open_time.min(), bars.open_time.max() + pd.Timedelta(minutes=15))
    assert len(events) == 1
    event = events.iloc[0]
    assert event.execution_model == "NEXT_BAR_OPEN_PROXY"
    assert event.entry_exec_time == bars.open_time.iloc[3]
    assert event.exit_exec_time == bars.open_time.iloc[6]
    assert diag["same_pair_blocked_overlap_signal_count"] >= 1
    assert 0 < diag["capital_occupancy_rate"] <= 1


def test_walk_forward_outputs_and_oos_labels(tmp_path, monkeypatch):
    spreads = np.tile([0, 120, 130, 0, 0, 160, 170, 0], 5)
    config = wf.WalkForwardConfig(train_bars=16, test_bars=8, min_train_events=1,
                                  bootstrap_samples=50, future_oos_start="2026-01-01T06:00:00Z")
    result = wf.run_walk_forward(_prices(spreads), config=config, output_dir=tmp_path)
    assert {"fold_events.csv", "fold_results.csv", "aggregate_results.csv",
            "frozen_parameters.csv", "manifest.json", "README.md"} <= {p.name for p in tmp_path.iterdir()}
    folds = result["folds"]
    assert set(folds.threshold_bps) <= set(wf.THRESHOLDS)
    assert set(folds.cost_bps) == set(wf.COSTS)
    assert folds.parameters_frozen.all()
    assert set(folds.parameters_selected_on) == {"NOT_APPLICABLE_FIXED_SCENARIO"}
    assert {wf.PSEUDO_OOS} == set(folds.oos_kind)
    assert set(folds.threshold_bps) == set(wf.THRESHOLDS)
    assert {"total_test_signals","realized_event_count","censored_event_count",
            "block_bootstrap_ci_low","block_bootstrap_ci_high"} <= set(folds.columns)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["test_reselection_allowed"] is False
    assert manifest["parameter_policy"] == "FIXED_THRESHOLD_MAIN_NO_THRESHOLD_SELECTION"
    assert manifest["future_true_oos_enabled"] is False


def test_fixed_thresholds_are_never_mixed_in_aggregate(tmp_path):
    spreads=np.tile([0,120,130,0,0,160,170,0],5)
    result=wf.run_walk_forward(_prices(spreads),config=wf.WalkForwardConfig(
        train_bars=16,test_bars=8,min_train_events=1,bootstrap_samples=20),output_dir=tmp_path)
    aggregate=result["aggregate"]
    required={"pair","threshold_bps","cost_bps","regime_policy","oos_kind","execution_model"}
    assert required<=set(aggregate.columns)
    assert set(aggregate.threshold_bps)=={100,150,200}
    assert not aggregate.duplicated(list(required)).any()


def test_censored_signals_are_retained_with_explicit_status():
    params=wf.RegimeParameters(2,1.0,10_000,50)
    cases=[([0,120],"NO_NEXT_BAR_FOR_ENTRY"),
           ([0,120,0],"NO_NEXT_BAR_FOR_EXIT"),
           ([0,120,130,140],"RIGHT_CENSORED")]
    for spreads,status in cases:
        bars=wf.build_pair_bars(_prices(spreads))["a/b"]
        events,_=wf.simulate_period(bars,params,100,20,bars.open_time.min(),
            bars.open_time.max()+pd.Timedelta(minutes=15))
        assert status in set(events.status)
        assert events.loc[events.status.eq(status),"net_pnl_bps"].isna().all()


def test_data_gap_during_hold_is_not_silently_dropped():
    params=wf.RegimeParameters(2,1.0,10_000,50)
    bars=wf.build_pair_bars(_prices([0,120,130,140,0]))["a/b"].drop(index=3).reset_index(drop=True)
    events,_=wf.simulate_period(bars,params,100,20,bars.open_time.min(),
        bars.open_time.max()+pd.Timedelta(minutes=15))
    assert "DATA_GAP_DURING_HOLD" in set(events.status)


def test_gate_and_non_gate_regime_policies_are_separate(tmp_path,monkeypatch):
    base=_prices(np.tile([0,120,130,0,0,160,170,0],5))
    gate=base[base.exchange.eq("b")].copy();gate["exchange"]="gate"
    binance=base[base.exchange.eq("a")].copy();binance["exchange"]="binance"
    okx=base[base.exchange.eq("b")].copy();okx["exchange"]="okx"
    prices=pd.concat([binance,gate,okx],ignore_index=True)
    times=pd.DataFrame({"open_time":sorted(prices.open_time.unique()),"causal_regime":"NORMAL"})
    monkeypatch.setattr(wf,"build_causal_regime_labels",lambda _:times)
    result=wf.run_walk_forward(prices,config=wf.WalkForwardConfig(
        train_bars=16,test_bars=8,min_train_events=1,bootstrap_samples=10),output_dir=tmp_path)
    folds=result["folds"]
    assert set(folds.loc[folds.pair.str.contains("gate"),"regime_policy"])=={wf.GATE_REGIME_POLICY}
    assert set(folds.loc[~folds.pair.str.contains("gate"),"regime_policy"])=={wf.NON_GATE_REGIME_POLICY}


def test_default_minimum_training_sample_is_ten():
    assert wf.WalkForwardConfig().min_train_events>=10


def test_gate_structural_regime_is_forbidden_but_non_gate_has_no_gate_filter():
    bars=wf.build_pair_bars(_prices([0,120,130,0,0]))["a/b"]
    bars["regime"]="STRUCTURAL_PREMIUM"
    params=wf.RegimeParameters(2,1.0,10_000,50)
    gate_events,_=wf.simulate_period(bars,params,100,20,bars.open_time.min(),
        bars.open_time.max()+pd.Timedelta(minutes=15),regimes_preclassified=True,
        regime_policy=wf.GATE_REGIME_POLICY)
    generic_events,_=wf.simulate_period(bars,params,100,20,bars.open_time.min(),
        bars.open_time.max()+pd.Timedelta(minutes=15),regimes_preclassified=True,
        regime_policy=wf.NON_GATE_REGIME_POLICY)
    assert gate_events.empty
    assert not generic_events.empty


def test_test_context_includes_training_tail_warmup(tmp_path,monkeypatch):
    spreads=np.tile([0,120,130,0,0,160,170,0],20)
    original=wf.simulate_period;observed=[]
    def capture(bars,params,threshold_bps,cost_bps,start,end,*args,**kwargs):
        observed.append((bars.open_time.min(),pd.Timestamp(start)))
        return original(bars,params,threshold_bps,cost_bps,start,end,*args,**kwargs)
    monkeypatch.setattr(wf,"simulate_period",capture)
    wf.run_walk_forward(_prices(spreads),config=wf.WalkForwardConfig(
        train_bars=120,test_bars=8,min_train_events=1,bootstrap_samples=0),output_dir=tmp_path)
    assert any(start-first>=pd.Timedelta(minutes=15*95) for first,start in observed)


def test_parameter_lock_rejects_test_reselection(tmp_path):
    path = tmp_path / "frozen_parameters.csv"
    base = pd.DataFrame([{"fold_id": 1, "pair": "a/b", "cost_bps": 20,
                          "threshold_bps": 100, "train_start": "2026-01-01T00:00Z",
                          "train_end": "2026-01-02T00:00Z", "test_start": "2026-01-02T00:00Z",
                          "test_end": "2026-01-03T00:00Z", "regime_lookback_bars": 8,
                          "regime_persistence_ratio": .8, "regime_structural_abs_bps": 100,
                          "regime_transient_abs_bps": 50}])
    wf._lock_parameters(path, base)
    changed = base.copy(); changed.loc[0, "threshold_bps"] = 200
    with pytest.raises(RuntimeError, match="reselection"):
        wf._lock_parameters(path, changed)
