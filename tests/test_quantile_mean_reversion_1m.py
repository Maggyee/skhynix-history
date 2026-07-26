from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from skhynix_research.quantile_mean_reversion_1m import (
    EVENT_COLUMNS, EXCHANGES, account_pnl, causal_history, funding_attribution,
    load_prices, pair_frame, prepare_data, simulate_global, simulate_pair, spread_bps,
)


def bars(exchange, times, retrieved="2026-01-01T00:01:00Z", price_type="trade"):
    times = pd.DatetimeIndex(times)
    base = 100 + np.arange(len(times)) * .01
    return pd.DataFrame({
        "exchange": exchange, "symbol": "X", "price_type": price_type,
        "open_time": times, "close_time": times + pd.Timedelta(seconds=59, milliseconds=999),
        "open": base, "high": base + 1, "low": base - 1, "close": base + .1,
        "retrieved_at": retrieved, "native_interval": "1m", "interval_minutes": 1,
    })


def prices_for_spread(values):
    values = np.asarray(values, float)
    b = np.full(len(values), 100.0)
    a = b * (20_000 + values) / (20_000 - values)
    return a, b


def strategy_frame(close_spreads, open_spreads=None):
    close_spreads = np.asarray(close_spreads, float)
    open_spreads = close_spreads if open_spreads is None else np.asarray(open_spreads, float)
    ca, cb = prices_for_spread(close_spreads)
    oa, ob = prices_for_spread(open_spreads)
    idx = pd.date_range("2026-01-01", periods=len(close_spreads), freq="min", tz="UTC")
    return pd.DataFrame({"open_a": oa, "open_b": ob, "open_spread_bps": open_spreads,
                         "close_a": ca, "close_b": cb, "close_spread_bps": close_spreads}, index=idx)


def ready_history(frame, mean=0.0, median=1.0):
    return pd.DataFrame({"historical_mean": mean, "historical_median": median,
                         "historical_p25": -5.0, "historical_p75": 5.0,
                         "historical_iqr": 10.0, "observation_count": 1440.0,
                         "history_status": "READY"}, index=frame.index)


def test_symmetric_spread_definition():
    assert spread_bps(101, 100) == pytest.approx(-spread_bps(100, 101))


def test_load_filters_hyperliquid_mark_and_incomplete_and_deduplicates(tmp_path: Path):
    root = tmp_path
    (root / "data/normalized").mkdir(parents=True)
    t = pd.date_range("2026-01-01", periods=2, freq="min", tz="UTC")
    frames = [bars(ex, t) for ex in EXCHANGES]
    frames += [bars("hyperliquid", t), bars("binance", t, price_type="mark")]
    old = bars("binance", t[:1], retrieved="2026-01-01T00:02:00Z"); old["close"] = 90
    frames.append(old)
    all_rows = pd.concat(frames, ignore_index=True)
    all_rows.to_parquet(root / "data/normalized/prices_1m.parquet", index=False)
    live_dir = root / "data/live_1m/prices/x"; live_dir.mkdir(parents=True)
    newer = bars("binance", t[:1], retrieved="2026-01-02T00:00:00Z"); newer["close"] = 101
    newer.to_parquet(live_dir / "p.parquet", index=False)
    out = load_prices(root, pd.Timestamp("2026-01-01T00:02:00Z"))
    assert set(out.exchange) == set(EXCHANGES)
    assert set(out.price_type) == {"trade"}
    assert out.groupby(["exchange", "open_time"]).size().max() == 1
    assert out[(out.exchange == "binance") & (out.open_time == t[0])].close.iloc[0] == 101
    assert t[1] in set(out.open_time)  # last fully closed bar remains


def test_current_incomplete_minute_is_excluded(tmp_path: Path):
    (tmp_path / "data/normalized").mkdir(parents=True)
    t = pd.DatetimeIndex([pd.Timestamp("2026-01-01T00:01:00Z")])
    pd.concat([bars(ex, t) for ex in EXCHANGES]).to_parquet(tmp_path / "data/normalized/prices_1m.parquet")
    with pytest.raises(ValueError, match="no valid"):
        load_prices(tmp_path, pd.Timestamp("2026-01-01T00:01:30Z"))


def test_gate_start_dynamic_and_pair_intersections_are_independent():
    all_times = pd.date_range("2026-01-01", periods=5, freq="min", tz="UTC")
    data = pd.concat([bars("binance", all_times), bars("bitget", all_times),
                      bars("gate", all_times[1:]), bars("okx", all_times.delete(3))], ignore_index=True)
    prepared = prepare_data(data, pd.Timestamp("2026-01-02T00:00:00Z"))
    assert prepared.gate_start == all_times[1]
    native = pair_frame(prepared, "binance", "bitget", "PAIR_NATIVE_WINDOW")
    common = pair_frame(prepared, "binance", "bitget", "COMMON_FOUR_WINDOW")
    assert all_times[3] in native.index
    assert all_times[3] not in common.index


def test_expanding_history_is_shifted_and_survives_gap():
    idx = pd.date_range("2026-01-01", periods=1441, freq="min", tz="UTC").append(
        pd.DatetimeIndex([pd.Timestamp("2026-01-02T00:02:00Z")]))
    values = np.arange(len(idx), dtype=float)
    frame = pd.DataFrame({"close_spread_bps": values}, index=idx)
    out = causal_history(frame, "EXPANDING_PAST")
    assert out.iloc[1440].historical_mean == pytest.approx(np.arange(1440).mean())
    assert out.iloc[1440].historical_p75 == pytest.approx(np.quantile(np.arange(1440), .75))
    assert out.iloc[-1].observation_count == 1441


@pytest.mark.parametrize("model,window", [("ROLLING_24H", 1440), ("ROLLING_72H", 4320), ("ROLLING_7D", 10080)])
def test_rolling_windows_are_exact_and_causal(model, window):
    idx = pd.date_range("2026-01-01", periods=window + 1, freq="min", tz="UTC")
    values = np.arange(window + 1, dtype=float)
    out = causal_history(pd.DataFrame({"close_spread_bps": values}, index=idx), model)
    assert out.iloc[-1].history_status == "READY"
    assert out.iloc[-1].historical_mean == pytest.approx(np.arange(window).mean())
    assert out.iloc[-1].historical_p25 == pytest.approx(np.quantile(np.arange(window), .25))


def test_rolling_gap_requires_full_rewarm():
    first = pd.date_range("2026-01-01", periods=1441, freq="min", tz="UTC")
    second = pd.date_range(first[-1] + pd.Timedelta(minutes=2), periods=1441, freq="min", tz="UTC")
    idx = first.append(second)
    out = causal_history(pd.DataFrame({"close_spread_bps": np.arange(len(idx))}, index=idx), "ROLLING_24H")
    assert out.loc[second[0], "history_status"] == "RESET_AFTER_GAP"
    assert out.loc[second[1439], "history_status"] == "RESET_AFTER_GAP"
    assert out.loc[second[1440], "history_status"] == "READY"


def test_upper_cross_recheck_next_open_and_frozen_mean_next_open_exit():
    frame = strategy_frame([-1, 0, 10, 5, -1, 0], [-1, 0, 10, 8, -1, 0])
    out = simulate_pair(frame, ready_history(frame), "binance", "bitget", "PAIR_NATIVE_WINDOW",
                        "EXPANDING_PAST", funding=None)
    assert len(out) == 1
    event = out.iloc[0]
    assert event.signal_time == frame.index[2]
    assert event.entry_exec_time == frame.index[3]
    assert event.exit_signal_time == frame.index[4]
    assert event.exit_exec_time == frame.index[5]
    assert event.long_exchange == "bitget" and event.short_exchange == "binance"
    assert event.status == "REALIZED"


def test_recheck_decay_and_locked_entry_sensitivity():
    frame = strategy_frame([-1, 0, 10, -1, -2], [-1, 0, 10, 2, -2])
    hist = ready_history(frame)
    recheck = simulate_pair(frame, hist, "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST")
    locked = simulate_pair(frame, hist, "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST",
                           entry_policy="LOCKED_SIGNAL_ENTRY")
    assert recheck.iloc[0].status == "ENTRY_SIGNAL_DECAYED"
    assert locked.iloc[0].entry_exec_time == frame.index[3]


def test_lower_cross_direction_and_two_sided_only():
    frame = strategy_frame([1, 0, -10, -5, 1, 2], [1, 0, -10, -8, 1, 2])
    hist = ready_history(frame)
    one = simulate_pair(frame, hist, "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST")
    two = simulate_pair(frame, hist, "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST",
                        side_policy="TWO_SIDED_P75_P25")
    assert one.empty
    assert len(two) == 1
    assert two.iloc[0].long_exchange == "binance" and two.iloc[0].short_exchange == "bitget"


def test_negative_upper_threshold_still_shorts_a_for_downward_reversion():
    frame = strategy_frame([-12, -11, -2, -3, -11, -12], [-12, -11, -2, -3, -11, -12])
    hist = ready_history(frame, mean=-10, median=-9)
    hist["historical_p25"] = -15.0; hist["historical_p75"] = -5.0; hist["historical_iqr"] = 10.0
    out = simulate_pair(frame, hist, "binance", "gate", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST")
    assert out.iloc[0].entry_direction == "SHORT_A_LONG_B"
    assert out.iloc[0].short_exchange == "binance"


def test_dynamic_mean_and_median_can_exit_at_different_times():
    frame = strategy_frame([-1, 0, 10, 5, -1, 0], [-1, 0, 10, 8, -1, 0])
    hist = ready_history(frame)
    hist.loc[frame.index[3], "historical_mean"] = 6
    frozen = simulate_pair(frame, hist, "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST")
    dynamic = simulate_pair(frame, hist, "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST",
                            exit_policy="DYNAMIC_CAUSAL_MEAN")
    assert frozen.iloc[0].exit_signal_time == frame.index[4]
    assert dynamic.iloc[0].exit_signal_time == frame.index[3]
    median = simulate_pair(frame, ready_history(frame, mean=-2, median=6), "binance", "bitget",
                           "PAIR_NATIVE_WINDOW", "EXPANDING_PAST", exit_policy="FROZEN_ENTRY_MEDIAN")
    assert median.iloc[0].exit_signal_time == frame.index[3]


def test_gap_right_censor_and_max_hold_statuses():
    frame = strategy_frame([-1, 0, 10, 8, 7, 6], [-1, 0, 10, 8, 7, 6])
    hist = ready_history(frame)
    censored = simulate_pair(frame, hist, "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST")
    forced = simulate_pair(frame, hist, "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST", max_hold=1)
    assert censored.iloc[0].status == "RIGHT_CENSORED"
    assert pd.isna(censored.iloc[0].net_account_price_pnl_bps)
    assert forced.iloc[0].status == "MAX_HOLD"
    gap_frame = frame.drop(frame.index[4])
    gap = simulate_pair(gap_frame, ready_history(gap_frame), "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST")
    assert gap.iloc[0].status == "DATA_GAP_DURING_HOLD"


def test_account_cost_basis_is_leg_average_then_one_cost():
    leg_sum, gross = account_pnl(100, 100, 1.01 * 100, .99 * 100)
    assert leg_sum == pytest.approx(200)
    assert gross == pytest.approx(100)
    assert gross - 20 == pytest.approx(80)


def test_realized_diagnostics_are_numeric():
    frame = strategy_frame([-1, 0, 10, 5, -1, 0], [-1, 0, 10, 8, -1, 0])
    out = simulate_pair(frame, ready_history(frame), "binance", "bitget", "PAIR_NATIVE_WINDOW", "EXPANDING_PAST")
    assert np.isfinite(float(out.iloc[0].mae_account_price_pnl_bps))
    assert np.isfinite(float(out.iloc[0].mfe_account_price_pnl_bps))


def test_funding_boundaries_are_strict_and_account_weighted():
    times = pd.to_datetime(["2026-01-01T00:00Z", "2026-01-01T01:00Z", "2026-01-01T02:00Z"], utc=True)
    funding = pd.DataFrame({"exchange": ["a", "a", "b"], "funding_time": times,
                            "funding_rate": [.01, .02, .03]})
    long_bps, short_bps, account = funding_attribution(funding, "a", "b", times[0], times[2])
    assert long_bps == pytest.approx(-200)
    assert short_bps == 0
    assert account == pytest.approx(-100)


def test_global_portfolio_chooses_largest_tail_and_never_overlaps():
    rows = []
    for pair, score, exit_minute in [("binance/bitget", 1.0, 4), ("binance/gate", 2.0, 3), ("gate/okx", 3.0, 5)]:
        row = {c: np.nan for c in EVENT_COLUMNS}
        row.update(pair=pair, data_scope="PAIR_NATIVE_WINDOW", history_model="EXPANDING_PAST",
                   strategy_side_policy="UPPER_P75_ONLY", entry_execution_policy="RECHECK_AT_NEXT_OPEN",
                   exit_center_policy="FROZEN_ENTRY_MEAN", max_holding_minutes=np.nan,
                   entry_exec_time=pd.Timestamp("2026-01-01T00:01Z") if pair != "gate/okx" else pd.Timestamp("2026-01-01T00:02Z"),
                   exit_exec_time=pd.Timestamp(f"2026-01-01T00:0{exit_minute}Z"), entry_long_price=100,
                   tail_score=score, status="REALIZED", net_account_price_pnl_bps=10)
        rows.append(row)
    chosen, _, metrics = simulate_global(pd.DataFrame(rows), pd.Timestamp("2026-01-01T00:00Z"), pd.Timestamp("2026-01-01T00:10Z"))
    assert chosen.pair.tolist() == ["binance/gate"]
    assert metrics["rejected_while_occupied"] == 2
