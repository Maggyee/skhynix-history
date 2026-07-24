"""Leakage-resistant high-threshold walk-forward evaluation.

The module intentionally has a narrow, auditable parameter surface.  Signals
are native-bar close observations and executions use the following contiguous
bar's open (``NEXT_BAR_OPEN_PROXY``).  Parameters are selected using a fold's
training slice only and are immutable while its test slice is evaluated.

This is a historical proxy study, not an executable order-book backtest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .analysis import symmetric_spread_bps
from .config import ROOT

EXECUTION_MODEL = "NEXT_BAR_OPEN_PROXY"
THRESHOLDS = (100, 150, 200)
COSTS = (20, 40, 80)
ALLOWED_ENTRY_REGIMES = frozenset({"NORMAL", "TRANSIENT_DISLOCATION"})
FORBIDDEN_ENTRY_REGIMES = frozenset({"STRUCTURAL_PREMIUM", "STALE_OR_INVALID"})
PSEUDO_OOS = "HISTORICAL_ROLLING_PSEUDO_OOS"
TRUE_OOS = "FUTURE_TRUE_OOS"


@dataclass(frozen=True)
class RegimeParameters:
    """Regime boundary values fitted on one pair's training slice."""

    lookback_bars: int
    persistence_ratio: float
    structural_abs_bps: float
    transient_abs_bps: float


@dataclass(frozen=True)
class FrozenParameters:
    fold_id: int
    pair: str
    cost_bps: int
    threshold_bps: int
    regime: RegimeParameters
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp


@dataclass(frozen=True)
class WalkForwardConfig:
    train_bars: int = 7 * 24 * 4
    test_bars: int = 2 * 24 * 4
    step_bars: int | None = None
    min_train_events: int = 2
    bootstrap_samples: int = 2000
    confidence_level: float = 0.95
    random_seed: int = 1729
    future_oos_start: pd.Timestamp | str | None = None

    def __post_init__(self):
        if self.train_bars < 2 or self.test_bars < 1:
            raise ValueError("train_bars must be >=2 and test_bars must be >=1")
        if self.step_bars is not None and self.step_bars < 1:
            raise ValueError("step_bars must be positive")
        if self.bootstrap_samples < 0 or not 0 < self.confidence_level < 1:
            raise ValueError("invalid bootstrap configuration")


# Deliberately small, predeclared search space.  A wide researcher-degree-of-
# freedom grid would weaken a short-history walk-forward study even when it is
# technically confined to training data.
REGIME_GRID = (
    (8, 0.80, 0.75, 0.75),
    (8, 0.90, 0.90, 0.75),
    (32, 0.80, 0.75, 0.75),
    (32, 0.90, 0.90, 0.75),
)


def _utc(value) -> pd.Timestamp:
    value = pd.Timestamp(value)
    return value.tz_localize("UTC") if value.tzinfo is None else value.tz_convert("UTC")


def _validate_cost(cost: int) -> int:
    if int(cost) not in COSTS:
        raise ValueError(f"cost_bps must be one of {COSTS}")
    return int(cost)


def _validate_threshold(threshold: int) -> int:
    if int(threshold) not in THRESHOLDS:
        raise ValueError(f"threshold_bps must be one of {THRESHOLDS}")
    return int(threshold)


def build_pair_bars(prices: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Build strict pair OHLC bars without filling, shifting, or resampling."""
    required = {"exchange", "price_type", "open_time", "open", "high", "low", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices missing columns: {sorted(missing)}")
    p = prices.copy()
    p["open_time"] = pd.to_datetime(p.open_time, utc=True)
    p = p[p.price_type == "trade"]
    if p.duplicated(["exchange", "open_time"]).any():
        raise ValueError("duplicate trade exchange/open_time rows")
    exchanges = sorted(p.exchange.unique())
    result: dict[str, pd.DataFrame] = {}
    for i, a in enumerate(exchanges):
        for b in exchanges[i + 1:]:
            cols = ["open_time", "open", "high", "low", "close"]
            left = p[p.exchange == a][cols]
            right = p[p.exchange == b][cols]
            z = left.merge(right, on="open_time", how="inner", suffixes=("_A", "_B"))
            if z.empty:
                continue
            z = z.sort_values("open_time").reset_index(drop=True)
            z["pair"] = f"{a}/{b}"
            z["spread_bps"] = symmetric_spread_bps(z.close_A, z.close_B)
            z["abs_spread_bps"] = z.spread_bps.abs()
            result[f"{a}/{b}"] = z
    return result


def fit_regime_parameters(train: pd.DataFrame, grid_spec) -> RegimeParameters:
    """Fit scalar regime boundaries using training observations only."""
    lookback, persistence, structural_q, transient_q = grid_spec
    x = pd.to_numeric(train.abs_spread_bps, errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if x.empty:
        raise ValueError("cannot fit regime parameters without valid training spreads")
    return RegimeParameters(
        lookback_bars=int(lookback), persistence_ratio=float(persistence),
        structural_abs_bps=float(x.quantile(structural_q)),
        transient_abs_bps=float(x.quantile(transient_q)),
    )


def classify_regimes(bars: pd.DataFrame, params: RegimeParameters) -> pd.Series:
    """Causal regime labels; rolling windows end at the current close."""
    z = bars.sort_values("open_time")
    valid_cols = ["open_A", "high_A", "low_A", "close_A", "open_B", "high_B", "low_B", "close_B"]
    valid = z[valid_cols].apply(pd.to_numeric, errors="coerce").gt(0).all(axis=1)
    spacing = z.open_time.diff()
    normal_step = spacing[spacing > pd.Timedelta(0)].mode()
    expected = normal_step.iloc[0] if len(normal_step) else pd.Timedelta(minutes=15)
    contiguous = spacing.isna() | spacing.eq(expected)
    sign = np.sign(z.spread_bps).replace(0, np.nan)
    dominant = sign.rolling(params.lookback_bars, min_periods=params.lookback_bars).apply(
        lambda a: max(np.mean(a > 0), np.mean(a < 0)), raw=True)
    rolling_abs = z.abs_spread_bps.rolling(params.lookback_bars, min_periods=params.lookback_bars).median()
    structural = (dominant >= params.persistence_ratio) & (rolling_abs >= params.structural_abs_bps)
    transient = z.abs_spread_bps >= params.transient_abs_bps
    labels = pd.Series("NORMAL", index=z.index, dtype="object")
    labels.loc[transient] = "TRANSIENT_DISLOCATION"
    labels.loc[structural] = "STRUCTURAL_PREMIUM"
    labels.loc[~valid | ~contiguous] = "STALE_OR_INVALID"
    return labels.reindex(bars.index)


def _leg_pnl(long_entry, short_entry, long_exit, short_exit) -> float:
    return ((long_exit / long_entry - 1.0) + (1.0 - short_exit / short_entry)) * 10_000


def _funding_bps(funding: pd.DataFrame | None, long_ex: str, short_ex: str,
                 start: pd.Timestamp, end: pd.Timestamp) -> float:
    if funding is None or funding.empty:
        return 0.0
    f = funding
    times = pd.to_datetime(f.funding_time, utc=True)
    active = f[(times > start) & (times < end)]
    return float((-active.loc[active.exchange == long_ex, "funding_rate"].sum()
                  + active.loc[active.exchange == short_ex, "funding_rate"].sum()) * 10_000)


def simulate_period(bars: pd.DataFrame, params: RegimeParameters, threshold_bps: int,
                    cost_bps: int, start, end, funding: pd.DataFrame | None = None,
                    *, regimes_preclassified: bool = False) -> tuple[pd.DataFrame, dict]:
    """Run one frozen parameter set.  At most one position per pair is open."""
    threshold_bps = _validate_threshold(threshold_bps)
    cost_bps = _validate_cost(cost_bps)
    start, end = _utc(start), _utc(end)
    z = bars.sort_values("open_time").reset_index(drop=True).copy()
    if not regimes_preclassified:
        z["regime"] = classify_regimes(z, params).to_numpy()
    elif "regime" not in z:
        raise ValueError("regimes_preclassified requires a regime column")
    times = pd.DatetimeIndex(z.open_time)
    if len(times) > 1:
        diffs = times.to_series().diff().dropna()
        bar = diffs.mode().iloc[0] if len(diffs) else pd.Timedelta(minutes=15)
    else:
        bar = pd.Timedelta(minutes=15)
    pair = str(z.pair.iloc[0]) if len(z) and "pair" in z else "UNKNOWN/UNKNOWN"
    a, b = pair.split("/", 1)
    abs_spreads = z.abs_spread_bps.to_numpy(float)
    spreads = z.spread_bps.to_numpy(float)
    regimes = z.regime.astype(str).to_numpy()
    open_a, open_b = z.open_A.to_numpy(float), z.open_B.to_numpy(float)
    close_a, close_b = z.close_A.to_numpy(float), z.close_B.to_numpy(float)
    rows, occupied_ns, overlap_signals = [], 0, 0
    i = int(times.searchsorted(start))
    stop = int(times.searchsorted(end))
    while i < stop:
        eligible = (abs_spreads[i] >= threshold_bps and regimes[i] in ALLOWED_ENTRY_REGIMES)
        if not eligible:
            i += 1
            continue
        entry_i = i + 1
        if entry_i >= stop or times[entry_i] != times[i] + bar:
            i += 1
            continue
        positive = spreads[i] > 0
        long_ex, short_ex = (b, a) if positive else (a, b)
        entry_long = open_b[entry_i] if positive else open_a[entry_i]
        entry_short = open_a[entry_i] if positive else open_b[entry_i]
        if not np.isfinite([entry_long, entry_short]).all() or min(entry_long, entry_short) <= 0:
            i += 1
            continue
        exit_signal_i = None
        j = entry_i
        while j < stop:
            if j > entry_i and abs_spreads[j] >= threshold_bps:
                overlap_signals += 1
            if times[j] != times[entry_i] + (j - entry_i) * bar:
                break
            if abs_spreads[j] < threshold_bps:
                exit_signal_i = j
                break
            j += 1
        exit_i = exit_signal_i + 1 if exit_signal_i is not None else None
        if exit_i is None or exit_i >= stop or times[exit_i] != times[exit_signal_i] + bar:
            i = max(i + 1, j)
            continue
        exit_long = open_b[exit_i] if positive else open_a[exit_i]
        exit_short = open_a[exit_i] if positive else open_b[exit_i]
        if not np.isfinite([exit_long, exit_short]).all() or min(exit_long, exit_short) <= 0:
            i = exit_i + 1
            continue
        ml = close_b[entry_i:exit_signal_i + 1] if positive else close_a[entry_i:exit_signal_i + 1]
        ms = close_a[entry_i:exit_signal_i + 1] if positive else close_b[entry_i:exit_signal_i + 1]
        valid_path = np.isfinite(ml) & np.isfinite(ms) & (ml > 0) & (ms > 0)
        path = ((ml[valid_path] / entry_long - 1.0) +
                (1.0 - ms[valid_path] / entry_short)) * 10_000
        gross_price = _leg_pnl(entry_long, entry_short, exit_long, exit_short)
        funding_pnl = _funding_bps(funding, long_ex, short_ex, times[entry_i], times[exit_i])
        gross = gross_price + funding_pnl
        holding = (times[exit_i] - times[entry_i]).total_seconds() / 60
        occupied_ns += (times[exit_i] - times[entry_i]).value
        rows.append({
            "pair": pair, "signal_time": times[i], "signal_regime": regimes[i],
            "threshold_bps": threshold_bps, "cost_bps": cost_bps,
            "entry_exec_time": times[entry_i], "exit_exec_time": times[exit_i],
            "holding_minutes": holding, "long_exchange": long_ex, "short_exchange": short_ex,
            "gross_price_pnl_bps": gross_price, "funding_pnl_bps": funding_pnl,
            "gross_pnl_bps": gross, "net_pnl_bps": gross - cost_bps,
            "mae_bps": float(path.min()) if len(path) else np.nan,
            "mfe_bps": float(path.max()) if len(path) else np.nan,
            "execution_model": EXECUTION_MODEL, "status": "REALIZED",
        })
        # The next signal may only be considered after this position has exited.
        i = exit_i
    duration_ns = max(0, (end - start).value)
    diagnostics = {
        "capital_occupancy_rate": occupied_ns / duration_ns if duration_ns else np.nan,
        "same_pair_blocked_overlap_signal_count": int(overlap_signals),
        "cross_pair_overlapping_event_count": 0,
    }
    return pd.DataFrame(rows), diagnostics


def _score_train(events: pd.DataFrame, min_events: int) -> tuple[float, int]:
    if len(events) < min_events:
        return -np.inf, len(events)
    # Mean is the primary objective; event count is the deterministic tie-break.
    return float(events.net_pnl_bps.mean()), len(events)


def select_parameters(train: pd.DataFrame, pair: str, fold_id: int, cost_bps: int,
                      train_start, train_end, test_start, test_end,
                      funding: pd.DataFrame | None, min_events: int) -> FrozenParameters:
    """Select using *only* ``train``; no test frame is accepted by this API."""
    cost_bps = _validate_cost(cost_bps)
    best = None
    for grid_index, spec in enumerate(REGIME_GRID):
        regime = fit_regime_parameters(train, spec)
        classified = train.copy()
        classified["regime"] = classify_regimes(classified, regime)
        for threshold in THRESHOLDS:
            events, _ = simulate_period(classified, regime, threshold, cost_bps, train_start, train_end,
                                        funding, regimes_preclassified=True)
            score, count = _score_train(events, min_events)
            candidate = (score, count, -threshold, -grid_index, threshold, regime)
            if best is None or candidate[:4] > best[:4]:
                best = candidate
    assert best is not None
    # If every candidate has too few observations, freeze the conservative
    # highest threshold with the first deterministic regime specification.
    threshold, regime = (200, fit_regime_parameters(train, REGIME_GRID[0])) if not np.isfinite(best[0]) else best[4:]
    return FrozenParameters(int(fold_id), pair, cost_bps, int(threshold), regime,
                            _utc(train_start), _utc(train_end), _utc(test_start), _utc(test_end))


def _bootstrap_ci(values: Iterable[float], samples: int, level: float, seed: int) -> tuple[float, float]:
    x = np.asarray(list(values), dtype=float)
    x = x[np.isfinite(x)]
    if not len(x) or samples == 0:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    means = rng.choice(x, size=(samples, len(x)), replace=True).mean(axis=1)
    alpha = (1 - level) / 2
    return tuple(float(v) for v in np.quantile(means, [alpha, 1 - alpha]))


def _summary_row(events: pd.DataFrame, diagnostics: dict, config: WalkForwardConfig, seed: int) -> dict:
    net = pd.to_numeric(events.get("net_pnl_bps", pd.Series(dtype=float)), errors="coerce").dropna()
    hold = pd.to_numeric(events.get("holding_minutes", pd.Series(dtype=float)), errors="coerce").dropna()
    mae = pd.to_numeric(events.get("mae_bps", pd.Series(dtype=float)), errors="coerce").dropna()
    mfe = pd.to_numeric(events.get("mfe_bps", pd.Series(dtype=float)), errors="coerce").dropna()
    low, high = _bootstrap_ci(net, config.bootstrap_samples, config.confidence_level, seed)
    return {
        "event_count": len(net), "mean_net_bps": net.mean() if len(net) else np.nan,
        "median_net_bps": net.median() if len(net) else np.nan,
        "win_rate": (net > 0).mean() if len(net) else np.nan,
        "mean_mae_bps": mae.mean() if len(mae) else np.nan,
        "median_mae_bps": mae.median() if len(mae) else np.nan,
        "mean_mfe_bps": mfe.mean() if len(mfe) else np.nan,
        "median_mfe_bps": mfe.median() if len(mfe) else np.nan,
        "mean_holding_minutes": hold.mean() if len(hold) else np.nan,
        "median_holding_minutes": hold.median() if len(hold) else np.nan,
        "capital_occupancy_rate": diagnostics.get("capital_occupancy_rate", np.nan),
        "same_pair_blocked_overlap_signal_count": int(
            diagnostics.get("same_pair_blocked_overlap_signal_count", 0)),
        "cross_pair_overlapping_event_count": int(
            diagnostics.get("cross_pair_overlapping_event_count", 0)),
        "bootstrap_mean_ci_low_bps": low, "bootstrap_mean_ci_high_bps": high,
        "bootstrap_confidence_level": config.confidence_level,
        "bootstrap_samples": config.bootstrap_samples,
    }


def _parameter_records(frozen: list[FrozenParameters]) -> pd.DataFrame:
    rows = []
    for p in frozen:
        row = asdict(p); regime = row.pop("regime"); row.update({f"regime_{k}": v for k, v in regime.items()})
        row.update({"execution_model": EXECUTION_MODEL, "selected_on": "TRAIN_ONLY",
                    "frozen_during_test": True})
        rows.append(row)
    return pd.DataFrame(rows)


def _lock_parameters(path: Path, parameters: pd.DataFrame) -> None:
    comparison = parameters.copy()
    for col in ["train_start", "train_end", "test_start", "test_end"]:
        comparison[col] = pd.to_datetime(comparison[col], utc=True).astype(str)
    keys = ["fold_id", "pair", "cost_bps", "test_start", "test_end"]
    if path.exists():
        old = pd.read_csv(path)
        shared = old.merge(comparison, on=keys, suffixes=("_old", "_new"))
        check_cols = ["threshold_bps", "regime_lookback_bars", "regime_persistence_ratio",
                      "regime_structural_abs_bps", "regime_transient_abs_bps"]
        for col in check_cols:
            if len(shared) and not np.allclose(pd.to_numeric(shared[f"{col}_old"]),
                                              pd.to_numeric(shared[f"{col}_new"]), equal_nan=True):
                raise RuntimeError("locked test-fold parameters changed; test-period reselection is forbidden")
    comparison.to_csv(path, index=False)


def _fold_boundaries(times: pd.DatetimeIndex, config: WalkForwardConfig):
    step = config.step_bars or config.test_bars
    fold_id, test_i = 1, config.train_bars
    while test_i < len(times):
        test_end_i = min(test_i + config.test_bars, len(times))
        if test_end_i <= test_i:
            break
        inferred_bar = times.to_series().diff().dropna().mode()
        bar = inferred_bar.iloc[0] if len(inferred_bar) else pd.Timedelta(minutes=15)
        yield fold_id, times[test_i-config.train_bars], times[test_i], times[test_i], times[test_end_i-1] + bar
        fold_id += 1
        test_i += step


def _cross_pair_overlap_count(events: pd.DataFrame) -> int:
    """Count realized events overlapping at least one event on another pair."""
    if len(events) < 2:
        return 0
    z = events.reset_index(drop=True).copy()
    starts = pd.to_datetime(z.entry_exec_time, utc=True)
    ends = pd.to_datetime(z.exit_exec_time, utc=True)
    overlaps = np.zeros(len(z), dtype=bool)
    for i in range(len(z)):
        other = z.pair.ne(z.pair.iloc[i])
        hit = other & starts.lt(ends.iloc[i]) & ends.gt(starts.iloc[i])
        overlaps[i] = bool(hit.any())
    return int(overlaps.sum())


def run_walk_forward(prices: pd.DataFrame, funding: pd.DataFrame | None = None,
                     config: WalkForwardConfig | None = None,
                     output_dir: str | Path | None = None) -> dict[str, pd.DataFrame | Path]:
    """Run and persist fold events, fold metrics, aggregate metrics and locks."""
    config = config or WalkForwardConfig()
    out = Path(output_dir or ROOT / "reports_high_threshold_walk_forward")
    out.mkdir(parents=True, exist_ok=True)
    pairs = build_pair_bars(prices)
    if not pairs:
        raise ValueError("no pair bars available")
    times = pd.DatetimeIndex(sorted(set.intersection(*(set(z.open_time) for z in pairs.values()))))
    if len(times) <= config.train_bars:
        raise ValueError("not enough common bars for one walk-forward fold")
    future_start = _utc(config.future_oos_start) if config.future_oos_start is not None else None
    all_events, fold_rows, frozen = [], [], []
    for fold_id, train_start, train_end, test_start, test_end in _fold_boundaries(times, config):
        oos_kind = TRUE_OOS if future_start is not None and test_start >= future_start else PSEUDO_OOS
        for pair, bars in pairs.items():
            train = bars[(bars.open_time >= train_start) & (bars.open_time < train_end)].copy()
            if train.empty:
                continue
            context = bars[bars.open_time < test_end].copy()
            # A fixed round-trip cost subtracts the same scalar from every
            # realized event, so it cannot change the ordering by mean return.
            # Select once on training at the first allowed cost, then attach the
            # identical train-selected parameters to all cost scenarios.
            selected = select_parameters(train, pair, fold_id, COSTS[0], train_start, train_end,
                                         test_start, test_end, funding, config.min_train_events)
            for cost in COSTS:
                locked = FrozenParameters(selected.fold_id, selected.pair, cost,
                                          selected.threshold_bps, selected.regime,
                                          selected.train_start, selected.train_end,
                                          selected.test_start, selected.test_end)
                frozen.append(locked)
                events, diagnostics = simulate_period(context, locked.regime, locked.threshold_bps,
                                                       cost, test_start, test_end, funding)
                events["fold_id"] = fold_id; events["oos_kind"] = oos_kind
                events["parameters_frozen"] = True
                all_events.append(events)
                row = {"fold_id": fold_id, "pair": pair, "cost_bps": cost,
                       "threshold_bps": locked.threshold_bps, "train_start": train_start,
                       "train_end": train_end, "test_start": test_start, "test_end": test_end,
                       "oos_kind": oos_kind, "execution_model": EXECUTION_MODEL,
                       "allowed_entry_regimes": "NORMAL|TRANSIENT_DISLOCATION",
                       "forbidden_entry_regimes": "STRUCTURAL_PREMIUM|STALE_OR_INVALID",
                       "parameters_selected_on": "TRAIN_ONLY", "parameters_frozen": True}
                row.update(_summary_row(events, diagnostics, config,
                                        config.random_seed + fold_id * 101 + cost))
                fold_rows.append(row)
    event_columns = ["pair", "signal_time", "signal_regime", "threshold_bps", "cost_bps",
                     "entry_exec_time", "exit_exec_time", "holding_minutes", "long_exchange",
                     "short_exchange", "gross_price_pnl_bps", "funding_pnl_bps", "gross_pnl_bps",
                     "net_pnl_bps", "mae_bps", "mfe_bps", "execution_model", "status",
                     "fold_id", "oos_kind", "parameters_frozen"]
    events_df = (pd.concat(all_events, ignore_index=True).reindex(columns=event_columns)
                 if all_events else pd.DataFrame(columns=event_columns))
    folds_df = pd.DataFrame(fold_rows)
    params_df = _parameter_records(frozen)
    _lock_parameters(out / "frozen_parameters.csv", params_df)
    events_df.to_csv(out / "fold_events.csv", index=False)
    folds_df.to_csv(out / "fold_results.csv", index=False)
    aggregate_rows = []
    for (pair, cost, kind), fold_subset in folds_df.groupby(["pair", "cost_bps", "oos_kind"], dropna=False):
        g = events_df[(events_df.pair == pair) & (events_df.cost_bps == cost) &
                      (events_df.oos_kind == kind)]
        occupied = float(fold_subset.capital_occupancy_rate.mean()) if len(fold_subset) else np.nan
        diagnostics = {"capital_occupancy_rate": occupied,
                       "same_pair_blocked_overlap_signal_count": int(
                           fold_subset.same_pair_blocked_overlap_signal_count.sum()),
                       "cross_pair_overlapping_event_count": 0}
        row = {"pair": pair, "cost_bps": cost, "oos_kind": kind,
               "execution_model": EXECUTION_MODEL, "fold_count": fold_subset.fold_id.nunique()}
        row.update(_summary_row(g, diagnostics, config, config.random_seed + int(cost)))
        aggregate_rows.append(row)
    for (cost, kind), fold_subset in folds_df.groupby(["cost_bps", "oos_kind"], dropna=False):
        g = events_df[(events_df.cost_bps == cost) & (events_df.oos_kind == kind)]
        diagnostics = {
            # Mean per-pair occupancy: one unit of capital capacity per pair.
            "capital_occupancy_rate": float(fold_subset.capital_occupancy_rate.mean()),
            "same_pair_blocked_overlap_signal_count": int(
                fold_subset.same_pair_blocked_overlap_signal_count.sum()),
            "cross_pair_overlapping_event_count": _cross_pair_overlap_count(g),
        }
        row = {"pair": "ALL_PAIRS", "cost_bps": cost, "oos_kind": kind,
               "execution_model": EXECUTION_MODEL, "fold_count": fold_subset.fold_id.nunique()}
        row.update(_summary_row(g, diagnostics, config, config.random_seed + int(cost) + 10_000))
        aggregate_rows.append(row)
    aggregate = pd.DataFrame(aggregate_rows)
    aggregate.to_csv(out / "aggregate_results.csv", index=False)
    manifest = {
        "execution_model": EXECUTION_MODEL, "thresholds_bps": list(THRESHOLDS),
        "costs_bps": list(COSTS), "allowed_entry_regimes": sorted(ALLOWED_ENTRY_REGIMES),
        "forbidden_entry_regimes": sorted(FORBIDDEN_ENTRY_REGIMES),
        "parameter_policy": "TRAIN_ONLY_THEN_FROZEN_FOR_TEST",
        "test_reselection_allowed": False,
        "historical_oos_label": PSEUDO_OOS, "future_oos_label": TRUE_OOS,
        "future_oos_start": str(future_start) if future_start is not None else None,
        "config": {k: (str(v) if isinstance(v, (pd.Timestamp,)) else v) for k, v in asdict(config).items()},
    }
    manifest["parameter_lock_sha256"] = hashlib.sha256((out / "frozen_parameters.csv").read_bytes()).hexdigest()
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (out / "README.md").write_text(
        "# High-threshold walk-forward\n\n"
        "所有历史测试 fold 均标为 `HISTORICAL_ROLLING_PSEUDO_OOS`；只有在参数已锁定后、"
        "且测试起点不早于显式 `future_oos_start` 的数据才标为 `FUTURE_TRUE_OOS`。\n\n"
        "参数只读取训练窗，测试窗冻结；同一测试窗禁止按测试结果重选。信号由 bar close 确认，"
        "仅使用下一根连续 bar open 作为成交代理。每个 pair 同时最多一个仓位。"
        "`same_pair_blocked_overlap_signal_count` 是持仓中被拦截的同 pair 信号；"
        "`cross_pair_overlapping_event_count` 是与其他 pair 仓位时间相交的已实现事件数。"
        "资金占用率以每个 pair 一单位容量计算。历史 K 线不是 BBO。\n"
    )
    return {"events": events_df, "folds": folds_df, "aggregate": aggregate,
            "parameters": params_df, "output_dir": out}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, default=ROOT / "data/normalized/prices_15m.parquet")
    parser.add_argument("--funding", type=Path, default=ROOT / "data/normalized/funding_events.parquet")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports_high_threshold_walk_forward")
    parser.add_argument("--train-bars", type=int, default=WalkForwardConfig.train_bars)
    parser.add_argument("--test-bars", type=int, default=WalkForwardConfig.test_bars)
    parser.add_argument("--step-bars", type=int)
    parser.add_argument("--bootstrap-samples", type=int, default=WalkForwardConfig.bootstrap_samples)
    parser.add_argument("--future-oos-start", help="UTC lock boundary; omit for historical pseudo-OOS only")
    args = parser.parse_args(argv)
    cfg = WalkForwardConfig(train_bars=args.train_bars, test_bars=args.test_bars,
                            step_bars=args.step_bars, bootstrap_samples=args.bootstrap_samples,
                            future_oos_start=args.future_oos_start)
    result = run_walk_forward(pd.read_parquet(args.prices),
                              pd.read_parquet(args.funding) if args.funding.exists() else None,
                              cfg, args.output_dir)
    print(f"folds={len(result['folds'])}; events={len(result['events'])}; output={result['output_dir']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
