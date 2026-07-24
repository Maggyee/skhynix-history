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
from .gate_regime_15m import build_causal_regime_labels

EXECUTION_MODEL = "NEXT_BAR_OPEN_PROXY"
THRESHOLDS = (100, 150, 200)
COSTS = (20, 40, 80)
ALLOWED_ENTRY_REGIMES = frozenset({"NORMAL", "TRANSIENT_DISLOCATION"})
FORBIDDEN_ENTRY_REGIMES = frozenset({"STRUCTURAL_PREMIUM", "STALE_OR_INVALID"})
PSEUDO_OOS = "HISTORICAL_ROLLING_PSEUDO_OOS"
TRUE_OOS = "FUTURE_TRUE_OOS"
GATE_REGIME_POLICY = "GATE_CAUSAL_ALLOWED_NORMAL_TRANSIENT"
NON_GATE_REGIME_POLICY = "NO_GATE_REGIME_FILTER"
EVENT_STATUSES = (
    "REALIZED", "RIGHT_CENSORED", "NO_NEXT_BAR_FOR_ENTRY", "NO_NEXT_BAR_FOR_EXIT",
    "DATA_GAP_DURING_HOLD", "INVALID_PRICE",
)


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
    min_train_events: int = 10
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
                    *, regimes_preclassified: bool = False,
                    regime_policy: str = "LEGACY_PAIR_CAUSAL") -> tuple[pd.DataFrame, dict]:
    """Run one fixed scenario and retain every eligible signal outcome."""
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

    def event(status, signal_i, **values):
        row = {
            "pair": pair, "signal_time": times[signal_i], "signal_regime": regimes[signal_i],
            "threshold_bps": threshold_bps, "cost_bps": cost_bps,
            "entry_exec_time": pd.NaT, "exit_exec_time": pd.NaT,
            "holding_minutes": np.nan, "long_exchange": None, "short_exchange": None,
            "gross_price_pnl_bps": np.nan, "funding_pnl_bps": np.nan,
            "gross_pnl_bps": np.nan, "net_pnl_bps": np.nan,
            "mae_bps": np.nan, "mfe_bps": np.nan,
            "execution_model": EXECUTION_MODEL, "regime_policy": regime_policy, "status": status,
        }
        row.update(values)
        rows.append(row)

    i = int(times.searchsorted(start))
    stop = int(times.searchsorted(end))
    while i < stop:
        regime_allowed = (regime_policy == NON_GATE_REGIME_POLICY or
                          regimes[i] in ALLOWED_ENTRY_REGIMES)
        eligible = abs_spreads[i] >= threshold_bps and regime_allowed
        if not eligible:
            i += 1
            continue
        entry_i = i + 1
        if entry_i >= stop:
            event("NO_NEXT_BAR_FOR_ENTRY", i)
            i += 1
            continue
        if times[entry_i] != times[i] + bar:
            event("NO_NEXT_BAR_FOR_ENTRY", i)
            i += 1
            continue
        positive = spreads[i] > 0
        long_ex, short_ex = (b, a) if positive else (a, b)
        entry_long = open_b[entry_i] if positive else open_a[entry_i]
        entry_short = open_a[entry_i] if positive else open_b[entry_i]
        if not np.isfinite([entry_long, entry_short]).all() or min(entry_long, entry_short) <= 0:
            event("INVALID_PRICE", i)
            i += 1
            continue
        exit_signal_i = None
        gap_during_hold = False
        j = entry_i
        while j < stop:
            if j > entry_i and abs_spreads[j] >= threshold_bps:
                overlap_signals += 1
            if times[j] != times[entry_i] + (j - entry_i) * bar:
                gap_during_hold = True
                break
            if abs_spreads[j] < threshold_bps:
                exit_signal_i = j
                break
            j += 1
        exit_i = exit_signal_i + 1 if exit_signal_i is not None else None
        if gap_during_hold:
            event("DATA_GAP_DURING_HOLD", i, entry_exec_time=times[entry_i],
                  long_exchange=long_ex, short_exchange=short_ex)
            i = max(i + 1, j)
            continue
        if exit_i is None:
            event("RIGHT_CENSORED", i, entry_exec_time=times[entry_i],
                  long_exchange=long_ex, short_exchange=short_ex)
            i = stop
            continue
        if exit_i >= stop or times[exit_i] != times[exit_signal_i] + bar:
            event("NO_NEXT_BAR_FOR_EXIT", i, entry_exec_time=times[entry_i],
                  long_exchange=long_ex, short_exchange=short_ex)
            i = max(i + 1, exit_signal_i + 1)
            continue
        exit_long = open_b[exit_i] if positive else open_a[exit_i]
        exit_short = open_a[exit_i] if positive else open_b[exit_i]
        if not np.isfinite([exit_long, exit_short]).all() or min(exit_long, exit_short) <= 0:
            event("INVALID_PRICE", i, entry_exec_time=times[entry_i],
                  exit_exec_time=times[exit_i], long_exchange=long_ex, short_exchange=short_ex)
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
        event("REALIZED", i, entry_exec_time=times[entry_i], exit_exec_time=times[exit_i],
              holding_minutes=holding, long_exchange=long_ex, short_exchange=short_ex,
              gross_price_pnl_bps=gross_price, funding_pnl_bps=funding_pnl,
              gross_pnl_bps=gross, net_pnl_bps=gross-cost_bps,
              mae_bps=float(path.min()) if len(path) else np.nan,
              mfe_bps=float(path.max()) if len(path) else np.nan)
        # The next signal may only be considered after this position has exited.
        i = exit_i
    duration_ns = max(0, (end - start).value)
    diagnostics = {
        "capital_occupancy_rate": occupied_ns / duration_ns if duration_ns else np.nan,
        "same_pair_blocked_overlap_signal_count": int(overlap_signals),
        "cross_pair_overlapping_event_count": 0,
    }
    events = pd.DataFrame(rows)
    if len(events) and not set(events.status) <= set(EVENT_STATUSES):
        raise AssertionError("unexpected event status")
    return events, diagnostics


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


def _day_block_bootstrap_ci(events: pd.DataFrame, samples: int, level: float,
                            seed: int) -> tuple[float, float]:
    realized = events[events.get("status", pd.Series(index=events.index, dtype=object)).eq("REALIZED")].copy()
    if realized.empty or samples == 0:
        return np.nan, np.nan
    realized["event_day"] = pd.to_datetime(realized.signal_time, utc=True).dt.floor("D")
    blocks = [pd.to_numeric(g.net_pnl_bps, errors="coerce").dropna().to_numpy()
              for _, g in realized.groupby("event_day")]
    blocks = [b for b in blocks if len(b)]
    if not blocks:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    means = []
    for _ in range(samples):
        picked = rng.integers(0, len(blocks), size=len(blocks))
        sample = np.concatenate([blocks[i] for i in picked])
        means.append(sample.mean())
    alpha = (1 - level) / 2
    return tuple(float(v) for v in np.quantile(means, [alpha, 1 - alpha]))


def _summary_row(events: pd.DataFrame, diagnostics: dict, config: WalkForwardConfig, seed: int) -> dict:
    status = events.get("status", pd.Series(index=events.index, dtype=object))
    realized = events[status.eq("REALIZED")]
    net = pd.to_numeric(realized.get("net_pnl_bps", pd.Series(dtype=float)), errors="coerce").dropna()
    hold = pd.to_numeric(realized.get("holding_minutes", pd.Series(dtype=float)), errors="coerce").dropna()
    mae = pd.to_numeric(realized.get("mae_bps", pd.Series(dtype=float)), errors="coerce").dropna()
    mfe = pd.to_numeric(realized.get("mfe_bps", pd.Series(dtype=float)), errors="coerce").dropna()
    naive_low, naive_high = _bootstrap_ci(net, config.bootstrap_samples, config.confidence_level, seed)
    block_low, block_high = _day_block_bootstrap_ci(
        realized, config.bootstrap_samples, config.confidence_level, seed + 1
    )
    total = len(events); censored = total - len(realized)
    return {
        "total_signal_count": total, "realized_event_count": len(realized),
        "censored_event_count": censored, "censor_rate": censored / total if total else np.nan,
        "event_count": len(net), "mean_net_bps": net.mean() if len(net) else np.nan,
        "median_net_bps": net.median() if len(net) else np.nan,
        "win_rate": (net > 0).mean() if len(net) else np.nan,
        "mae_bps": mae.mean() if len(mae) else np.nan,
        "mfe_bps": mfe.mean() if len(mfe) else np.nan,
        "holding_minutes": hold.mean() if len(hold) else np.nan,
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
        "naive_event_bootstrap_ci_low": naive_low,
        "naive_event_bootstrap_ci_high": naive_high,
        "block_bootstrap_ci_low": block_low,
        "block_bootstrap_ci_high": block_high,
        "bootstrap_mean_ci_low_bps": block_low, "bootstrap_mean_ci_high_bps": block_high,
        "bootstrap_method_primary": "DAY_BLOCK_BOOTSTRAP",
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
    events = events[events.get("status", pd.Series(index=events.index, dtype=object)).eq("REALIZED")]
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
    """Run fixed 100/150/200 bps scenarios without cross-threshold mixing."""
    config = config or WalkForwardConfig()
    out = Path(output_dir or ROOT / "reports_high_threshold_walk_forward")
    out.mkdir(parents=True, exist_ok=True)
    pairs = build_pair_bars(prices)
    if not pairs:
        raise ValueError("no pair bars available")
    times = pd.DatetimeIndex(sorted(set.intersection(*(set(z.open_time) for z in pairs.values()))))
    if len(times) <= config.train_bars:
        raise ValueError("not enough common bars for one walk-forward fold")
    # Current history is always pseudo-OOS.  Merely passing a date at runtime
    # cannot retroactively prove that parameters were locked before that date.
    future_start = None
    has_gate_pair = any("gate" in pair.split("/") for pair in pairs)
    causal = (build_causal_regime_labels(prices)[["open_time", "causal_regime"]]
              if has_gate_pair else pd.DataFrame(columns=["open_time", "causal_regime"]))
    default_params = RegimeParameters(96, .80, 50.0, 100.0)
    all_events, fold_rows, parameter_rows = [], [], []
    for fold_id, train_start, train_end, test_start, test_end in _fold_boundaries(times, config):
        oos_kind = PSEUDO_OOS
        for pair, bars in pairs.items():
            gate_pair = "gate" in pair.split("/")
            regime_policy = GATE_REGIME_POLICY if gate_pair else NON_GATE_REGIME_POLICY
            pair_scope = "GATE_PAIRS" if gate_pair else "NON_GATE_PAIRS"
            classified = bars.merge(causal, on="open_time", how="left") if gate_pair else bars.copy()
            classified["regime"] = (classified.pop("causal_regime").fillna("STALE_OR_INVALID")
                                    if gate_pair else NON_GATE_REGIME_POLICY)
            train = classified[(classified.open_time >= train_start) &
                               (classified.open_time < train_end)].copy()
            if train.empty:
                continue
            # Explicit test warm-up: retain the final causal lookback from the
            # training window, but simulate_period only admits signals >= test_start.
            context_start = train_end - pd.Timedelta(minutes=15 * default_params.lookback_bars)
            context = classified[(classified.open_time >= context_start) &
                                 (classified.open_time < test_end)].copy()
            for threshold in THRESHOLDS:
                for cost in COSTS:
                    train_events, _ = simulate_period(
                        classified, default_params, threshold, cost, train_start, train_end,
                        funding, regimes_preclassified=True, regime_policy=regime_policy
                    )
                    train_realized = int(train_events.status.eq("REALIZED").sum()) if len(train_events) else 0
                    train_status = ("OK" if train_realized >= config.min_train_events
                                    else "INSUFFICIENT_TRAIN_SAMPLE")
                    events, diagnostics = simulate_period(
                        context, default_params, threshold, cost, test_start, test_end, funding,
                        regimes_preclassified=True, regime_policy=regime_policy
                    )
                    events["fold_id"] = fold_id; events["oos_kind"] = oos_kind
                    events["parameters_frozen"] = True; events["pair_scope"] = pair_scope
                    all_events.append(events)
                    parameter_rows.append({
                        "fold_id": fold_id, "pair": pair, "threshold_bps": threshold,
                        "cost_bps": cost, "regime_policy": regime_policy,
                        "scenario_scope": "FIXED_THRESHOLD_MAIN",
                        "selected_on": "NOT_APPLICABLE_FIXED_SCENARIO",
                        "train_sample_status": train_status, "train_event_count": train_realized,
                        "test_start": test_start, "test_end": test_end,
                    })
                    row = {"fold_id": fold_id, "pair": pair, "pair_scope": pair_scope,
                       "cost_bps": cost, "threshold_bps": threshold,
                       "regime_policy": regime_policy, "train_start": train_start,
                       "train_end": train_end, "test_start": test_start, "test_end": test_end,
                       "oos_kind": oos_kind, "execution_model": EXECUTION_MODEL,
                       "allowed_entry_regimes": ("NORMAL|TRANSIENT_DISLOCATION" if gate_pair
                                                  else "NOT_APPLICABLE"),
                       "forbidden_entry_regimes": ("STRUCTURAL_PREMIUM|STALE_OR_INVALID" if gate_pair
                                                    else "NOT_APPLICABLE"),
                       "train_event_count": train_realized, "train_sample_status": train_status,
                       "parameters_selected_on": "NOT_APPLICABLE_FIXED_SCENARIO",
                       "parameters_frozen": True}
                    row.update(_summary_row(events, diagnostics, config,
                                            config.random_seed + fold_id * 1009 + threshold + cost))
                    row["total_test_signals"] = row["total_signal_count"]
                    fold_rows.append(row)
    event_columns = ["pair", "signal_time", "signal_regime", "threshold_bps", "cost_bps",
                     "entry_exec_time", "exit_exec_time", "holding_minutes", "long_exchange",
                     "short_exchange", "gross_price_pnl_bps", "funding_pnl_bps", "gross_pnl_bps",
                     "net_pnl_bps", "mae_bps", "mfe_bps", "execution_model", "status",
                     "regime_policy", "fold_id", "oos_kind", "parameters_frozen", "pair_scope"]
    events_df = (pd.concat(all_events, ignore_index=True).reindex(columns=event_columns)
                 if all_events else pd.DataFrame(columns=event_columns))
    folds_df = pd.DataFrame(fold_rows)
    params_df = pd.DataFrame(parameter_rows)
    params_df.to_csv(out / "frozen_parameters.csv", index=False)
    events_df.to_csv(out / "fold_events.csv", index=False)
    folds_df.to_csv(out / "fold_results.csv", index=False)
    aggregate_rows = []
    group_cols = ["pair", "threshold_bps", "cost_bps", "regime_policy", "oos_kind",
                  "execution_model", "pair_scope"]
    for keys, fold_subset in folds_df.groupby(group_cols, dropna=False):
        pair, threshold, cost, policy, kind, execution, pair_scope = keys
        g = events_df[(events_df.pair == pair) & (events_df.threshold_bps == threshold) &
                      (events_df.cost_bps == cost) & (events_df.oos_kind == kind)]
        occupied = float(fold_subset.capital_occupancy_rate.mean()) if len(fold_subset) else np.nan
        diagnostics = {"capital_occupancy_rate": occupied,
                       "same_pair_blocked_overlap_signal_count": int(
                           fold_subset.same_pair_blocked_overlap_signal_count.sum()),
                       "cross_pair_overlapping_event_count": 0}
        row = {"pair": pair, "threshold_bps": threshold, "cost_bps": cost,
               "regime_policy": policy, "oos_kind": kind, "execution_model": execution,
               "pair_scope": pair_scope, "fold_count": fold_subset.fold_id.nunique(),
               "insufficient_train_fold_count": int(fold_subset.train_sample_status.eq(
                   "INSUFFICIENT_TRAIN_SAMPLE").sum())}
        row.update(_summary_row(g, diagnostics, config, config.random_seed + int(cost)))
        aggregate_rows.append(row)
    for (pair_scope, threshold, cost, policy, kind), fold_subset in folds_df.groupby(
            ["pair_scope", "threshold_bps", "cost_bps", "regime_policy", "oos_kind"],
            dropna=False):
        g = events_df[(events_df.pair_scope == pair_scope) &
                      (events_df.threshold_bps == threshold) &
                      (events_df.cost_bps == cost) & (events_df.oos_kind == kind)]
        diagnostics = {
            # Mean per-pair occupancy: one unit of capital capacity per pair.
            "capital_occupancy_rate": float(fold_subset.capital_occupancy_rate.mean()),
            "same_pair_blocked_overlap_signal_count": int(
                fold_subset.same_pair_blocked_overlap_signal_count.sum()),
            "cross_pair_overlapping_event_count": _cross_pair_overlap_count(g),
        }
        row = {"pair": f"ALL_{pair_scope}", "threshold_bps": threshold, "cost_bps": cost,
               "regime_policy": policy, "oos_kind": kind,
               "execution_model": EXECUTION_MODEL, "pair_scope": pair_scope,
               "fold_count": fold_subset.fold_id.nunique(),
               "insufficient_train_fold_count": int(fold_subset.train_sample_status.eq(
                   "INSUFFICIENT_TRAIN_SAMPLE").sum())}
        row.update(_summary_row(g, diagnostics, config, config.random_seed + int(cost) + 10_000))
        aggregate_rows.append(row)
    aggregate = pd.DataFrame(aggregate_rows)
    aggregate.to_csv(out / "aggregate_results.csv", index=False)
    manifest = {
        "execution_model": EXECUTION_MODEL, "thresholds_bps": list(THRESHOLDS),
        "costs_bps": list(COSTS), "allowed_entry_regimes": sorted(ALLOWED_ENTRY_REGIMES),
        "forbidden_entry_regimes": sorted(FORBIDDEN_ENTRY_REGIMES),
        "parameter_policy": "FIXED_THRESHOLD_MAIN_NO_THRESHOLD_SELECTION",
        "secondary_train_selected_threshold": False,
        "test_reselection_allowed": False,
        "historical_oos_label": PSEUDO_OOS, "future_oos_label": TRUE_OOS,
        "future_oos_start": None, "future_true_oos_enabled": False,
        "config": {k: (str(v) if isinstance(v, (pd.Timestamp,)) else v) for k, v in asdict(config).items()},
    }
    manifest["parameter_lock_sha256"] = hashlib.sha256((out / "frozen_parameters.csv").read_bytes()).hexdigest()
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (out / "README.md").write_text(
        "# High-threshold walk-forward\n\n"
        "所有历史测试 fold 均标为 `HISTORICAL_ROLLING_PSEUDO_OOS`；只有在参数已锁定后、"
        "且测试起点不早于显式 `future_oos_start` 的数据才标为 `FUTURE_TRUE_OOS`。\n\n"
        "主结果将100/150/200 bps作为三个独立固定场景，不进行跨阈值训练选择。Gate pair使用"
        "Gate因果标签，非Gate pair明确标为NO_GATE_REGIME_FILTER。测试窗使用训练末尾lookback"
        "作为warm-up，但只允许测试期信号。信号由 bar close 确认，"
        "仅使用下一根连续 bar open 作为成交代理。每个 pair 同时最多一个仓位。"
        "`same_pair_blocked_overlap_signal_count` 是持仓中被拦截的同 pair 信号；"
        "`cross_pair_overlapping_event_count` 是与其他 pair 仓位时间相交的已实现事件数。"
        "资金占用率以每个 pair 一单位容量计算。未退出及无下一根bar的事件不会被删除；"
        "主置信区间为day-block bootstrap，同时输出naive event bootstrap。历史 K 线不是 BBO。\n"
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
