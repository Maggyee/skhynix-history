from __future__ import annotations

import itertools
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


BAR_INTERVAL = pd.Timedelta(minutes=15)
BASELINE_WINDOWS = (pd.Timedelta(hours=24), pd.Timedelta(hours=72))
ASSUMED_TOTAL_COST_BPS = 20.0
EXIT_BUFFER_BPS = 20.0
SUMMARY_COLUMNS = [
    "pair",
    "baseline_window",
    "median_raw_spread_bps",
    "median_abs_raw_spread_bps",
    "median_residual_bps",
    "p95_abs_residual_bps",
    "p99_abs_residual_bps",
    "share_abs_residual_gt_20bps",
    "share_abs_residual_gt_50bps",
    "share_abs_residual_gt_100bps",
    "median_net_residual_edge_bps",
    "p95_net_residual_edge_bps",
    "strict_common_window_start",
    "strict_common_window_end",
    "valid_residual_count",
    "insufficient_history_count",
]


def compute_raw_directed_spread(price_a: pd.Series, price_b: pd.Series) -> pd.Series:
    """Return A-minus-B symmetric spread on exact common observations, in bps."""
    a, b = price_a.align(price_b, join="inner")
    denominator = a + b
    spread = 20_000.0 * (a - b) / denominator
    return spread.where(a.notna() & b.notna() & denominator.ne(0)).rename("spread_bps")


def compute_structural_baseline(
    spread: pd.Series,
    window: str | pd.Timedelta,
    *,
    bar_interval: str | pd.Timedelta = BAR_INTERVAL,
) -> pd.Series:
    """Causal trailing median that resets at timestamp or value gaps.

    A baseline at time t contains exactly the preceding ``window`` worth of
    contiguous native bars and never includes the observation at t.
    """
    if not isinstance(spread.index, pd.DatetimeIndex):
        raise TypeError("spread must use a DatetimeIndex")
    values = spread.sort_index().astype(float)
    duration = pd.Timedelta(window)
    interval = pd.Timedelta(bar_interval)
    if duration <= pd.Timedelta(0) or interval <= pd.Timedelta(0) or duration % interval:
        raise ValueError("window must be a positive multiple of bar_interval")
    periods = int(duration / interval)
    valid = values.notna()
    timestamp_break = values.index.to_series().diff().ne(interval).to_numpy()
    value_break = (~valid | ~valid.shift(fill_value=False)).to_numpy()
    segment = pd.Series(timestamp_break | value_break, index=values.index).cumsum()
    baseline = values.groupby(segment, group_keys=False).transform(
        lambda part: part.shift(1).rolling(periods, min_periods=periods).median()
    )
    baseline.name = f"baseline_{_window_label(duration)}"
    return baseline.reindex(spread.index)


def compute_residual_spread(spread: pd.Series, baseline: pd.Series) -> pd.Series:
    raw, structural = spread.align(baseline, join="inner")
    return (raw - structural).rename("residual_bps")


def compute_residual_edge(
    residual: pd.Series,
    assumed_total_cost_bps: float = ASSUMED_TOTAL_COST_BPS,
    exit_buffer_bps: float = EXIT_BUFFER_BPS,
) -> pd.Series:
    return (
        residual.abs() - float(assumed_total_cost_bps) - float(exit_buffer_bps)
    ).rename("net_residual_edge_bps")


def make_relative_spread_summary_table(
    diagnostics: dict[str, pd.DataFrame],
    *,
    strict_common_window_start: pd.Timestamp | None = None,
    strict_common_window_end: pd.Timestamp | None = None,
) -> pd.DataFrame:
    rows: list[dict] = []
    for pair, frame in diagnostics.items():
        raw = frame["spread_bps"].dropna()
        start = (
            strict_common_window_start
            if strict_common_window_start is not None
            else (raw.index.min() if len(raw) else pd.NaT)
        )
        end = (
            strict_common_window_end
            if strict_common_window_end is not None
            else (raw.index.max() + BAR_INTERVAL if len(raw) else pd.NaT)
        )
        for window in BASELINE_WINDOWS:
            label = _window_label(window)
            residual = frame[f"residual_{label}_bps"].dropna()
            edge = frame[f"net_residual_edge_{label}_bps"].dropna()
            absolute = residual.abs()
            rows.append(
                {
                    "pair": pair,
                    "baseline_window": label,
                    "median_raw_spread_bps": raw.median(),
                    "median_abs_raw_spread_bps": raw.abs().median(),
                    "median_residual_bps": residual.median(),
                    "p95_abs_residual_bps": absolute.quantile(0.95),
                    "p99_abs_residual_bps": absolute.quantile(0.99),
                    "share_abs_residual_gt_20bps": (absolute > 20).mean(),
                    "share_abs_residual_gt_50bps": (absolute > 50).mean(),
                    "share_abs_residual_gt_100bps": (absolute > 100).mean(),
                    "median_net_residual_edge_bps": edge.median(),
                    "p95_net_residual_edge_bps": edge.quantile(0.95),
                    "strict_common_window_start": start,
                    "strict_common_window_end": end,
                    "valid_residual_count": len(residual),
                    "insufficient_history_count": int(
                        frame[f"baseline_{label}_status"].eq("insufficient_history").sum()
                    ),
                }
            )
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS).sort_values(
        ["pair", "baseline_window"], ignore_index=True
    )


def make_relative_spread_plots(
    pair: str,
    frame: pd.DataFrame,
    output_dir: Path,
    *,
    assumed_total_cost_bps: float = ASSUMED_TOTAL_COST_BPS,
    exit_buffer_bps: float = EXIT_BUFFER_BPS,
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = _pair_stem(pair)
    paths = [
        output_dir / f"{stem}_raw_vs_baseline.png",
        output_dir / f"{stem}_residual.png",
        output_dir / f"{stem}_residual_edge.png",
    ]
    label = pair.replace("/", " / ").title()

    fig, ax = plt.subplots(figsize=(12, 5.2))
    ax.plot(frame.index, frame.spread_bps, color="#64748b", linewidth=0.8, label="Raw directed spread")
    ax.plot(frame.index, frame.baseline_24h_bps, color="#2563eb", linewidth=1.2, label="24h causal baseline")
    ax.plot(frame.index, frame.baseline_72h_bps, color="#dc2626", linewidth=1.2, label="72h causal baseline")
    ax.axhline(0, color="black", linewidth=0.8)
    _finish_axis(ax, f"{label}: Raw Directed Spread vs 24h/72h Structural Baseline")
    fig.tight_layout()
    fig.savefig(paths[0], dpi=160, bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5.2))
    ax.plot(frame.index, frame.residual_24h_bps, color="#2563eb", linewidth=1.0, label="Residual vs 24h baseline")
    ax.plot(frame.index, frame.residual_72h_bps, color="#dc2626", linewidth=1.0, label="Residual vs 72h baseline")
    ax.axhline(0, color="black", linewidth=0.9)
    for level in (20, 50, 100):
        for signed in (-level, level):
            ax.axhline(signed, color="#94a3b8", linewidth=0.6, linestyle="--", alpha=0.6)
    _finish_axis(ax, f"{label}: Residual Spread Relative to Structural Baseline")
    fig.tight_layout()
    fig.savefig(paths[1], dpi=160, bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5.2))
    ax.plot(frame.index, frame.net_residual_edge_24h_bps, color="#2563eb", linewidth=1.0, label="24h diagnostic edge")
    ax.plot(frame.index, frame.net_residual_edge_72h_bps, color="#dc2626", linewidth=1.0, label="72h diagnostic edge")
    ax.axhline(0, color="black", linewidth=0.9)
    for level in (100, 150, 200):
        ax.axhline(level, color="#94a3b8", linewidth=0.6, linestyle="--", alpha=0.6)
    ax.text(
        0.01,
        0.98,
        f"Research assumption only: assumed cost = {assumed_total_cost_bps:g} bps, "
        f"exit buffer = {exit_buffer_bps:g} bps; not executable net P&L",
        transform=ax.transAxes,
        va="top",
        fontsize=9,
        bbox={"facecolor": "#fff7ed", "edgecolor": "#fdba74", "alpha": 0.9},
    )
    _finish_axis(ax, f"{label}: Residual Edge After Assumed Cost and Exit Buffer")
    fig.tight_layout()
    fig.savefig(paths[2], dpi=160, bbox_inches="tight")
    plt.close(fig)
    return paths


def build_relative_spread_diagnostics(
    aligned_prices: pd.DataFrame,
    output_dir: Path,
    *,
    assumed_total_cost_bps: float = ASSUMED_TOTAL_COST_BPS,
    exit_buffer_bps: float = EXIT_BUFFER_BPS,
) -> tuple[pd.DataFrame, list[str]]:
    """Build the seven-pair report artifacts from strict native 15m prices."""
    wide = aligned_prices.pivot(index="open_time", columns="exchange", values="close").sort_index()
    required_gate_pairs = [
        ("gate", "binance"),
        ("gate", "bitget"),
        ("gate", "okx"),
        ("gate", "hyperliquid"),
    ]
    non_gate_exchanges = sorted(x for x in wide.columns if x != "gate")
    scores = []
    for a, b in itertools.combinations(non_gate_exchanges, 2):
        raw = compute_raw_directed_spread(wide[a], wide[b])
        scores.append((float(raw.abs().quantile(0.95)), a, b))
    top_non_gate = [(a, b) for _, a, b in sorted(scores, key=lambda x: (-x[0], x[1], x[2]))[:3]]
    selected = [pair for pair in required_gate_pairs if set(pair).issubset(wide.columns)] + top_non_gate

    diagnostics: dict[str, pd.DataFrame] = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    for a, b in selected:
        pair = f"{a}/{b}"
        raw = compute_raw_directed_spread(wide[a], wide[b])
        frame = pd.DataFrame({"spread_bps": raw})
        for window in BASELINE_WINDOWS:
            label = _window_label(window)
            baseline = compute_structural_baseline(raw, window)
            residual = compute_residual_spread(raw, baseline)
            frame[f"baseline_{label}_bps"] = baseline
            frame[f"baseline_{label}_status"] = np.where(
                baseline.notna(), "ok", "insufficient_history"
            )
            frame[f"residual_{label}_bps"] = residual
            frame[f"net_residual_edge_{label}_bps"] = compute_residual_edge(
                residual, assumed_total_cost_bps, exit_buffer_bps
            )
        diagnostics[pair] = frame
        make_relative_spread_plots(
            pair,
            frame,
            output_dir,
            assumed_total_cost_bps=assumed_total_cost_bps,
            exit_buffer_bps=exit_buffer_bps,
        )
    start = wide.index.min()
    end = wide.index.max() + BAR_INTERVAL
    summary = make_relative_spread_summary_table(
        diagnostics,
        strict_common_window_start=start,
        strict_common_window_end=end,
    )
    summary.to_csv(output_dir / "relative_spread_summary.csv", index=False)
    return summary, [f"{a}/{b}" for a, b in selected]


def _window_label(window: pd.Timedelta) -> str:
    return f"{int(window / pd.Timedelta(hours=1))}h"


def _pair_stem(pair: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", pair.lower()).strip("_")


def _finish_axis(ax: plt.Axes, title: str) -> None:
    ax.set_title(title)
    ax.set_ylabel("Directed spread / residual (bps)")
    ax.set_xlabel("UTC time — native 15m trade-close bars, not BBO")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    ax.legend(loc="best", fontsize=9)
    ax.grid(True, alpha=0.25)
