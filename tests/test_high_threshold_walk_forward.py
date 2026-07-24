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
    # Keep selection small in the unit test; production retains the full grid.
    monkeypatch.setattr(wf, "REGIME_GRID", ((2, 1.0, .9, .5),))
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
    assert set(folds.parameters_selected_on) == {"TRAIN_ONLY"}
    assert {wf.PSEUDO_OOS, wf.TRUE_OOS} == set(folds.oos_kind)
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["test_reselection_allowed"] is False
    assert manifest["parameter_policy"] == "TRAIN_ONLY_THEN_FROZEN_FOR_TEST"


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
