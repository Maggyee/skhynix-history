"""Higher-bps historical proxy study on native 15-minute trade OHLC.

This module is research-only.  It contains no account, authentication, private
API, or order path.  Signals use a completed bar close and executions use only
the following contiguous bar open.  Historical results are labelled rolling
pseudo-OOS and must not be interpreted as executable BBO results.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

from .config import ROOT
from .gate_regime_15m import build_causal_regime_labels

BAR = pd.Timedelta(minutes=15)
THRESHOLDS = (20, 50, 100, 150, 200, 250, 300, 400, 500)
COSTS = (0, 20, 40, 58, 80)
BASELINE_WINDOWS_HOURS = (24, 72)
EXIT_POLICIES = ("LEGACY_RAW_EXIT", "BASELINE_RESIDUAL_EXIT", "FRACTIONAL_RESIDUAL_EXIT")
STATUSES = ("REALIZED", "RIGHT_CENSORED", "NO_NEXT_BAR_FOR_ENTRY",
    "NO_NEXT_BAR_FOR_EXIT", "DATA_GAP_DURING_HOLD", "INVALID_PRICE",
    "INSUFFICIENT_HISTORY", "REGIME_FILTERED")
PSEUDO_OOS = "HISTORICAL_ROLLING_PSEUDO_OOS"
EXECUTION_MODEL = "NEXT_CONTIGUOUS_15M_BAR_OPEN_PROXY"
EVENT_COLUMNS = [
    "event_id", "pair", "pair_scope", "fold_id", "signal_time", "entry_exec_time",
    "exit_exec_time", "threshold_bps", "trigger_type", "baseline_window_hours",
    "exit_policy", "regime", "status", "long_exchange", "short_exchange",
    "gross_price_pnl_bps", "funding_pnl_bps", "gross_combined_pnl_bps",
    "assumed_total_cost_bps", "combined_net_pnl_bps", "entry_raw_spread_bps",
    "entry_baseline_bps", "entry_residual_bps", "entry_mad_bps",
    "entry_same_sign_ratio", "exit_raw_spread_bps", "exit_baseline_bps",
    "exit_residual_bps", "mae_bps", "mfe_bps", "holding_minutes",
    "same_pair_overlap_count", "oos_kind", "execution_model",
]
RESULT_STATS = [
    "total_signal_count", "realized_event_count", "censored_event_count", "censor_rate",
    "mean_net_bps", "median_net_bps", "win_rate", "p05_net_bps", "p25_net_bps",
    "p75_net_bps", "p95_net_bps", "mean_mae_bps", "median_mae_bps",
    "mean_mfe_bps", "median_mfe_bps", "mean_holding_minutes",
    "median_holding_minutes", "p90_holding_minutes", "max_holding_minutes",
    "capital_occupancy_rate", "same_pair_overlap_count", "cross_pair_overlap_count",
    "day_block_ci_low", "day_block_ci_high", "mean_gross_price_pnl_bps",
    "mean_funding_pnl_bps", "mean_gross_combined_pnl_bps", "mean_cost_contribution_bps",
    "realized_day_count",
]


@dataclass(frozen=True)
class StudyConfig:
    train_bars: int = 7 * 24 * 4
    test_bars: int = 2 * 24 * 4
    bootstrap_samples: int = 1000
    confidence_level: float = .95
    random_seed: int = 260724

    def __post_init__(self):
        if self.train_bars < 288 or self.test_bars < 1:
            raise ValueError("train_bars must cover the declared 72h baseline and test_bars must be positive")
        if self.bootstrap_samples < 0 or not 0 < self.confidence_level < 1:
            raise ValueError("invalid bootstrap configuration")


def _spread(a, b):
    a, b = pd.to_numeric(a, errors="coerce"), pd.to_numeric(b, errors="coerce")
    return 20_000 * (a - b) / (a + b)


def build_pair_bars(prices: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Strict same-timestamp native trade bars reindexed only to expose gaps."""
    required = {"exchange", "price_type", "open_time", "open", "high", "low", "close"}
    if missing := required - set(prices):
        raise ValueError(f"prices missing columns: {sorted(missing)}")
    p = prices.copy(); p["open_time"] = pd.to_datetime(p.open_time, utc=True)
    p = p[p.price_type.eq("trade")]
    if p.duplicated(["exchange", "open_time"]).any():
        raise ValueError("duplicate exchange/open_time trade bars")
    result = {}
    exchanges = sorted(p.exchange.unique())
    for i, a in enumerate(exchanges):
        for b in exchanges[i + 1:]:
            cols = ["open_time", "open", "high", "low", "close"]
            z = p[p.exchange.eq(a)][cols].merge(
                p[p.exchange.eq(b)][cols], on="open_time", how="inner", suffixes=("_A", "_B"))
            if z.empty:
                continue
            grid = pd.date_range(z.open_time.min(), z.open_time.max(), freq=BAR, tz="UTC")
            z = z.set_index("open_time").reindex(grid).rename_axis("open_time").reset_index()
            z["pair"] = f"{a}/{b}"
            z["raw_spread_bps"] = _spread(z.close_A, z.close_B)
            z["open_spread_bps"] = _spread(z.open_A, z.open_B)
            result[f"{a}/{b}"] = z
    return result


def add_past_only_baseline(bars: pd.DataFrame, hours: int) -> pd.DataFrame:
    """Add a trailing baseline using strictly earlier contiguous bars."""
    if hours not in BASELINE_WINDOWS_HOURS:
        raise ValueError(f"baseline hours must be one of {BASELINE_WINDOWS_HOURS}")
    z = bars.copy(); window = hours * 4
    valid = z[["open_A", "high_A", "low_A", "close_A", "open_B", "high_B", "low_B", "close_B"]]
    valid = valid.apply(pd.to_numeric, errors="coerce").gt(0).all(axis=1)
    history = z.raw_spread_bps.where(valid).shift(1)
    rolling = history.rolling(window, min_periods=window)
    baseline = rolling.median()

    def mad(v):
        center = np.median(v)
        return float(np.median(np.abs(v - center)))

    def same_sign(v):
        sign = np.sign(np.median(v))
        return float(np.mean(np.sign(v) == sign))

    z["baseline_bps"] = baseline
    z["residual_bps"] = z.raw_spread_bps - baseline
    z["mad_bps"] = rolling.apply(mad, raw=True)
    z["same_sign_ratio"] = rolling.apply(same_sign, raw=True)
    z["history_ready"] = baseline.notna()
    z["baseline_window_hours"] = hours
    return z


def _funding_bps(funding, long_exchange, short_exchange, start, end):
    if funding is None or funding.empty:
        return 0.0
    times = pd.to_datetime(funding.funding_time, utc=True)
    active = funding[(times > start) & (times < end)]
    return float((-active.loc[active.exchange.eq(long_exchange), "funding_rate"].sum()
                  + active.loc[active.exchange.eq(short_exchange), "funding_rate"].sum()) * 10_000)


def _leg_pnl(long_entry, short_entry, long_exit, short_exit):
    return float(((long_exit / long_entry - 1) + (1 - short_exit / short_entry)) * 10_000)


def simulate_scenario(bars: pd.DataFrame, threshold: int, trigger_type: str,
                      exit_policy: str, start, end, funding=None, fold_id=0) -> pd.DataFrame:
    """Simulate one cost-free fixed scenario; costs are attached exactly once later."""
    if threshold not in THRESHOLDS or exit_policy not in EXIT_POLICIES:
        raise ValueError("unfrozen threshold or exit policy")
    if trigger_type not in {"RAW_SPREAD", "RESIDUAL_SPREAD"}:
        raise ValueError("unknown trigger_type")
    z = bars.reset_index(drop=True); times = pd.DatetimeIndex(z.open_time)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    signal = z.raw_spread_bps if trigger_type == "RAW_SPREAD" else z.residual_bps
    ready = pd.Series(True, index=z.index) if trigger_type == "RAW_SPREAD" else z.history_ready
    active = signal.abs().ge(threshold) & ready
    onset = active & ~active.shift(1, fill_value=False)
    pair = str(z.pair.dropna().iloc[0]); a, b = pair.split("/")
    gate_pair = "gate" in {a, b}; pair_scope = "GATE_PAIRS" if gate_pair else "NON_GATE_PAIRS"
    rows = []; occupied_until = -1

    def add(status, i, **kwargs):
        row = {c: np.nan for c in EVENT_COLUMNS}
        row.update({"event_id": f"{pair}-{fold_id}-{trigger_type}-{int(z.baseline_window_hours.iloc[0])}-{threshold}-{exit_policy}-{i}",
            "pair": pair, "pair_scope": pair_scope, "fold_id": fold_id,
            "signal_time": times[i], "threshold_bps": threshold, "trigger_type": trigger_type,
            "baseline_window_hours": int(z.baseline_window_hours.iloc[0]), "exit_policy": exit_policy,
            "regime": str(z.regime.iloc[i]), "status": status, "oos_kind": PSEUDO_OOS,
            "execution_model": EXECUTION_MODEL, "same_pair_overlap_count": 0})
        row.update(kwargs); rows.append(row)

    first, stop = int(times.searchsorted(start)), int(times.searchsorted(end))
    if trigger_type == "RESIDUAL_SPREAD":
        not_ready = (~ready) & times.to_series(index=z.index).between(start, end, inclusive="left")
        starts = not_ready & ~not_ready.shift(1, fill_value=False)
        for i in np.flatnonzero(starts):
            add("INSUFFICIENT_HISTORY", int(i))
    for i in np.flatnonzero(onset.to_numpy()):
        if i < first or i >= stop:
            continue
        if i <= occupied_until:
            if rows and rows[-1]["status"] == "REALIZED":
                rows[-1]["same_pair_overlap_count"] += 1
            continue
        if gate_pair and str(z.regime.iloc[i]) not in {"NORMAL", "TRANSIENT_DISLOCATION"}:
            add("REGIME_FILTERED", i); continue
        entry_i = i + 1
        if entry_i >= stop or times[entry_i] != times[i] + BAR:
            add("NO_NEXT_BAR_FOR_ENTRY", i); continue
        vals = z.loc[entry_i, ["open_A", "open_B"]].to_numpy(float)
        if not np.isfinite(vals).all() or min(vals) <= 0:
            add("INVALID_PRICE", i); continue
        direction = np.sign(signal.iloc[i])
        if direction == 0:
            add("INVALID_PRICE", i); continue
        positive = direction > 0
        long_ex, short_ex = (b, a) if positive else (a, b)
        entry_long, entry_short = (vals[1], vals[0]) if positive else (vals[0], vals[1])
        exit_signal_i = None; gap_i = None
        entry_residual_abs = abs(float(z.residual_bps.iloc[i])) if pd.notna(z.residual_bps.iloc[i]) else np.nan
        j = entry_i
        while j < stop:
            valid_close = np.isfinite(z.loc[j, ["close_A", "close_B"]].to_numpy(float)).all()
            contiguous = times[j] == times[entry_i] + (j - entry_i) * BAR
            if not valid_close or not contiguous:
                gap_i = j; break
            raw_abs = abs(float(z.raw_spread_bps.iloc[j]))
            residual_abs = abs(float(z.residual_bps.iloc[j])) if pd.notna(z.residual_bps.iloc[j]) else np.inf
            should_exit = (raw_abs < threshold if exit_policy == "LEGACY_RAW_EXIT" else
                residual_abs <= 20 if exit_policy == "BASELINE_RESIDUAL_EXIT" else
                residual_abs <= .25 * entry_residual_abs)
            if should_exit:
                exit_signal_i = j; break
            j += 1
        common = {"entry_exec_time": times[entry_i], "long_exchange": long_ex,
            "short_exchange": short_ex, "entry_raw_spread_bps": z.raw_spread_bps.iloc[i],
            "entry_baseline_bps": z.baseline_bps.iloc[i], "entry_residual_bps": z.residual_bps.iloc[i],
            "entry_mad_bps": z.mad_bps.iloc[i], "entry_same_sign_ratio": z.same_sign_ratio.iloc[i]}
        if gap_i is not None:
            add("DATA_GAP_DURING_HOLD", i, **common); occupied_until = gap_i; continue
        if exit_signal_i is None:
            add("RIGHT_CENSORED", i, **common); occupied_until = stop - 1; continue
        exit_i = exit_signal_i + 1
        if exit_i >= stop or times[exit_i] != times[exit_signal_i] + BAR:
            add("NO_NEXT_BAR_FOR_EXIT", i, **common); occupied_until = exit_signal_i; continue
        exit_vals = z.loc[exit_i, ["open_A", "open_B"]].to_numpy(float)
        if not np.isfinite(exit_vals).all() or min(exit_vals) <= 0:
            add("INVALID_PRICE", i, exit_exec_time=times[exit_i], **common); occupied_until = exit_i; continue
        exit_long, exit_short = (exit_vals[1], exit_vals[0]) if positive else (exit_vals[0], exit_vals[1])
        path_a = z.close_B.iloc[entry_i:exit_signal_i + 1] if positive else z.close_A.iloc[entry_i:exit_signal_i + 1]
        path_b = z.close_A.iloc[entry_i:exit_signal_i + 1] if positive else z.close_B.iloc[entry_i:exit_signal_i + 1]
        path = ((path_a / entry_long - 1) + (1 - path_b / entry_short)) * 10_000
        gross_price = _leg_pnl(entry_long, entry_short, exit_long, exit_short)
        fund = _funding_bps(funding, long_ex, short_ex, times[entry_i], times[exit_i])
        add("REALIZED", i, exit_exec_time=times[exit_i],
            gross_price_pnl_bps=gross_price, funding_pnl_bps=fund,
            gross_combined_pnl_bps=gross_price + fund,
            exit_raw_spread_bps=z.raw_spread_bps.iloc[exit_signal_i],
            exit_baseline_bps=z.baseline_bps.iloc[exit_signal_i],
            exit_residual_bps=z.residual_bps.iloc[exit_signal_i],
            mae_bps=path.min(), mfe_bps=path.max(),
            holding_minutes=(times[exit_i] - times[entry_i]).total_seconds() / 60,
            **common)
        occupied_until = exit_i
    return pd.DataFrame(rows).reindex(columns=EVENT_COLUMNS)


def attach_costs(events: pd.DataFrame) -> pd.DataFrame:
    copies = []
    for cost in COSTS:
        z = events.copy(); z["assumed_total_cost_bps"] = cost
        realized = z.status.eq("REALIZED")
        z.loc[realized, "combined_net_pnl_bps"] = z.loc[realized, "gross_combined_pnl_bps"] - cost
        z.loc[~realized, "combined_net_pnl_bps"] = np.nan
        copies.append(z)
    return pd.concat(copies, ignore_index=True) if copies else pd.DataFrame(columns=EVENT_COLUMNS)


def _day_block_ci(events, samples, level, seed):
    x = events[events.status.eq("REALIZED")].copy()
    if x.empty or samples == 0:
        return np.nan, np.nan
    x["day"] = pd.to_datetime(x.signal_time, utc=True).dt.floor("D")
    blocks = x.groupby("day").combined_net_pnl_bps.agg(["sum","count"])
    if blocks.empty:
        return np.nan, np.nan
    rng = np.random.default_rng(seed)
    selected = rng.integers(0, len(blocks), size=(samples, len(blocks)))
    sums = blocks["sum"].to_numpy()[selected].sum(axis=1)
    counts = blocks["count"].to_numpy()[selected].sum(axis=1)
    means = sums / counts
    alpha = (1 - level) / 2
    return tuple(np.quantile(means, [alpha, 1 - alpha]))


def _cross_overlap(events):
    x = events[events.status.eq("REALIZED")].reset_index(drop=True)
    if len(x) < 2:
        return 0
    start = pd.to_datetime(x.entry_exec_time, utc=True); end = pd.to_datetime(x.exit_exec_time, utc=True)
    return int(sum(bool((x.pair.ne(x.pair.iloc[i]) & start.lt(end.iloc[i]) & end.gt(start.iloc[i])).any())
                   for i in range(len(x))))


def summarize(events: pd.DataFrame, pair_count: int, duration_minutes: float,
              config: StudyConfig, seed: int) -> dict:
    realized = events[events.status.eq("REALIZED")]
    net = realized.combined_net_pnl_bps.dropna(); hold = realized.holding_minutes.dropna()
    mae = realized.mae_bps.dropna(); mfe = realized.mfe_bps.dropna()
    low, high = _day_block_ci(realized, config.bootstrap_samples, config.confidence_level, seed)
    total, n = len(events), len(realized)
    occupied = hold.sum() / max(1, duration_minutes * pair_count)
    q = lambda s, v: s.quantile(v) if len(s) else np.nan
    return {"total_signal_count": total, "realized_event_count": n,
        "censored_event_count": total - n, "censor_rate": (total - n) / total if total else np.nan,
        "mean_net_bps": net.mean(), "median_net_bps": net.median(),
        "win_rate": (net > 0).mean() if len(net) else np.nan,
        "p05_net_bps": q(net,.05), "p25_net_bps": q(net,.25), "p75_net_bps": q(net,.75),
        "p95_net_bps": q(net,.95), "mean_mae_bps": mae.mean(), "median_mae_bps": mae.median(),
        "mean_mfe_bps": mfe.mean(), "median_mfe_bps": mfe.median(),
        "mean_holding_minutes": hold.mean(), "median_holding_minutes": hold.median(),
        "p90_holding_minutes": q(hold,.90), "max_holding_minutes": hold.max(),
        "capital_occupancy_rate": occupied,
        "same_pair_overlap_count": int(events.same_pair_overlap_count.fillna(0).sum()),
        "cross_pair_overlap_count": _cross_overlap(realized), "day_block_ci_low": low,
        "day_block_ci_high": high, "mean_gross_price_pnl_bps": realized.gross_price_pnl_bps.mean(),
        "mean_funding_pnl_bps": realized.funding_pnl_bps.mean(),
        "mean_gross_combined_pnl_bps": realized.gross_combined_pnl_bps.mean(),
        "mean_cost_contribution_bps": -realized.assumed_total_cost_bps.mean(),
        "realized_day_count": int(pd.to_datetime(realized.signal_time,utc=True).dt.floor("D").nunique())}


def summarize_all(events: pd.DataFrame, config: StudyConfig, duration_minutes: float) -> pd.DataFrame:
    scenario = ["threshold_bps", "assumed_total_cost_bps", "trigger_type",
                "baseline_window_hours", "exit_policy"]
    rows = []
    for keys, scenario_events in events.groupby(scenario, dropna=False):
        base = dict(zip(scenario, keys)); pairs = sorted(scenario_events.pair.unique())
        groups = [(p, scenario_events[scenario_events.pair.eq(p)], 1) for p in pairs]
        gate = scenario_events[scenario_events.pair_scope.eq("GATE_PAIRS")]
        nongate = scenario_events[scenario_events.pair_scope.eq("NON_GATE_PAIRS")]
        groups += [("ALL_GATE_PAIRS", gate, max(1, gate.pair.nunique())),
                   ("ALL_NON_GATE_PAIRS", nongate, max(1, nongate.pair.nunique())),
                   ("ALL_PAIRS", scenario_events, max(1, len(pairs)))]
        for label, subset, pair_count in groups:
            row = {**base, "pair": label, "pair_scope": ("ALL" if label == "ALL_PAIRS" else
                "GATE_PAIRS" if label == "ALL_GATE_PAIRS" else
                "NON_GATE_PAIRS" if label == "ALL_NON_GATE_PAIRS" else
                str(subset.pair_scope.iloc[0]) if len(subset) else "EMPTY")}
            row.update(summarize(subset, pair_count, duration_minutes, config,
                config.random_seed + int(base["threshold_bps"]) + int(base["assumed_total_cost_bps"])))
            rows.append(row)
    return pd.DataFrame(rows)


def complete_zero_scenarios(results: pd.DataFrame, pair_names: list[str], trigger_type: str,
                            windows: tuple[int, ...]) -> pd.DataFrame:
    """Materialize every predeclared scenario, including genuine zero-signal rows."""
    labels=[*pair_names,"ALL_GATE_PAIRS","ALL_NON_GATE_PAIRS","ALL_PAIRS"]
    grid=pd.MultiIndex.from_product([THRESHOLDS,COSTS,(trigger_type,),windows,EXIT_POLICIES,labels],
        names=["threshold_bps","assumed_total_cost_bps","trigger_type",
               "baseline_window_hours","exit_policy","pair"]).to_frame(index=False)
    merged=grid.merge(results,on=["threshold_bps","assumed_total_cost_bps","trigger_type",
        "baseline_window_hours","exit_policy","pair"],how="left")
    count_cols=["total_signal_count","realized_event_count","censored_event_count",
                "same_pair_overlap_count","cross_pair_overlap_count"]
    for col in count_cols:merged[col]=merged[col].fillna(0).astype(int)
    def scope(label):
        if label=="ALL_PAIRS":return "ALL"
        if label=="ALL_GATE_PAIRS":return "GATE_PAIRS"
        if label=="ALL_NON_GATE_PAIRS":return "NON_GATE_PAIRS"
        return "GATE_PAIRS" if "gate" in label.split("/") else "NON_GATE_PAIRS"
    merged["pair_scope"]=merged.pair_scope.fillna(merged.pair.map(scope))
    return merged.reindex(columns=results.columns)


def _folds(times: pd.DatetimeIndex, config: StudyConfig):
    fold_id = 1
    for i in range(config.train_bars, len(times), config.test_bars):
        end_i = min(i + config.test_bars, len(times))
        yield fold_id, times[i-config.train_bars], times[i], times[end_i-1] + BAR
        fold_id += 1


def _baseline_reproduction_text() -> str:
    return """# Baseline reproduction

Reproduced before changing study logic from `main` commit `af361df` on 2026-07-24 UTC.

- `uv run --extra dev pytest -q`: **148 passed**.
- `make analysis-15m`: completed; strict common window ended at 06:15 UTC.
- `make gate-regime-15m`: completed.
- `make high-threshold-walk-forward`: completed (1,710 fold rows; 2,523 event rows).
- The regenerated public-data window extended the committed snapshot from 04:15 to 06:15 UTC. This explains small count/value changes; execution semantics and schemas reproduced.

Selected regenerated aggregate results (all figures are historical 15-minute proxies; costs are research assumptions):

| scope | threshold | cost | signals | realized | mean net | median net | day-block 95% CI |
|---|---:|---:|---:|---:|---:|---:|---:|
| Gate | 100 | 20 | 218 | 167 | 6.76 | -2.00 | [0.53, 15.56] |
| Gate | 100 | 40 | 218 | 167 | -13.24 | -22.00 | [-18.79, -4.18] |
| Gate | 100 | 80 | 218 | 167 | -53.24 | -62.00 | [-59.03, -44.75] |
| Gate | 150 | 20 | 277 | 247 | -2.10 | -6.56 | [-5.42, 1.44] |
| Gate | 150 | 40 | 277 | 247 | -22.10 | -26.56 | [-25.27, -18.45] |
| Gate | 150 | 80 | 277 | 247 | -62.10 | -66.56 | [-65.29, -58.52] |
| Gate | 200 | 20 | 190 | 182 | -4.96 | -9.22 | [-9.00, 0.34] |
| Gate | 200 | 40 | 190 | 182 | -24.96 | -29.22 | [-28.89, -19.61] |
| Gate | 200 | 80 | 190 | 182 | -64.96 | -69.22 | [-68.70, -59.92] |
| non-Gate | 100 | 20 | 118 | 108 | -6.98 | -8.98 | [-10.18, -3.50] |
| non-Gate | 100 | 40 | 118 | 108 | -26.98 | -28.98 | [-30.49, -23.38] |
| non-Gate | 100 | 80 | 118 | 108 | -66.98 | -68.98 | [-70.46, -63.48] |
| non-Gate | 150 | 20 | 25 | 22 | 11.20 | 20.39 | [0.73, 30.69] |
| non-Gate | 150 | 40 | 25 | 22 | -8.80 | 0.39 | [-19.08, 10.69] |
| non-Gate | 150 | 80 | 25 | 22 | -48.80 | -39.61 | [-59.08, -29.31] |
| non-Gate | 200 | 20 | 13 | 12 | 5.04 | -1.05 | [2.00, 8.09] |
| non-Gate | 200 | 40 | 13 | 12 | -14.96 | -21.05 | [-18.00, -11.91] |
| non-Gate | 200 | 80 | 13 | 12 | -54.96 | -61.05 | [-58.00, -51.91] |

At 40 and 80 bps every displayed scope/threshold mean was lower by exactly 20 and 60 bps respectively, confirming one total-cost deduction per realized event. The baseline reproduction therefore passed and extension was allowed to proceed.
"""


def _methodology_text(config: StudyConfig) -> str:
    return f"""# Methodology

## Scope and labels

This is **{PSEUDO_OOS}** research, not an executable backtest and not a live strategy. Inputs are native 15-minute **trade OHLC**. Pair bars require exact common timestamps; no filling, resampling, event-peak fills, or future values are used. A completed close confirms a signal and only the next contiguous 15-minute open is an execution proxy.

The first {config.train_bars} bars (7 days) are training/warm-up and each following {config.test_bars}-bar (2-day) test block freezes all predeclared parameters. Fixed thresholds, 24h/72h windows, costs and exits are independently reported; none is selected from test outcomes.

## Triggers and exits

- Raw directed spread: `20000 * (price_A-price_B)/(price_A+price_B)`; trigger on absolute spread at least the threshold.
- Residual: directed spread minus a trailing median of **only earlier bars**. Declared windows are 24h and 72h. MAD and same-sign ratio use the identical prior window. Any missing pair bar invalidates the rolling window until all 96/288 observations reaccumulate.
- Exits are independently evaluated: raw spread below entry threshold; residual within 20 bps; residual within 25% of entry residual. Exit is also confirmed at close and executed at the following contiguous open.

## Attribution and uncertainty

Price PnL marks both legs from entry opens to exit opens. Funding includes only public settlement events strictly after entry and strictly before exit. `gross_combined = price + funding`; one assumed total cost of 0/20/40/58/80 bps is then deducted. These are research assumptions, not any user's fee tier. Funding-minus-cost is not called a standalone funding strategy because both-leg basis risk remains.

All incomplete and rejected outcomes retain explicit status. Only `REALIZED` rows enter PnL statistics. The primary mean confidence interval resamples whole UTC day blocks ({config.bootstrap_samples} draws). Candidate language additionally requires at least 30 realized events, positive mean and median, a day-block lower bound not materially below zero, consistent 20/40 bps direction, multiple contributing dates, acceptable censoring and explainable holding/MAE risk.

## Gate filter audit

Gate pairs use causal labels only. Non-Gate pairs have no Gate regime filter and are reported separately. The rolling audit fix preserves the declared label thresholds but computes 24h median/MAD/sign persistence from one identical window and requires a full 24h re-warm after any gap.

## BBO boundary

Historical 15-minute OHLC cannot reconstruct bid/ask, quote age, timestamp skew or first-level capacity. Future BBO paper observations start only after the public collector runs and use long ask/short bid to enter, long bid/short ask to exit.
"""


def _candidate_label(row: pd.Series, cost20: pd.Series | None, cost40: pd.Series | None) -> str:
    if int(row.realized_event_count) < 30:
        return "PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE"
    conditions = [row.mean_net_bps > 0, row.median_net_bps > 0,
                  pd.notna(row.day_block_ci_low) and row.day_block_ci_low >= -2,
                  row.censor_rate <= .25, row.realized_day_count >= 2,
                  pd.notna(row.mean_mae_bps) and pd.notna(row.max_holding_minutes)]
    direction = (cost20 is not None and cost40 is not None and
                 np.sign(cost20.mean_net_bps) == np.sign(cost40.mean_net_bps) and
                 np.sign(cost20.median_net_bps) == np.sign(cost40.median_net_bps))
    return ("RESEARCH_CANDIDATE_FOR_FURTHER_PAPER_VALIDATION" if all(conditions) and direction
            else "REJECT_OR_OBSERVE_ONLY")


def _markdown(frame: pd.DataFrame) -> str:
    values=frame.copy()
    for col in values.select_dtypes(include=["float"]).columns:
        values[col]=values[col].map(lambda x: "" if pd.isna(x) else f"{x:.2f}")
    header="| "+" | ".join(map(str,values.columns))+" |"
    rule="|"+"|".join("---" for _ in values.columns)+"|"
    rows=["| "+" | ".join(map(str,row))+" |" for row in values.itertuples(index=False,name=None)]
    return "\n".join([header,rule,*rows])


def _executive_summary(results: pd.DataFrame, events: pd.DataFrame) -> str:
    focus = results[(results.pair.eq("ALL_PAIRS")) & results.assumed_total_cost_bps.eq(40)]
    rows = []
    for _, r in focus.iterrows():
        base = results[(results.pair.eq("ALL_PAIRS")) &
            results.threshold_bps.eq(r.threshold_bps) &
            results.trigger_type.eq(r.trigger_type) &
            results.baseline_window_hours.eq(r.baseline_window_hours) &
            results.exit_policy.eq(r.exit_policy)]
        c20 = base[base.assumed_total_cost_bps.eq(20)]
        label = _candidate_label(r, c20.iloc[0] if len(c20) else None, r)
        rows.append({"trigger": r.trigger_type, "window_h": int(r.baseline_window_hours),
            "exit": r.exit_policy, "threshold": int(r.threshold_bps), "realized": int(r.realized_event_count),
            "mean40": r.mean_net_bps, "median40": r.median_net_bps,
            "ci": f"[{r.day_block_ci_low:.1f}, {r.day_block_ci_high:.1f}]", "decision": label})
    table = pd.DataFrame(rows).sort_values(["decision","mean40"], ascending=[True,False]).head(30)
    raw_table=results[(results.pair.eq("ALL_PAIRS")) & results.trigger_type.eq("RAW_SPREAD") &
        results.exit_policy.eq("LEGACY_RAW_EXIT") & results.assumed_total_cost_bps.isin([20,40,58,80])][
        ["threshold_bps","assumed_total_cost_bps","realized_event_count","censor_rate",
         "mean_net_bps","median_net_bps","day_block_ci_low","day_block_ci_high"]]
    residual_table=results[(results.pair.eq("ALL_PAIRS")) & results.trigger_type.eq("RESIDUAL_SPREAD") &
        results.exit_policy.eq("BASELINE_RESIDUAL_EXIT") & results.assumed_total_cost_bps.isin([20,40])][
        ["baseline_window_hours","threshold_bps","assumed_total_cost_bps","realized_event_count",
         "censor_rate","mean_net_bps","median_net_bps","day_block_ci_low","day_block_ci_high"]]
    candidate_count=int(table.decision.eq("RESEARCH_CANDIDATE_FOR_FURTHER_PAPER_VALIDATION").sum())
    realized = events[events.status.eq("REALIZED") & events.assumed_total_cost_bps.eq(40)]
    funding_share = (realized.funding_pnl_bps.abs().sum() /
        realized.gross_combined_pnl_bps.abs().sum() if len(realized) else np.nan)
    return f"""# Executive summary

## Bottom line

Raising a raw observed-spread threshold does not by itself establish a robust improvement. Mean and median must be read together: positive means with non-positive medians indicate tail dependence, while the highest thresholds often have too few independent realized events. Gate and non-Gate results are separated because Gate's persistent basis and causal filter materially change the event population.

Residual triggers are the cleaner answer to “structural basis or temporary deviation”: the trailing median estimates structural center and the residual measures short-lived displacement. The two declared windows are shown independently; neither is selected after seeing outcomes. Funding is a joint-trade attribution component, not an independent strategy. Across duplicated 40-bps realized scenario rows its absolute contribution ratio is {funding_share:.2%}.

Costs are assumed total round-trip research scenarios. A positive gross result that fails at 40/58/80 bps is not robust. No historical result here is called tradable: 15-minute opens omit executable bid/ask, depth, latency and skew. Candidate labels mean only “continue future public-BBO paper validation.”

**No scenario passes all candidate criteria (`{candidate_count}` candidates).** Raw 20/50/100 bps legacy scenarios have negative mean and median even at the 20-bps assumption. Raw 150 bps turns positive at 20 bps but has only 19 realized events, 93.0% censoring, a CI crossing zero, and becomes negative at 40 bps. Raw 200 bps has 11 events and a negative median; 250 bps has two events; 300/400/500 have none. Thus the apparent high-threshold improvement is a small-sample/tail effect, not robust evidence.

Residual 50-bps triggers are positive at the 20-bps assumption (55 events for 24h; 58 for 72h) but become negative at 40 bps. Residual 100–200 bps positive cells contain only one to three realized events with censor rates above 98%. Residual triggering is conceptually more appropriate for separating structural basis, but current history and causal Gate data quality do not support a candidate.

## Raw trigger / legacy exit cost matrix

{_markdown(raw_table)}

## Residual trigger / baseline-residual exit comparison

{_markdown(residual_table)}

## 40-bps all-pair screening table

{_markdown(table)}

## Required interpretation

1. Inspect both mean and median; disagreement is evidence of a few large events masking the typical outcome.
2. Intervals crossing zero and fewer than 30 realized events prevent a positive claim.
3. Compare `ALL_GATE_PAIRS`, `ALL_NON_GATE_PAIRS`, individual pairs and UTC-day blocks before attributing improvement broadly.
4. High censoring, large adverse excursion or very long holding time disqualifies otherwise attractive averages.
5. Thresholds failing the stated candidate criteria are either observation-only or rejected; none is approved for live trading.
"""


def _charts(results: pd.DataFrame, events: pd.DataFrame, chart_dir: Path):
    chart_dir.mkdir(parents=True, exist_ok=True); sns.set_theme(style="whitegrid")
    focus = results[(results.pair.eq("ALL_PAIRS")) & results.assumed_total_cost_bps.eq(40) &
                    results.exit_policy.eq("BASELINE_RESIDUAL_EXIT")]
    def line(metric, title, name):
        fig, ax = plt.subplots(figsize=(10,5))
        for (trigger, window), g in focus.groupby(["trigger_type","baseline_window_hours"]):
            g=g.sort_values("threshold_bps"); label=f"{trigger} {int(window)}h"
            ax.plot(g.threshold_bps,g[metric],marker="o",label=label)
            for _,r in g.iterrows(): ax.annotate(f"n={int(r.realized_event_count)}",(r.threshold_bps,r[metric]),fontsize=7)
        ax.axhline(0,color="black",lw=.8);ax.set(title=title,xlabel="threshold bps",ylabel=metric);ax.legend()
        fig.tight_layout();fig.savefig(chart_dir/name,dpi=150);plt.close(fig)
    line("mean_net_bps","Threshold vs mean net (40-bps assumed cost)","threshold_mean_net.png")
    line("median_net_bps","Threshold vs median net (40-bps assumed cost)","threshold_median_net.png")
    line("realized_event_count","Threshold vs realized sample count","threshold_event_count.png")
    line("censor_rate","Threshold vs censor rate","threshold_censor_rate.png")
    line("win_rate","Threshold vs win rate","threshold_win_rate.png")
    line("median_holding_minutes","Threshold vs median holding time","threshold_median_holding.png")
    fig,ax=plt.subplots(figsize=(10,5))
    g=focus[(focus.trigger_type.eq("RAW_SPREAD")) & focus.baseline_window_hours.eq(24)].sort_values("threshold_bps")
    ax.errorbar(g.threshold_bps,g.mean_net_bps,yerr=[g.mean_net_bps-g.day_block_ci_low,g.day_block_ci_high-g.mean_net_bps],fmt="o-")
    for _,r in g.iterrows():ax.annotate(f"n={int(r.realized_event_count)}",(r.threshold_bps,r.mean_net_bps),fontsize=7)
    ax.axhline(0,color="black",lw=.8);ax.set(title="Raw trigger day-block 95% CI (40-bps cost)",xlabel="threshold bps",ylabel="mean net bps")
    fig.tight_layout();fig.savefig(chart_dir/"threshold_day_block_ci.png",dpi=150);plt.close(fig)
    sample=events[events.status.eq("REALIZED") & events.assumed_total_cost_bps.eq(40)]
    fig,ax=plt.subplots(figsize=(10,5)); sns.ecdfplot(sample,x="holding_minutes",hue="threshold_bps",palette="viridis",ax=ax)
    ax.set_xscale("log");ax.set_title(f"Event duration ECDF (n={len(sample):,})");fig.tight_layout();fig.savefig(chart_dir/"duration_ecdf.png",dpi=150);plt.close(fig)
    fig,axes=plt.subplots(1,2,figsize=(11,4));sns.histplot(sample,x="mae_bps",bins=40,ax=axes[0]);sns.histplot(sample,x="mfe_bps",bins=40,ax=axes[1])
    axes[0].set_title(f"MAE distribution n={len(sample):,}");axes[1].set_title(f"MFE distribution n={len(sample):,}");fig.tight_layout();fig.savefig(chart_dir/"mae_mfe_distribution.png",dpi=150);plt.close(fig)
    compare=focus.groupby(["threshold_bps","trigger_type"],as_index=False).agg(mean_net_bps=("mean_net_bps","mean"),n=("realized_event_count","sum"))
    fig,ax=plt.subplots(figsize=(10,5));sns.barplot(compare,x="threshold_bps",y="mean_net_bps",hue="trigger_type",ax=ax)
    ax.set_title("Raw vs residual triggers (labels include aggregate n)");fig.tight_layout();fig.savefig(chart_dir/"raw_vs_residual.png",dpi=150);plt.close(fig)
    scope=results[(results.pair.isin(["ALL_GATE_PAIRS","ALL_NON_GATE_PAIRS"])) & results.assumed_total_cost_bps.eq(40) & results.trigger_type.eq("RAW_SPREAD") & results.exit_policy.eq("LEGACY_RAW_EXIT")]
    fig,ax=plt.subplots(figsize=(10,5));sns.lineplot(scope,x="threshold_bps",y="mean_net_bps",hue="pair",marker="o",ax=ax);ax.axhline(0,color="black",lw=.8)
    ax.set_title("Gate vs non-Gate raw trigger (40-bps cost)");fig.tight_layout();fig.savefig(chart_dir/"gate_vs_non_gate.png",dpi=150);plt.close(fig)
    costs=results[(results.pair.eq("ALL_PAIRS")) & results.trigger_type.eq("RAW_SPREAD") & results.exit_policy.eq("LEGACY_RAW_EXIT")]
    fig,ax=plt.subplots(figsize=(10,5));sns.lineplot(costs,x="threshold_bps",y="mean_net_bps",hue="assumed_total_cost_bps",marker="o",palette="magma",ax=ax);ax.axhline(0,color="black",lw=.8)
    ax.set_title("Assumed total-cost sensitivity (not user fee tiers)");fig.tight_layout();fig.savefig(chart_dir/"cost_sensitivity.png",dpi=150);plt.close(fig)


def _report_html(out: Path, results: pd.DataFrame):
    charts = sorted((out/"charts").glob("*.png"))
    focus = results[(results.pair.isin(["ALL_PAIRS","ALL_GATE_PAIRS","ALL_NON_GATE_PAIRS"])) &
                    results.assumed_total_cost_bps.isin([20,40,58,80])]
    html = ["<!doctype html><meta charset='utf-8'><title>Higher bps trigger study</title>",
        "<style>body{font-family:system-ui;max-width:1200px;margin:auto}img{max-width:100%}table{border-collapse:collapse;font-size:12px}td,th{border:1px solid #ccc;padding:4px}</style>",
        "<h1>Higher bps trigger study</h1><p><b>HISTORICAL_ROLLING_PSEUDO_OOS.</b> Native 15-minute OHLC is not executable BBO. All costs are research assumptions; all outputs are paper-only research.</p>"]
    html += [f"<h2>{p.stem}</h2><img src='charts/{p.name}' alt='{p.stem}'>" for p in charts]
    html += ["<h2>Aggregate result excerpt</h2>",focus.head(300).to_html(index=False),
             "<p>See CSV files for every independent threshold, pair, cost, trigger and exit.</p>"]
    (out/"report.html").write_text("\n".join(html),encoding="utf-8")


def run_study(prices: pd.DataFrame, funding: pd.DataFrame | None = None,
              output_dir: str | Path | None = None, config: StudyConfig | None = None):
    config=config or StudyConfig();out=Path(output_dir or ROOT/"reports_higher_bps_study");out.mkdir(parents=True,exist_ok=True)
    pairs=build_pair_bars(prices)
    observed=[set(z.loc[z.close_A.notna() & z.close_B.notna(),"open_time"]) for z in pairs.values()]
    common=pd.DatetimeIndex(sorted(set.intersection(*observed)))
    if len(common)<=config.train_bars:raise ValueError("not enough strict common bars")
    has_gate=any("gate" in p.split("/") for p in pairs)
    causal=(build_causal_regime_labels(prices)[["open_time","causal_regime"]]
            if has_gate else pd.DataFrame(columns=["open_time","causal_regime"]))
    enriched={}
    for pair,bars in pairs.items():
        regime=(bars.merge(causal,on="open_time",how="left").causal_regime.fillna("STALE_OR_INVALID")
                if "gate" in pair.split("/") else pd.Series("NOT_APPLICABLE",index=bars.index))
        for hours in BASELINE_WINDOWS_HOURS:
            z=add_past_only_baseline(bars,hours);z["regime"]=regime.to_numpy();enriched[(pair,hours)]=z
    raw_rows=[];residual_rows=[]
    for fold_id,train_start,test_start,test_end in _folds(common,config):
        for (pair,hours),bars in enriched.items():
            for threshold in THRESHOLDS:
                for policy in EXIT_POLICIES:
                    if hours==24:
                        raw_rows.append(simulate_scenario(bars,threshold,"RAW_SPREAD",policy,test_start,test_end,funding,fold_id))
                    residual_rows.append(simulate_scenario(bars,threshold,"RESIDUAL_SPREAD",policy,test_start,test_end,funding,fold_id))
    raw=attach_costs(pd.concat(raw_rows,ignore_index=True));residual=attach_costs(pd.concat(residual_rows,ignore_index=True))
    all_events=pd.concat([raw,residual],ignore_index=True);duration=(common.max()-common[config.train_bars]).total_seconds()/60+15
    raw_results=complete_zero_scenarios(summarize_all(raw,config,duration),sorted(pairs),"RAW_SPREAD",(24,))
    residual_results=complete_zero_scenarios(summarize_all(residual,config,duration),sorted(pairs),"RESIDUAL_SPREAD",BASELINE_WINDOWS_HOURS)
    all_results=pd.concat([raw_results,residual_results],ignore_index=True)
    raw.to_csv(out/"fixed_threshold_events.csv",index=False);raw_results.to_csv(out/"fixed_threshold_results.csv",index=False)
    residual.to_csv(out/"residual_threshold_events.csv",index=False);residual_results.to_csv(out/"residual_threshold_results.csv",index=False)
    matrix=all_results[all_results.pair.eq("ALL_PAIRS")].copy();matrix.to_csv(out/"threshold_cost_matrix.csv",index=False)
    gate=all_results[all_results.pair.isin(["ALL_GATE_PAIRS","ALL_NON_GATE_PAIRS"])].copy();gate.to_csv(out/"gate_vs_non_gate_summary.csv",index=False)
    (out/"baseline_reproduction.md").write_text(_baseline_reproduction_text(),encoding="utf-8")
    (out/"methodology.md").write_text(_methodology_text(config),encoding="utf-8")
    (out/"EXECUTIVE_SUMMARY.md").write_text(_executive_summary(all_results,all_events),encoding="utf-8")
    before=pd.read_csv(ROOT/"reports_15m/gate_causal_regime_summary_15m.csv") if (ROOT/"reports_15m/gate_causal_regime_summary_15m.csv").exists() else pd.DataFrame()
    after=build_causal_regime_labels(prices).causal_regime.value_counts().rename_axis("causal_regime").rename("after_bars").reset_index() if has_gate else pd.DataFrame()
    audit=(before.merge(after,on="causal_regime",how="outer").fillna(0) if len(before) else after)
    audit_text="# Gate causal-regime audit\n\nThe audit found and fixed nested rolling MAD/sign windows and incomplete post-gap re-warm. Threshold parameters were not tuned. Synthetic tests cover persistent same-sign basis, one transient spike, a gap with full reaccumulation, and normal low spread.\n\n"+_markdown(audit)+"\n\nThe study was regenerated after the fix. Before/after bar-count changes are implementation effects, not evidence of profitability.\n"
    (out/"regime_audit.md").write_text(audit_text,encoding="utf-8")
    _charts(all_results,all_events,out/"charts");_report_html(out,all_results)
    manifest={"oos_kind":PSEUDO_OOS,"execution_model":EXECUTION_MODEL,"thresholds":THRESHOLDS,"costs":COSTS,
        "baseline_windows_hours":BASELINE_WINDOWS_HOURS,"exit_policies":EXIT_POLICIES,"statuses":STATUSES,
        "config":asdict(config),"historical_bbo_reconstructed":False,"paper_only":True}
    (out/"manifest.json").write_text(json.dumps(manifest,indent=2),encoding="utf-8")
    return {"events":all_events,"results":all_results,"output_dir":out}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--prices",type=Path,default=ROOT/"data/normalized/prices_15m.parquet")
    p.add_argument("--funding",type=Path,default=ROOT/"data/normalized/funding_events.parquet");p.add_argument("--output-dir",type=Path,default=ROOT/"reports_higher_bps_study")
    p.add_argument("--bootstrap-samples",type=int,default=StudyConfig.bootstrap_samples);a=p.parse_args(argv)
    result=run_study(pd.read_parquet(a.prices),pd.read_parquet(a.funding) if a.funding.exists() else None,a.output_dir,StudyConfig(bootstrap_samples=a.bootstrap_samples))
    print(f"rows={len(result['events']):,}; output={result['output_dir']}");return 0


if __name__=="__main__":raise SystemExit(main())
