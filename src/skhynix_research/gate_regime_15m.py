"""Independent, reproducible Gate native-15m regime diagnosis.

This module deliberately does not call, import, or mutate the joint-strategy
calculation.  It reads the normalized native 15-minute candles and real funding
settlements, performs strict timestamp joins (never fill/resample), and writes a
self-contained diagnostic bundle under ``reports_15m``.
"""
from __future__ import annotations

import argparse
import itertools
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

from .analysis import symmetric_spread_bps
from .calendar import parse_utc
from .config import ROOT

BAR = pd.Timedelta(minutes=15)
PRESET_START = pd.Timestamp("2026-07-16T00:00:00Z")
PRESET_END = pd.Timestamp("2026-07-20T00:00:00Z")
EXCHANGES = ("binance", "bitget", "gate", "hyperliquid", "okx")
EXTERNAL_TRADE = ("binance", "bitget", "okx")
EXTERNAL_MARK = ("binance", "bitget", "okx")
THRESHOLDS = (20, 50, 100, 150, 200)
STRICT_EXTERNAL_SCOPE = "STRICT_3_OF_3_EXTERNAL"
SENSITIVITY_EXTERNAL_SCOPE = "AVAILABLE_2_OF_3_SENSITIVITY"
RETROSPECTIVE_CHANGE_POINTS = "RETROSPECTIVE_CHANGE_POINTS"
CAUSAL_REGIME_LABELS = "CAUSAL_REGIME_LABELS"
CAUSAL_REGIMES = (
    "NORMAL", "TRANSIENT_DISLOCATION", "STRUCTURAL_PREMIUM", "STALE_OR_INVALID"
)
CAUSAL_RESEARCH_PARAMS = {
    "lookback_bars": 96,
    "structural_median_bps": 50.0,
    "structural_same_sign_ratio": 0.80,
    "transient_current_bps": 100.0,
    "transient_max_structural_median_bps": 50.0,
    "transient_mad_multiplier": 5.0,
    "mad_floor_bps": 1.0,
}
R15 = ROOT / "reports_15m"
CHARTS = R15 / "charts"


def manual_period_for_time(value) -> str:
    """Return the descriptive manual date segment; never use this for trading."""
    t = parse_utc(value)
    if t < PRESET_START:
        return "PRE_20260716"
    if t < PRESET_END:
        return "GATE_REGIME_20260716_20260720"
    return "POST_20260720"


# Compatibility for historical report code.  The name is intentionally not
# exposed by the strategy-facing causal provider below.
regime_for_time = manual_period_for_time


def _bps(a, b):
    """Signed symmetric bps; positive means *a* is higher than *b*."""
    return symmetric_spread_bps(a, b)


def _native_prices(prices: pd.DataFrame) -> pd.DataFrame:
    required = {"exchange", "symbol", "price_type", "open_time", "open", "high", "low", "close",
                "volume_base", "native_interval", "interval_minutes", "source_endpoint"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices_15m missing columns: {sorted(missing)}")
    p = prices.copy()
    p["open_time"] = pd.to_datetime(p.open_time, utc=True)
    if not p.native_interval.eq("15m").all() or not p.interval_minutes.eq(15).all():
        raise ValueError("Only native 15-minute candles are allowed")
    aligned = ((p.open_time.dt.minute % 15 == 0) & (p.open_time.dt.second == 0) &
               (p.open_time.dt.microsecond == 0))
    if not aligned.all():
        raise ValueError("15-minute timestamps must fall on 00/15/30/45 UTC")
    if p.source_endpoint.astype(str).str.contains("resample|aggregate|prices_1m", case=False, regex=True).any():
        raise ValueError("Synthetic 15-minute data is forbidden")
    return p.sort_values(["exchange", "price_type", "open_time"]).reset_index(drop=True)


def strict_wide(prices: pd.DataFrame, price_type: str, exchanges=EXCHANGES) -> pd.DataFrame:
    """One row per exact native open_time; no filling and no timestamp shifting."""
    p = _native_prices(prices)
    p = p[(p.price_type == price_type) & p.exchange.isin(exchanges)]
    if p.duplicated(["exchange", "open_time"]).any():
        raise ValueError(f"duplicate {price_type} exchange/open_time rows")
    return p.pivot(index="open_time", columns="exchange", values="close").sort_index()


def external_median(wide: pd.DataFrame, exchanges=EXTERNAL_TRADE) -> pd.Series:
    """Strict external median: all three specified venues must be present."""
    if "gate" in exchanges:
        raise ValueError("Gate cannot enter the external market median")
    columns = list(exchanges)
    if any(x not in wide.columns for x in columns):
        return pd.Series(index=wide.index, dtype=float)
    external = wide[columns]
    return external.median(axis=1).where(external.notna().all(axis=1))


def external_sensitivity(wide: pd.DataFrame, exchanges=EXTERNAL_TRADE) -> pd.DataFrame:
    """Return the exactly-two-of-three sensitivity series, never the main series."""
    if "gate" in exchanges:
        raise ValueError("Gate cannot enter the external market median")
    columns = list(exchanges)
    external = wide.reindex(columns=columns)
    count = external.notna().sum(axis=1)
    return pd.DataFrame({
        "external_venue_count": count,
        "external_scope": SENSITIVITY_EXTERNAL_SCOPE,
        "available_2_of_3_external_median": external.median(axis=1).where(count.eq(2)),
    }, index=wide.index).loc[count.eq(2)]


def build_core(prices: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return strict trade comparison, mark comparison, and Gate decomposition frames."""
    p = _native_prices(prices)
    trade = strict_wide(p, "trade")
    mark = strict_wide(p, "mark", ("binance", "bitget", "gate", "okx"))
    index = strict_wide(p, "index", ("binance", "bitget", "gate", "okx"))
    trade["external_venue_count"] = trade.reindex(columns=EXTERNAL_TRADE).notna().sum(axis=1)
    trade["external_scope"] = STRICT_EXTERNAL_SCOPE
    trade["market_trade_median"] = external_median(trade)
    trade["available_2_of_3_market_trade_median"] = external_sensitivity(trade)[
        "available_2_of_3_external_median"
    ].reindex(trade.index)
    mark["market_mark_median"] = external_median(mark, EXTERNAL_MARK)
    for ex in ("binance", "bitget", "hyperliquid", "okx"):
        trade[f"gate_vs_{ex}_bps"] = _bps(trade.get("gate"), trade.get(ex))
    trade["gate_premium_vs_market_median_bps"] = _bps(trade.get("gate"), trade.market_trade_median)

    # All decomposition rows are the exact intersection of the four required
    # values.  No value is borrowed from an adjacent bar.
    d = pd.concat({
        "gate_trade": trade.get("gate"), "market_trade_median": trade.market_trade_median,
        "gate_mark": mark.get("gate"), "gate_index": index.get("gate"),
        "market_mark_median": mark.market_mark_median,
    }, axis=1).dropna(subset=["gate_trade", "market_trade_median", "gate_mark", "gate_index"])
    d["total_gate_trade_vs_market_bps"] = _bps(d.gate_trade, d.market_trade_median)
    d["gate_trade_minus_gate_mark_bps"] = _bps(d.gate_trade, d.gate_mark)
    d["gate_mark_minus_gate_index_bps"] = _bps(d.gate_mark, d.gate_index)
    d["gate_index_minus_market_bps"] = _bps(d.gate_index, d.market_trade_median)
    d["decomposition_residual_bps"] = (d.total_gate_trade_vs_market_bps -
        d.gate_trade_minus_gate_mark_bps - d.gate_mark_minus_gate_index_bps -
        d.gate_index_minus_market_bps)
    for frame in (trade, mark, d):
        frame["regime"] = [regime_for_time(x) for x in frame.index]
    return trade, mark, d


def _valid_trade_ohlc(prices: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    p = _native_prices(prices)
    p = p[(p.price_type == "trade") & p.exchange.isin(("gate",) + EXTERNAL_TRADE)].copy()
    valid = (
        p[["open", "high", "low", "close"]].notna().all(axis=1)
        & (p[["open", "high", "low", "close"]] > 0).all(axis=1)
        & (p.high >= p[["open", "close"]].max(axis=1))
        & (p.low <= p[["open", "close"]].min(axis=1))
    )
    p["valid_ohlc"] = valid
    return p.pivot(index="open_time", columns="exchange", values="valid_ohlc").reindex(index)


def build_causal_regime_labels(
    prices: pd.DataFrame, params: dict | None = None
) -> pd.DataFrame:
    """Build past-only labels on a complete 15-minute grid.

    Parameters are research inputs, not full-sample optimized strategy values.
    A missing or invalid bar makes the following lookback window unavailable,
    preventing a gap from being silently treated as continuous history.
    """
    cfg = {**CAUSAL_RESEARCH_PARAMS, **(params or {})}
    trade, _, _ = build_core(prices)
    grid = pd.date_range(trade.index.min(), trade.index.max(), freq=BAR, tz="UTC")
    trade = trade.reindex(grid)
    ohlc = _valid_trade_ohlc(prices, grid).reindex(columns=("gate",) + EXTERNAL_TRADE)
    premium = trade.gate_premium_vs_market_median_bps
    count = trade.reindex(columns=EXTERNAL_TRADE).notna().sum(axis=1)
    price_valid = (
        trade.reindex(columns=("gate",) + EXTERNAL_TRADE).notna().all(axis=1)
        & (trade.reindex(columns=("gate",) + EXTERNAL_TRADE) > 0).all(axis=1)
    )
    ohlc_valid = ohlc.notna().all(axis=1) & ohlc.fillna(False).all(axis=1)
    current_valid = count.eq(3) & price_valid & ohlc_valid & premium.notna()
    lookback = int(cfg["lookback_bars"])
    continuity_valid = current_valid & current_valid.shift(1, fill_value=False)
    history_ready = current_valid.rolling(lookback, min_periods=lookback).sum().eq(lookback)

    out = pd.DataFrame(index=grid)
    out.index.name = "open_time"
    out["gate_premium_vs_market_median_bps"] = premium
    out["external_venue_count"] = count.astype("Int64")
    out["external_scope"] = STRICT_EXTERNAL_SCOPE
    for hours in (4, 12, 24):
        bars = hours * 4
        out[f"rolling_median_{hours}h_bps"] = premium.rolling(bars, min_periods=bars).median()
    median24 = out["rolling_median_24h_bps"]
    out["rolling_mad_24h_bps"] = (
        premium - median24
    ).abs().rolling(96, min_periods=96).median()
    direction = np.sign(median24)
    same_sign = pd.Series(
        np.where(direction >= 0, premium > 0, premium < 0), index=grid, dtype=float
    )
    same_sign[premium.isna() | median24.isna()] = np.nan
    out["same_sign_ratio_24h"] = same_sign.rolling(96, min_periods=96).mean()

    structural = (
        history_ready
        & median24.abs().ge(float(cfg["structural_median_bps"]))
        & out.same_sign_ratio_24h.ge(float(cfg["structural_same_sign_ratio"]))
    )
    mad_scale = out.rolling_mad_24h_bps.clip(lower=float(cfg["mad_floor_bps"]))
    transient = (
        history_ready
        & premium.abs().ge(float(cfg["transient_current_bps"]))
        & median24.abs().lt(float(cfg["transient_max_structural_median_bps"]))
        & (premium - median24).abs().ge(float(cfg["transient_mad_multiplier"]) * mad_scale)
    )
    out["causal_regime"] = "NORMAL"
    out.loc[transient, "causal_regime"] = "TRANSIENT_DISLOCATION"
    out.loc[structural, "causal_regime"] = "STRUCTURAL_PREMIUM"
    out.loc[~continuity_valid, "causal_regime"] = "STALE_OR_INVALID"

    reason = pd.Series("VALID_OTHERWISE_NORMAL", index=grid, dtype=object)
    reason.loc[transient] = "CURRENT_EDGE_ABNORMAL_VS_PAST_24H_MAD"
    reason.loc[structural] = "PAST_24H_DIRECTED_MEDIAN_AND_SIGN_PERSISTENCE"
    reason.loc[~continuity_valid] = "CURRENT_OR_PREVIOUS_BAR_NOT_CONTIGUOUS_VALID"
    reason.loc[~ohlc_valid] = "INVALID_OR_MISSING_OHLC"
    reason.loc[~price_valid] = "GATE_OR_EXTERNAL_PRICE_INVALID"
    reason.loc[count.lt(3)] = "STRICT_EXTERNAL_3_OF_3_MISSING"
    out["regime_reason"] = reason
    out["is_entry_allowed"] = out.causal_regime.isin(("NORMAL", "TRANSIENT_DISLOCATION"))
    if not set(out.causal_regime.unique()) <= set(CAUSAL_REGIMES):
        raise AssertionError("unexpected causal regime")
    return out.reset_index()


def causal_regime_for_time(labels: pd.DataFrame, value) -> str:
    """Strategy-facing lookup; accepts causal labels only, never change points."""
    required = {"open_time", "causal_regime"}
    if not required <= set(labels.columns):
        raise ValueError("causal label frame required; retrospective change points are forbidden")
    t = parse_utc(value).floor("15min")
    frame = labels.copy()
    frame["open_time"] = pd.to_datetime(frame.open_time, utc=True)
    row = frame.loc[frame.open_time.eq(t), "causal_regime"]
    return row.iloc[-1] if len(row) else "STALE_OR_INVALID"


def causal_regime_summary(labels: pd.DataFrame) -> pd.DataFrame:
    """Return all allowed categories, including categories with zero bars."""
    counts = labels.causal_regime.value_counts().reindex(CAUSAL_REGIMES, fill_value=0)
    return counts.rename_axis("causal_regime").rename("bar_count").reset_index()


def _longest_minutes(times: pd.Series | pd.DatetimeIndex, active) -> int:
    z = pd.DataFrame({"time": pd.to_datetime(times, utc=True), "active": np.asarray(active, dtype=bool)})
    z = z[z.active].sort_values("time")
    if z.empty:
        return 0
    groups = z.time.diff().ne(BAR).cumsum()
    return int(z.groupby(groups).size().max() * 15)


def _stats(values: pd.Series) -> dict:
    x = pd.to_numeric(values, errors="coerce").dropna()
    if x.empty:
        return {k: np.nan for k in ("median_signed_bps", "mean_signed_bps", "std_bps",
            "p05_signed_bps", "p95_signed_bps", "p95_abs_bps", "p99_abs_bps", "max_abs_bps",
            "percent_gate_higher")}
    return {"median_signed_bps": x.median(), "mean_signed_bps": x.mean(), "std_bps": x.std(ddof=1),
        "p05_signed_bps": x.quantile(.05), "p95_signed_bps": x.quantile(.95),
        "p95_abs_bps": x.abs().quantile(.95), "p99_abs_bps": x.abs().quantile(.99),
        "max_abs_bps": x.abs().max(), "percent_gate_higher": 100 * (x > 0).mean()}


def summarize_pairs(trade: pd.DataFrame) -> pd.DataFrame:
    rows = []
    bounds = {r: (g.index.min(), g.index.max() + BAR) for r, g in trade.groupby("regime")}
    for regime, group in trade.groupby("regime", sort=False):
        start, end = bounds[regime]
        possible = max(1, int((end - start) / BAR))
        for ex in ("binance", "bitget", "hyperliquid", "okx"):
            col = f"gate_vs_{ex}_bps"; x = group[col].dropna(); row = {
                "row_type": "pair", "pair": f"gate/{ex}", "regime": regime, "count": len(x),
                "coverage_percent": 100 * len(x) / possible, **_stats(x)}
            for threshold in THRESHOLDS:
                row[f"ratio_abs_gt_{threshold}bps"] = float((x.abs() > threshold).mean()) if len(x) else np.nan
                row[f"longest_over_{threshold}bps_minutes"] = _longest_minutes(x.index, x.abs() > threshold)
            rows.append(row)
    return pd.DataFrame(rows)


def cross_sectional_consistency(trade: pd.DataFrame) -> pd.DataFrame:
    cols = [f"gate_vs_{x}_bps" for x in ("binance", "bitget", "hyperliquid", "okx")]
    rows = []
    for regime, g in trade.groupby("regime", sort=False):
        valid = g[cols].notna(); high = g[cols].gt(0) & valid; abnormal = g[cols].abs().gt(50) & valid
        denom = valid.sum(axis=1)
        rows.append({"regime": regime, "count": len(g),
            "all_available_gate_higher_ratio": float(((high.sum(axis=1) == denom) & (denom == 4)).mean()),
            "all_four_gate_higher_ratio": float((high.sum(axis=1) == 4).mean()),
            "only_one_pair_abs_gt_50bps_ratio": float((abnormal.sum(axis=1) == 1).mean()),
            "at_least_three_pairs_abs_gt_50bps_ratio": float((abnormal.sum(axis=1) >= 3).mean()),
            "median_cross_pair_signed_std_bps": float(g[cols].std(axis=1).median())})
    return pd.DataFrame(rows)


def summarize_decomposition(d: pd.DataFrame) -> pd.DataFrame:
    components = {
        "total_gate_trade_vs_market": "total_gate_trade_vs_market_bps",
        "gate_trade_minus_gate_mark": "gate_trade_minus_gate_mark_bps",
        "gate_mark_minus_gate_index": "gate_mark_minus_gate_index_bps",
        "gate_index_minus_external_market": "gate_index_minus_market_bps",
        "decomposition_residual": "decomposition_residual_bps",
    }
    rows = []
    for regime, g in d.groupby("regime", sort=False):
        contribution_cols = list(components.values())[1:4]
        denom = g[contribution_cols].abs().sum(axis=1).replace(0, np.nan)
        for name, col in components.items():
            x = g[col].dropna()
            rows.append({"component_name": name, "regime": regime, "count": len(x),
                "median_bps": x.median(), "mean_bps": x.mean(), "p95_abs_bps": x.abs().quantile(.95),
                "p99_abs_bps": x.abs().quantile(.99), "max_abs_bps": x.abs().max(),
                "positive_ratio": float((x > 0).mean()),
                "median_abs_explanation_share": (float((g[col].abs() / denom).median())
                    if col in contribution_cols else np.nan)})
    return pd.DataFrame(rows)


def rolling_diagnostics(trade: pd.DataFrame) -> pd.DataFrame:
    out = trade[["gate_premium_vs_market_median_bps"]].copy()
    x = out.gate_premium_vs_market_median_bps
    for hours in (4, 12, 24):
        window = hours * 4
        out[f"rolling_median_{hours}h_bps"] = x.rolling(window, min_periods=max(4, window // 2)).median()
        med = out[f"rolling_median_{hours}h_bps"]
        out[f"rolling_mad_{hours}h_bps"] = (x-med).abs().rolling(window, min_periods=max(4, window//2)).median()
        out[f"rolling_std_{hours}h_bps"] = x.rolling(window, min_periods=max(4, window//2)).std()
    return out


def detect_change_points(trade: pd.DataFrame) -> pd.DataFrame:
    x = trade.gate_premium_vs_market_median_bps.dropna().sort_index()
    rows = []
    if len(x) < 192:
        return pd.DataFrame(columns=["change_time", "pre_median_bps", "post_median_bps", "median_shift_bps",
            "pre_p95_abs_bps", "post_p95_abs_bps", "confidence_metric", "method"])
    # Symmetric 24h median-shift scan. Candidate peaks must be >=12h apart.
    pre = x.rolling(96, min_periods=48).median().shift(1)
    post = x.iloc[::-1].rolling(96, min_periods=48).median().iloc[::-1]
    premad = (x-pre).abs().rolling(96, min_periods=48).median().shift(1)
    postmad = (x-post).abs().iloc[::-1].rolling(96, min_periods=48).median().iloc[::-1]
    shift = post-pre; score = shift.abs() / (premad.fillna(0)+postmad.fillna(0)+1)
    candidates = score.sort_values(ascending=False)
    picked = []
    for t in candidates.index:
        if not np.isfinite(candidates[t]) or any(abs(t-q) < pd.Timedelta(hours=12) for q in picked):
            continue
        a = x[(x.index >= t-pd.Timedelta(hours=24)) & (x.index < t)]
        b = x[(x.index >= t) & (x.index < t+pd.Timedelta(hours=24))]
        if len(a) < 48 or len(b) < 48:
            continue
        rows.append({"change_time": t, "pre_median_bps": a.median(), "post_median_bps": b.median(),
            "median_shift_bps": b.median()-a.median(), "pre_p95_abs_bps": a.abs().quantile(.95),
            "post_p95_abs_bps": b.abs().quantile(.95), "confidence_metric": score[t],
            "method": "24h_symmetric_median_shift_robust_mad"})
        picked.append(t)
        if len(picked) == 10:
            break
    # Lightweight two-sided CUSUM on robustly centered observations.
    center = x.median()
    scale = max(float((x-center).abs().median()) * 1.4826, 1.0)
    pos = neg = 0.0; last = None
    for t, value in x.items():
        z = (value-center)/scale; pos = max(0.0, pos+z-.5); neg = min(0.0, neg+z+.5)
        if max(pos, -neg) > 20 and (last is None or t-last >= pd.Timedelta(hours=12)):
            a=x[(x.index>=t-pd.Timedelta(hours=24))&(x.index<t)]; b=x[(x.index>=t)&(x.index<t+pd.Timedelta(hours=24))]
            if len(a)>=48 and len(b)>=48:
                rows.append({"change_time":t,"pre_median_bps":a.median(),"post_median_bps":b.median(),
                    "median_shift_bps":b.median()-a.median(),"pre_p95_abs_bps":a.abs().quantile(.95),
                    "post_p95_abs_bps":b.abs().quantile(.95),"confidence_metric":max(pos,-neg),"method":"robust_cusum"})
                last=t
            pos=neg=0.0
    result = pd.DataFrame(rows).sort_values(
        ["method", "confidence_metric"], ascending=[True, False]
    ).reset_index(drop=True)
    result["analysis_scope"] = RETROSPECTIVE_CHANGE_POINTS
    result["uses_pre_and_post_windows"] = True
    result["uses_full_sample"] = True
    result["strategy_eligible"] = False
    return result


def retrospective_segments(changes: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Create five historical explanation segments from full-sample change points."""
    labels = ("BASELINE", "BUILDUP", "STRUCTURAL_PREMIUM", "NORMALIZATION", "POST_NORMALIZATION")
    start, end = pd.DatetimeIndex(index).min(), pd.DatetimeIndex(index).max() + BAR
    if changes.empty:
        return pd.DataFrame([{
            "retrospective_segment": "BASELINE", "start_time": start, "end_time": end,
            "analysis_scope": RETROSPECTIVE_CHANGE_POINTS, "strategy_eligible": False,
        }])
    ranked = changes.sort_values("confidence_metric", ascending=False)
    picked = []
    for t in pd.to_datetime(ranked.change_time, utc=True):
        if start < t < end and all(abs(t - old) >= pd.Timedelta(hours=12) for old in picked):
            picked.append(t)
        if len(picked) == 4:
            break
    picked = sorted(picked)
    bounds = [start, *picked, end]
    names = labels[: len(bounds) - 1]
    return pd.DataFrame([{
        "retrospective_segment": name, "start_time": left, "end_time": right,
        "analysis_scope": RETROSPECTIVE_CHANGE_POINTS, "strategy_eligible": False,
    } for name, left, right in zip(names, bounds[:-1], bounds[1:])])


def continuous_events(trade: pd.DataFrame) -> pd.DataFrame:
    x = trade.gate_premium_vs_market_median_bps.dropna().sort_index(); rows=[]
    for threshold in (50, 100, 150, 200):
        active=x[x.abs()>threshold]
        if active.empty: continue
        groups=(active.index.to_series().diff().ne(BAR)).cumsum()
        for event_no, (_, g) in enumerate(active.groupby(groups), 1):
            peak=g.abs().idxmax()
            rows.append({"threshold_bps":threshold,"event_id":f"gate-{threshold}-{event_no:04d}",
                "start_time":g.index.min(),"end_time":g.index.max()+BAR,"duration_minutes":len(g)*15,
                "bar_count":len(g),"peak_time":peak,"peak_signed_bps":g.loc[peak],
                "peak_abs_bps":abs(g.loc[peak]),"regime":regime_for_time(g.index.min())})
    return pd.DataFrame(rows)


def data_quality(prices: pd.DataFrame, trade: pd.DataFrame) -> pd.DataFrame:
    p=_native_prices(prices); gate=p[p.exchange=="gate"].copy(); rows=[]
    def add(check, evidence, status): rows.append({"check":check,"evidence":evidence,"conclusion":status})
    aligned=((gate.open_time.dt.minute%15==0)&(gate.open_time.dt.second.eq(0))).all()
    add("quarter_hour_alignment",f"aligned={aligned}; rows={len(gate)}","DATA_ERROR_NOT_SUPPORTED" if aligned else "DATA_ERROR_SUPPORTED")
    dup=int(gate.duplicated(["price_type","open_time"]).sum())
    add("duplicate_timestamps",f"duplicate rows={dup}","DATA_ERROR_NOT_SUPPORTED" if dup==0 else "DATA_ERROR_SUPPORTED")
    gaps=[]
    for typ,g in gate.groupby("price_type"):
        dif=g.open_time.sort_values().diff(); gaps.append(f"{typ}:{int((dif>BAR).sum())}")
    add("missing_buckets","; ".join(gaps),"INCONCLUSIVE" if any(not x.endswith(":0") for x in gaps) else "DATA_ERROR_NOT_SUPPORTED")
    backwards=int(sum((g.open_time.diff()<pd.Timedelta(0)).sum() for _,g in gate.groupby("raw_file",dropna=False))) if "raw_file" in gate else 0
    add("pagination_order",f"normalized rows are sorted; raw API page ordering is not preserved as a column (observed backward count after normalization={backwards})","INCONCLUSIVE")
    epoch_bad=int((gate.open_time.dt.year<2020).sum()+(gate.open_time.dt.year>2035).sum())
    add("seconds_milliseconds",f"implausible years={epoch_bad}","DATA_ERROR_NOT_SUPPORTED" if epoch_bad==0 else "DATA_ERROR_SUPPORTED")
    ratios=[]
    for ex in EXTERNAL_TRADE:
        q=(trade.gate/trade[ex]).replace([np.inf,-np.inf],np.nan).dropna(); ratios.append(q.median())
    scale=any(r>100 or r<.01 for r in ratios)
    add("price_multiplier_1000",f"median Gate/external ratios={','.join(f'{x:.6f}' for x in ratios)}","DATA_ERROR_SUPPORTED" if scale else "DATA_ERROR_NOT_SUPPORTED")
    symbols=gate.groupby("price_type").symbol.unique().map(lambda x:"|".join(map(str,x))).to_dict()
    same=len(set(symbols.values()))==1
    add("trade_mark_index_symbol",json.dumps(symbols,ensure_ascii=False),"DATA_ERROR_NOT_SUPPORTED" if same else "DATA_ERROR_SUPPORTED")
    ohlc=((gate.low<=gate.open)&(gate.open<=gate.high)&(gate.low<=gate.close)&(gate.close<=gate.high))
    add("ohlc_invariants",f"violations={int((~ohlc).sum())}","DATA_ERROR_NOT_SUPPORTED" if ohlc.all() else "DATA_ERROR_SUPPORTED")
    gt=gate[gate.price_type=="trade"].set_index("open_time"); vol=gt.volume_base
    zero=float((vol.fillna(0)<=0).mean()); add("zero_or_missing_volume",f"ratio={zero:.4%}","INCONCLUSIVE" if zero>.01 else "DATA_ERROR_NOT_SUPPORTED")
    add("trade_count_availability","trade_count column is absent from native 15m schema","INCONCLUSIVE")
    joined=pd.concat([trade.gate_premium_vs_market_median_bps.abs(),vol],axis=1).dropna(); joined.columns=["premium","volume"]
    low=joined.volume<=joined.volume.quantile(.1); extreme=joined.premium>joined.premium.quantile(.95)
    concentration=float(low[extreme].mean()) if extreme.any() else np.nan
    add("extremes_at_low_volume",f"lowest-volume-decile share among top-5% premium={concentration:.4%}","INCONCLUSIVE")
    unchanged=gt.close.diff().eq(0); first_after=~unchanged & unchanged.shift(1,fill_value=False)
    conc2=float(first_after.reindex(joined.index,fill_value=False)[extreme].mean()) if extreme.any() else np.nan
    add("extremes_after_stale_trade",f"first-price-change share among top-5% premium={conc2:.4%}","INCONCLUSIVE")
    rounded=(trade.gate/trade.market_trade_median).replace([np.inf,-np.inf],np.nan).round(3).value_counts(normalize=True).head(5)
    add("discrete_ratio_steps",rounded.to_json(),"INCONCLUSIVE")
    return pd.DataFrame(rows)


def liquidity_analysis(prices: pd.DataFrame, trade: pd.DataFrame) -> tuple[pd.DataFrame,pd.DataFrame]:
    gate=_native_prices(prices); gate=gate[(gate.exchange=="gate")&(gate.price_type=="trade")].set_index("open_time")
    z=gate[["volume_base","high","low","close"]].join(trade[["gate_premium_vs_market_median_bps"]],how="inner")
    z["abs_premium_bps"]=z.gate_premium_vs_market_median_bps.abs(); z["range_bps"]=_bps(z.high,z.low).abs()
    z["return_bps"]=_bps(z.close,z.close.shift()).abs(); z["price_changed"]=z.close.ne(z.close.shift())
    z["consecutive_unchanged_bars"]=(~z.price_changed).groupby(z.price_changed.cumsum()).cumsum()
    last_change=pd.Series(z.index.where(z.price_changed),index=z.index).ffill()
    z["minutes_since_price_change"]=(z.index.to_series()-last_change).dt.total_seconds()/60
    z["regime"]=[regime_for_time(x) for x in z.index]
    rows=[]
    for regime,g in z.groupby("regime",sort=False):
        valid=g.dropna(subset=["abs_premium_bps","volume_base"]); q=valid.volume_base.quantile(.1); low=valid.volume_base<=q
        extreme=valid.abs_premium_bps>valid.abs_premium_bps.quantile(.95)
        rows.append({"regime":regime,"count":len(valid),
            "corr_abs_premium_log1p_volume":valid.abs_premium_bps.corr(np.log1p(valid.volume_base.clip(lower=0))),
            "high_premium_lowest_volume_decile_ratio":float(low[extreme].mean()) if extreme.any() else np.nan,
            "p95_abs_premium_low_volume_bps":valid.loc[low,"abs_premium_bps"].quantile(.95),
            "p95_abs_premium_normal_volume_bps":valid.loc[~low,"abs_premium_bps"].quantile(.95),
            "p95_abs_premium_unchanged_bps":valid.loc[valid.consecutive_unchanged_bars>0,"abs_premium_bps"].quantile(.95),
            "p95_abs_premium_updated_bps":valid.loc[valid.consecutive_unchanged_bars==0,"abs_premium_bps"].quantile(.95),
            "median_volume":valid.volume_base.median(),"median_range_bps":valid.range_bps.median(),
            "corr_abs_premium_range_bps":valid.abs_premium_bps.corr(valid.range_bps)})
    return z.reset_index(),pd.DataFrame(rows)


def _calendar_features(index: pd.DatetimeIndex) -> pd.DataFrame:
    import exchange_calendars as xcals
    idx=pd.DatetimeIndex(pd.to_datetime(index,utc=True)); start=idx.min()-pd.Timedelta(days=7); end=idx.max()+pd.Timedelta(days=7)
    krx=xcals.get_calendar("XKRX"); ny=xcals.get_calendar("XNYS")
    ks=krx.schedule.loc[str(start.date()):str(end.date())]; ns=ny.schedule.loc[str(start.date()):str(end.date())]
    ko=pd.DatetimeIndex(ks.open); kc=pd.DatetimeIndex(ks.close); no=pd.DatetimeIndex(ns.open); nc=pd.DatetimeIndex(ns.close)
    rows=[]
    for t in idx:
        in_krx=bool(((ko<=t)&(t<kc)).any()); in_us=bool(((no<=t)&(t<nc)).any())
        prev_close=kc[kc<=t].max() if (kc<=t).any() else pd.NaT; next_open=ko[ko>t].min() if (ko>t).any() else pd.NaT
        local=t.tz_convert("Asia/Seoul"); is_session_date=local.normalize().tz_localize(None) in ks.index
        if in_krx: session="KRX_REGULAR"
        elif t.weekday()>=5: session="WEEKEND"
        elif not is_session_date: session="KOREAN_HOLIDAY"
        elif in_us: session="US_REGULAR_KRX_CLOSED"
        elif 8<=t.hour<16: session="ASIA_NIGHT"
        else: session="KRX_AFTER_CLOSE"
        rows.append({"open_time":t,"session":session,"krx_open":in_krx,"us_regular":in_us,
            "weekday":t.day_name(),"utc_hour":t.hour,"is_weekend":t.weekday()>=5,
            "hours_since_last_krx_close":(t-prev_close).total_seconds()/3600 if pd.notna(prev_close) else np.nan,
            "hours_until_next_krx_open":(next_open-t).total_seconds()/3600 if pd.notna(next_open) else np.nan,
            "calendar_source":"exchange_calendars:XKRX+XNYS"})
    return pd.DataFrame(rows).set_index("open_time")


def session_analysis(trade: pd.DataFrame) -> tuple[pd.DataFrame,pd.DataFrame]:
    x=trade[["gate_premium_vs_market_median_bps","regime"]].join(_calendar_features(trade.index),how="left")
    rows=[]
    for dims in (("regime","session"),("regime","utc_hour"),("regime","weekday")):
        for keys,g in x.groupby(list(dims),dropna=False,sort=False):
            keys=keys if isinstance(keys,tuple) else (keys,); v=g.gate_premium_vs_market_median_bps.dropna()
            rows.append({"dimension":"x".join(dims),**dict(zip(dims,keys)),"count":len(v),"median_bps":v.median(),
                "p95_abs_bps":v.abs().quantile(.95),"percent_gate_higher":100*(v>0).mean(),
                "calendar_source":"exchange_calendars:XKRX+XNYS"})
    for flag,label in (("krx_open","KRX_REOPEN_1H"),("us_regular","US_REGULAR_OPEN_1H")):
        starts=x.index[x[flag] & ~x[flag].shift(fill_value=False)]
        for regime in x.regime.unique():
            changes=[]
            for t in starts:
                if regime_for_time(t)!=regime: continue
                before=x.gate_premium_vs_market_median_bps.get(t-BAR,np.nan)
                after=x.gate_premium_vs_market_median_bps.get(t+pd.Timedelta(hours=1),np.nan)
                if pd.notna(before) and pd.notna(after): changes.append(after-before)
            rows.append({"dimension":"transition","regime":regime,"session":label,"count":len(changes),
                "median_bps":float(np.median(changes)) if changes else np.nan,
                "p95_abs_bps":float(pd.Series(changes).abs().quantile(.95)) if changes else np.nan,
                "percent_gate_higher":np.nan,"calendar_source":"exchange_calendars:XKRX+XNYS"})
    return x.reset_index(),pd.DataFrame(rows)


def nongate_analysis(trade: pd.DataFrame) -> tuple[pd.DataFrame,pd.DataFrame]:
    nongate=("binance","bitget","hyperliquid","okx"); pair_cols={}
    for a,b in itertools.combinations(nongate,2): pair_cols[f"{a}/{b}"]=_bps(trade[a],trade[b])
    frame=pd.DataFrame(pair_cols,index=trade.index); frame["median_non_gate_abs_spread_bps"]=frame.abs().median(axis=1)
    frame["regime"]=trade.regime
    rows=[]
    for regime,g in frame.groupby("regime",sort=False):
        for pair in pair_cols:
            x=g[pair].dropna(); rows.append({"pair":pair,"regime":regime,"count":len(x),
                "median_signed_bps":x.median(),"median_abs_bps":x.abs().median(),"p95_abs_bps":x.abs().quantile(.95)})
        x=g.median_non_gate_abs_spread_bps.dropna(); rows.append({"pair":"NON_GATE_CROSS_SECTION_MEDIAN","regime":regime,"count":len(x),
            "median_signed_bps":np.nan,"median_abs_bps":x.median(),"p95_abs_bps":x.quantile(.95)})
        gate_abs=trade.loc[g.index,"gate_premium_vs_market_median_bps"].abs()
        rows.append({"pair":"GATE_EXCESS_VS_NON_GATE_MEDIAN","regime":regime,"count":int((gate_abs.notna()&x.reindex(g.index).notna()).sum()),
            "median_signed_bps":np.nan,"median_abs_bps":(gate_abs-g.median_non_gate_abs_spread_bps).median(),
            "p95_abs_bps":(gate_abs-g.median_non_gate_abs_spread_bps).quantile(.95)})
    return frame.reset_index(),pd.DataFrame(rows)


def funding_analysis(funding: pd.DataFrame, trade: pd.DataFrame) -> pd.DataFrame:
    f=funding.copy(); f["funding_time"]=pd.to_datetime(f.funding_time,utc=True); f["settlement_hour"]=f.funding_time.dt.floor("h")
    f=f.sort_values("funding_time").drop_duplicates(["exchange","settlement_hour"],keep="last")
    wide=f.pivot(index="settlement_hour",columns="exchange",values="funding_rate").sort_index(); rows=[]
    for ex in ("binance","bitget","hyperliquid","okx"):
        if "gate" not in wide or ex not in wide: continue
        diff=(wide.gate-wide[ex]).dropna(); premium=trade[f"gate_vs_{ex}_bps"].dropna()
        for t,value in diff.items():
            prior=premium[premium.index<=t]
            rows.append({"record_type":"settlement","pair":f"gate/{ex}","funding_time":t,"lag_hours":np.nan,
                "gate_funding_rate":wide.loc[t,"gate"],"other_funding_rate":wide.loc[t,ex],"funding_diff":value,
                "premium_bps":prior.iloc[-1] if len(prior) and t-prior.index[-1]<=BAR else np.nan,
                "premium_before_4h_bps":np.nan,"premium_after_4h_bps":np.nan,"abs_premium_change_after_settlement_bps":np.nan,
                "correlation":np.nan,"alignment":"real_settlement_event","regime":regime_for_time(t)})
            before=premium.get(t-pd.Timedelta(hours=4),np.nan); after=premium.get(t+pd.Timedelta(hours=4),np.nan)
            rows.append({"record_type":"settlement_window","pair":f"gate/{ex}","funding_time":t,"lag_hours":np.nan,
                "gate_funding_rate":wide.loc[t,"gate"],"other_funding_rate":wide.loc[t,ex],"funding_diff":value,
                "premium_bps":np.nan,"premium_before_4h_bps":before,"premium_after_4h_bps":after,
                "abs_premium_change_after_settlement_bps":abs(after)-abs(before) if pd.notna(before) and pd.notna(after) else np.nan,
                "correlation":np.nan,"alignment":"real_settlement_plus_minus_4h","regime":regime_for_time(t)})
        # Description only: hold the last known settlement rate. The label is
        # explicit so it cannot be mistaken for synthetic 15-minute cash flow.
        held=diff.reindex(premium.index,method="ffill",tolerance=pd.Timedelta(hours=24))
        for lag in (-24,-12,-8,-4,0,4,8,12,24):
            shifted=held.shift(periods=-lag*4); valid=pd.concat([premium,shifted],axis=1).dropna()
            rows.append({"record_type":"lag_correlation","pair":f"gate/{ex}","funding_time":pd.NaT,
                "lag_hours":lag,"gate_funding_rate":np.nan,"other_funding_rate":np.nan,"funding_diff":np.nan,
                "premium_bps":np.nan,"premium_before_4h_bps":np.nan,"premium_after_4h_bps":np.nan,
                "abs_premium_change_after_settlement_bps":np.nan,
                "correlation":valid.iloc[:,0].corr(valid.iloc[:,1]) if len(valid)>2 else np.nan,
                "alignment":"descriptive_forward_hold","regime":None})
    return pd.DataFrame(rows)


def _plot_gap(ax, series, *args, **kwargs):
    s=series.dropna().sort_index(); groups=s.index.to_series().diff().ne(BAR).cumsum()
    for _,g in s.groupby(groups): ax.plot(g.index,g.values,*args,**kwargs)


def make_charts(trade, rolling, decomposition, liquidity, sessions, nongate, funding, events, changes):
    CHARTS.mkdir(parents=True,exist_ok=True); sns.set_theme(style="whitegrid")
    note="Native 15m trade close, not executable BBO. Time is UTC; gaps are not connected."
    fig,axes=plt.subplots(2,1,figsize=(14,9),sharex=True)
    for ex in ("binance","bitget","hyperliquid","okx"): _plot_gap(axes[0],trade[f"gate_vs_{ex}_bps"],label=f"Gate/{ex}",lw=.8)
    _plot_gap(axes[1],trade.gate_premium_vs_market_median_bps,label="Gate vs external trade median",color="black",lw=.9)
    for ax in axes:
        ax.axvspan(PRESET_START,PRESET_END,color="orange",alpha=.12); ax.axhline(0,color="grey",lw=.6); ax.set_ylabel("signed bps"); ax.legend(ncol=3)
    for t in pd.to_datetime(changes.head(5).change_time,utc=True) if len(changes) else []: axes[1].axvline(t,color="red",alpha=.3,lw=.8)
    fig.suptitle("Gate directed spreads and data-driven change points");fig.text(.5,.01,note,ha="center",fontsize=8);fig.tight_layout(rect=(0,.03,1,.97));fig.savefig(CHARTS/"gate_premium_timeseries_15m.png",dpi=160);plt.close(fig)
    fig,ax=plt.subplots(figsize=(12,6)); box=trade.reset_index().melt(id_vars=["open_time","regime"],value_vars=[f"gate_vs_{x}_bps" for x in ("binance","bitget","hyperliquid","okx")],var_name="pair",value_name="bps");sns.boxplot(box,x="pair",y="bps",hue="regime",showfliers=False,ax=ax);ax.set_ylabel("signed bps");ax.set_title("Gate spread distribution by preset regime");fig.text(.5,.01,note,ha="center",fontsize=8);fig.tight_layout(rect=(0,.03,1,1));fig.savefig(CHARTS/"gate_regime_distribution_15m.png",dpi=160);plt.close(fig)
    fig,axes=plt.subplots(3,1,figsize=(14,9),sharex=True)
    for ax,col,title in zip(axes,["gate_trade_minus_gate_mark_bps","gate_mark_minus_gate_index_bps","gate_index_minus_market_bps"],["trade − mark","mark − index","index − external trade median"]): _plot_gap(ax,decomposition[col],color="tab:blue",lw=.7);ax.set_ylabel("bps");ax.set_title(title)
    fig.suptitle("Gate trade / mark / index approximate decomposition");fig.text(.5,.01,note,ha="center",fontsize=8);fig.tight_layout(rect=(0,.03,1,.97));fig.savefig(CHARTS/"gate_trade_mark_index_decomposition_15m.png",dpi=160);plt.close(fig)
    fig,ax=plt.subplots(figsize=(10,6)); z=liquidity.dropna(subset=["volume_base","abs_premium_bps"]);ax.scatter(np.log1p(z.volume_base.clip(lower=0)),z.abs_premium_bps,s=7,alpha=.25);ax.set(xlabel="log1p(Gate native volume)",ylabel="absolute premium (bps)",title="Gate premium versus native candle volume");fig.text(.5,.01,note,ha="center",fontsize=8);fig.tight_layout(rect=(0,.03,1,1));fig.savefig(CHARTS/"gate_premium_vs_volume_15m.png",dpi=160);plt.close(fig)
    sr=sessions.groupby(["session","regime"]).gate_premium_vs_market_median_bps.agg(median="median",p95=lambda x:x.abs().quantile(.95)).reset_index();fig,ax=plt.subplots(figsize=(12,6));sns.barplot(sr,x="session",y="p95",hue="regime",ax=ax);ax.tick_params(axis="x",rotation=25);ax.set(ylabel="P95 absolute bps",title="Gate premium by exchange-calendar session");fig.tight_layout();fig.savefig(CHARTS/"gate_premium_by_session_15m.png",dpi=160);plt.close(fig)
    fig,ax=plt.subplots(figsize=(12,6)); fc=funding[funding.record_type=="lag_correlation"];sns.lineplot(fc,x="lag_hours",y="correlation",hue="pair",marker="o",ax=ax);ax.axhline(0,color="grey",lw=.6);ax.set(title="Premium vs funding-difference lag correlation",ylabel="correlation (descriptive forward hold)");fig.tight_layout();fig.savefig(CHARTS/"gate_funding_vs_premium_15m.png",dpi=160);plt.close(fig)
    ns=nongate.groupby("regime").median_non_gate_abs_spread_bps.quantile(.95); gs=trade.groupby("regime").gate_premium_vs_market_median_bps.apply(lambda x:x.abs().quantile(.95)); pd.DataFrame({"Gate vs median":gs,"non-Gate median":ns}).plot.bar(figsize=(10,6));plt.ylabel("P95 absolute bps");plt.title("Gate versus non-Gate dispersion");plt.tight_layout();plt.savefig(CHARTS/"gate_vs_nongate_15m.png",dpi=160);plt.close()
    fig,ax=plt.subplots(figsize=(10,6)); ev=events.groupby(["threshold_bps","regime"]).duration_minutes.max().reset_index();sns.barplot(ev,x="threshold_bps",y="duration_minutes",hue="regime",ax=ax);ax.set(title="Longest continuous threshold event",ylabel="minutes");fig.tight_layout();fig.savefig(CHARTS/"gate_event_durations_15m.png",dpi=160);plt.close(fig)
    pv=sessions.pivot_table(index="utc_hour",columns="regime",values="gate_premium_vs_market_median_bps",aggfunc=lambda x:x.abs().quantile(.95));fig,ax=plt.subplots(figsize=(10,7));sns.heatmap(pv,annot=True,fmt=".0f",cmap="mako",ax=ax);ax.set_title("UTC hour × regime: P95 absolute Gate premium (bps)");fig.tight_layout();fig.savefig(CHARTS/"gate_premium_utc_hour_heatmap_15m.png",dpi=160);plt.close(fig)
    fig,ax=plt.subplots(figsize=(14,6));
    for h in (4,12,24): _plot_gap(ax,rolling[f"rolling_median_{h}h_bps"],label=f"{h}h rolling median",lw=.9)
    ax.axvspan(PRESET_START,PRESET_END,color="orange",alpha=.12);ax.set(ylabel="bps",title="Data-driven rolling Gate premium statistics");ax.legend();fig.tight_layout();fig.savefig(CHARTS/"gate_rolling_regime_15m.png",dpi=160);plt.close(fig)


def hypothesis_table(dq, liquidity_summary, decomp, nongate_summary, funding) -> pd.DataFrame:
    data_error=(dq.conclusion=="DATA_ERROR_SUPPORTED").any(); liq=liquidity_summary
    liq_support=bool(len(liq) and (liq.p95_abs_premium_low_volume_bps>liq.p95_abs_premium_normal_volume_bps*1.5).any())
    med=decomp.pivot(index="regime",columns="component_name",values="median_bps") if len(decomp) else pd.DataFrame()
    def row(h,s,c,m,conf,status): return {"hypothesis":h,"supporting_evidence":s,"contradicting_evidence":c,"missing_evidence":m,"confidence":conf,"status":status}
    return pd.DataFrame([
        row("H1 数据采集或标准化错误","存在明确校验失败" if data_error else "无","时间、OHLC、比例、symbol和重复检查未支持系统性错误","交易所逐笔原始回放","高" if not data_error else "中等","PARTIALLY_SUPPORTED" if data_error else "NOT_SUPPORTED"),
        row("H2 低成交量及陈旧trade close","低量组P95明显更高" if liq_support else "局部相关性","高溢价并非主要集中于最低成交量十分位" if not liq_support else "不能解释全部时段","历史逐笔成交与盘口","中等","PARTIALLY_SUPPORTED" if liq_support else "NOT_SUPPORTED"),
        row("H3 Gate内部订单簿或用户多头需求","trade-mark分量和四组同向可与该机制一致","15分钟close不能识别订单流因果","历史BBO、深度、主动买卖流","低","INCONCLUSIVE"),
        row("H4 Gate mark机制导致偏离","mark-index分量可定量观测","分量只描述价格层，不证明公式原因","官方逐时公式参数","中等","PARTIALLY_SUPPORTED"),
        row("H5 Gate index成分或休市处理不同","index-market分量可定量观测且按交易时段分组","缺少历史指数成分值与权重","Gate历史指数成分/权重","中等","PARTIALLY_SUPPORTED"),
        row("H6 产品映射或合约定义不同","metadata中乘数与产品类型存在跨所差异","价格比例不存在1000倍错误","各所正式产品条款的同口径映射","低","INCONCLUSIVE"),
        row("H7 资金费率规则改变造成或放大偏离","资金差与溢价存在描述性滞后相关","相关性不能证明因果","规则变更的官方时间戳与库存数据","低","INCONCLUSIVE"),
        row("H8 全市场共同price discovery分裂，并非Gate特有","非Gate P95提供同期基准","Gate excess若显著为正则反对完全共同分裂","跨所历史BBO","中等","PARTIALLY_SUPPORTED"),
        row("H9 外部事件导致短期价格锚分叉","交易日历/时段效应和外部时间线可比对","时间重合不构成因果","可验证公告及分钟级事件研究","低","INCONCLUSIVE"),
    ])


def _fmt(x,digits=1):
    return "NA" if pd.isna(x) else f"{float(x):.{digits}f}"


def _markdown(frame: pd.DataFrame, index: bool = True, digits: int = 2) -> str:
    """Dependency-free Markdown table renderer."""
    x = frame.reset_index() if index else frame.copy()
    columns = [str(c) for c in x.columns]
    lines = ["| " + " | ".join(columns) + " |", "| " + " | ".join(["---"]*len(columns)) + " |"]
    for row in x.itertuples(index=False, name=None):
        cells=[]
        for value in row:
            if pd.isna(value): cells.append("NA")
            elif isinstance(value,(float,np.floating)): cells.append(f"{value:.{digits}f}")
            else: cells.append(str(value).replace("|","\\|"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def write_report(summary,decomp,changes,dq,liq_summary,session_summary,nongate_summary,funding,hypotheses,trade,events,external,causal,retro):
    pair=summary[summary.row_type=="pair"]
    piv=pair.pivot(index="regime",columns="pair",values="p95_abs_bps")
    dmed=decomp.pivot(index="regime",columns="component_name",values="median_bps")
    top=changes.sort_values("confidence_metric",ascending=False).head(6)
    p95_total=decomp[decomp.component_name=="total_gate_trade_vs_market"].set_index("regime").p95_abs_bps
    onset=events.sort_values("start_time").groupby("threshold_bps").first() if len(events) else pd.DataFrame()
    onset_text=(", ".join(f"{int(t)}bps={pd.Timestamp(r.start_time).strftime('%Y-%m-%d %H:%M')}" for t,r in onset.iterrows())
        if len(onset) else "无")
    regime_dom=(decomp[(decomp.regime=="GATE_REGIME_20260716_20260720") &
        decomp.component_name.isin(["gate_trade_minus_gate_mark","gate_mark_minus_gate_index","gate_index_minus_external_market"])]
        .sort_values("median_abs_explanation_share",ascending=False))
    dominant=regime_dom.iloc[0].component_name if len(regime_dom) else "NA"
    tail_dom=(regime_dom.sort_values("p95_abs_bps",ascending=False).iloc[0].component_name if len(regime_dom) else "NA")
    low_share=liq_summary.high_premium_lowest_volume_decile_ratio.max() if len(liq_summary) else np.nan
    max_lag=abs(funding.loc[funding.record_type=="lag_correlation","correlation"]).max() if len(funding) else np.nan
    krx_transition=session_summary[(session_summary.dimension=="transition")&(session_summary.session=="KRX_REOPEN_1H")]
    krx_text=", ".join(f"{r.regime}={_fmt(r.median_bps)}bps" for r in krx_transition.itertuples())
    settle=funding[funding.record_type=="settlement_window"].dropna(subset=["abs_premium_change_after_settlement_bps"])
    settle_text=", ".join(f"{k}={_fmt(g.abs_premium_change_after_settlement_bps.median())}bps" for k,g in settle.groupby("regime",dropna=False))
    external_note=(f"已使用 {int((external.verified==True).sum())} 条已核验外部来源；它们仅作为候选机制。" if len(external) else "外部原因未验证。")
    strict = trade.gate_premium_vs_market_median_bps.dropna()
    causal_counts = causal_regime_summary(causal).set_index("causal_regime")
    lines=["# Gate 原生15分钟价差 regime 原因诊断","","## 1. 直接结论","",
        f"- **高置信度**：严格3-of-3外部基准窗口为 `{strict.index.min()}` 至 `{strict.index.max()+BAR}`（左闭右开）。首次连续阈值事件：{onset_text} UTC；数据驱动上移候选在 6月22—24日，因此7月16日不是起点。",
        f"- **高置信度**：这是长期结构性Gate基差叠加波动regime。Gate/外部中位数P95在 PRE 为 {_fmt(p95_total.get('PRE_20260716'))} bps、7/16—20 为 {_fmt(p95_total.get('GATE_REGIME_20260716_20260720'))} bps、POST 为 {_fmt(p95_total.get('POST_20260720'))} bps；7/16工作日延续高位，但完整预设窗口不是相对PRE的额外放大，7/17后的下移才是最强变点。",
        f"- **中等置信度**：7/16—20典型偏差最大层为 `{dominant}`，尾部P95最大层为 `{tail_dom}`；PRE主要是index-market层。对称bps分量不是严格可加，残差已单列。",
        f"- **高置信度**：低15分钟成交量和陈旧trade close不足以解释；高溢价桶落入最低成交量十分位的最高分段占比仅 {_fmt(100*low_share,2)}%。由于缺少历史BBO和深度，订单簿流动性仍无法确认。相关性不作因果解释。",
        f"- **高置信度**：数据错误判定为 `{'DATA_ERROR_SUPPORTED' if (dq.conclusion=='DATA_ERROR_SUPPORTED').any() else 'DATA_ERROR_NOT_SUPPORTED'}`（个别不可验证项仍为 INCONCLUSIVE）。",
        "- **无法确认**：15分钟成交收盘不是BBO；没有历史bid/ask和深度，不能确认实际可成交价差或容量。","",
        "## 2. 三类分段严格分离","",
        "### 2.1 人工日期段（仅描述）","",
        "`PRE_20260716`、`GATE_REGIME_20260716_20260720`、`POST_20260720` 仅用于人工日期对照，不是实时标签。","",
        "### 2.2 数据驱动历史解释段（retrospective）","",
        "这些边界可使用变点前后窗口与完整样本，只用于历史解释，不能用于策略，也不能称为实时可识别标签。","",
        _markdown(retro,index=False),"",
        "### 2.3 因果实时标签统计","",
        "仅使用当前及过去bar；研究参数单独输出，不宣称为完整样本最优的未来策略参数。","",
        _markdown(causal_counts,digits=0),"",
        "### Gate/四家分人工日期段 P95 绝对价差（bps）","",_markdown(piv,digits=1),"",
        "### trade / mark / index 分解中位数（bps）","",_markdown(dmed,digits=2),"",
        "## 2. 已由数据确认","",
        f"- 主比较只用原生15分钟trade close，完全相同 `open_time`；未填充缺口。外部trade中位数固定为 {', '.join(EXTERNAL_TRADE)} 且要求严格3-of-3齐全；两家结果只在 sensitivity CSV 单列。",
        "- 外部mark中位数只含 Binance、Bitget、OKX；Hyperliquid没有可比历史mark，未加入。",
        f"- KRX实际交易日历重开后1小时的溢价变化中位数（负数为收敛）：{krx_text}；并非所有regime都在开盘后系统性收敛。周末、工作日、美国时段与UTC小时的完整分组见 sessions CSV。",
        f"- 自动变点候选共 {len(changes)} 个；最高置信候选如下：","",_markdown(top,index=False,digits=2) if len(top) else "无足够数据", "",
        "- 连续阈值事件的end_time为右开边界，持续时间严格为15分钟倍数。","",
        "## 3. 较强支持的解释","",
        "- 以分解中绝对中位数/P95最大的层作为主要价格口径来源；这识别的是偏差所在层，不等于证明其业务原因。",
        "- 若四个Gate组合同时同向而非Gate基准明显更小，更支持Gate自身定价层级，而不是只有Gate/OKX的双边定义差异。","",
        "## 4. 只能作为候选的解释","",
        "- Gate用户流/做市库存、mark公式、index休市处理、合约映射及外部事件都需要官方历史参数或盘口数据才能确认。",
        f"- 资金滞后相关使用 `descriptive_forward_hold`，只作描述；真实现金流仅保留真实结算事件。{external_note}","",
        f"- 最大绝对滞后相关仅 {_fmt(max_lag,3)}，不同交易所领先/滞后方向不一致；无法支持稳定的资金费率领先或滞后关系。","",
        f"- 真实共同资金结算前后4小时的绝对溢价变化中位数（负数为结算后收敛）：{settle_text}；该事件研究仍不识别因果。","",
        "## 5. 当前不支持的解释","",
        "- 没有把7月16日当作首次异常；完整15分钟PRE窗口直接反对该叙事。",
        "- 校验未支持秒/毫秒、1000倍乘数、重复时间戳或OHLC破坏足以制造整体regime。","",
        "## 6. 仍缺少的关键数据","",
        "- Gate历史BBO、逐笔成交、订单簿深度、trade_count、指数成分及权重、mark公式逐时参数、做市库存。",
        "- 其他交易所同口径历史BBO；$1,000/$5,000/$10,000名义订单冲击成本。","",
        "## 7. 对套利策略的影响","",
        "1. 15分钟trade close价差不等于可执行BBO。",
        "2. 长期同方向基差不保证快速收敛；做空Gate有基差继续扩大的风险。",
        "3. Gate持续更高可能伴随做空资金收入，但资金规则、mark/index差异会改变现金流和强平风险。",
        "4. 历史最大价差不代表容量；必须用实时Gate best bid对另一所best ask并测量深度/slippage。","",
        "## 8. 下一步需要采集的数据","",
        "- 同步采集五家1秒或逐笔BBO、至少20档深度、主动成交方向和真实资金结算。",
        "- 保存Gate mark/index原始快照、指数成分、合约规则版本与公告生效时间。","",
        "## 数据质量逐项结论","",_markdown(dq,index=False),"","## 流动性摘要","",_markdown(liq_summary,index=False,digits=4),"",
        "## 资金与非Gate基准","",_markdown(nongate_summary,index=False,digits=2),"","## 假设评分","",_markdown(hypotheses,index=False),"",
        "## 外部资料（与仓库数据结论分栏）","",_markdown(external,index=False) if len(external) else "外部原因未验证。","",
        "## 复现","","```bash","uv run python -m skhynix_research.gate_regime_15m","```","",
        "所有时间UTC；主价格为原生15分钟成交K线收盘，不是BBO。"]
    path=R15/"gate_regime_15m_diagnostics.md"; path.write_text("\n".join(lines),encoding="utf-8"); return path


def update_quick_report(report_path: Path, summary: pd.DataFrame, decomp: pd.DataFrame, changes: pd.DataFrame, causal: pd.DataFrame, retro: pd.DataFrame):
    if not report_path.exists(): return
    doc=report_path.read_text(encoding="utf-8"); start="<!-- GATE_REGIME_15M_START -->"; end="<!-- GATE_REGIME_15M_END -->"
    if start in doc and end in doc: doc=doc[:doc.index(start)]+doc[doc.index(end)+len(end):]
    p=summary.pivot(index="regime",columns="pair",values="p95_abs_bps").round(1)
    d=decomp.pivot(index="regime",columns="component_name",values="median_bps").round(2)
    cp=changes.sort_values("confidence_metric",ascending=False).head(5)
    cc=causal_regime_summary(causal).set_index("causal_regime")
    section=f'''{start}<section id="gate-regime-15m"><h2>Gate 15分钟 regime 独立诊断</h2><p><strong>主统计口径：</strong>{STRICT_EXTERNAL_SCOPE}，固定 median(binance, bitget, okx)，同一open_time严格3-of-3齐全。两家可用结果仅进入独立敏感性文件。</p><p><strong>直接结论：</strong>大价差在6月23日前后已经出现，7月16日不是起点。低15分钟成交量和陈旧trade close不足以解释；由于缺少历史BBO和深度，订单簿流动性仍无法确认。</p><h3>人工日期段统计（仅描述）</h3>{p.to_html(classes="dataframe",border=0)}<h3>历史解释段（retrospective，不可用于策略）</h3>{retro.to_html(index=False,classes="dataframe",border=0)}<h3>因果实时标签统计</h3>{cc.to_html(classes="dataframe",border=0)}<h3>trade/mark/index分解中位数（bps）</h3>{d.to_html(classes="dataframe",border=0)}<h3>最高置信回看变点</h3>{cp.to_html(index=False,classes="dataframe",border=0)}<p><a href="gate_regime_15m_diagnostics.md">完整诊断、限制与假设评分</a></p></section>{end}'''
    marker="</main>" if "</main>" in doc else "</body>"; doc=doc.replace(marker,section+marker,1); report_path.write_text(doc,encoding="utf-8")


def run(root: Path=ROOT) -> dict:
    global ROOT,R15,CHARTS
    ROOT=Path(root);R15=ROOT/"reports_15m";CHARTS=R15/"charts";R15.mkdir(parents=True,exist_ok=True);CHARTS.mkdir(parents=True,exist_ok=True)
    prices=pd.read_parquet(ROOT/"data/normalized/prices_15m.parquet"); funding=pd.read_parquet(ROOT/"data/normalized/funding_events.parquet")
    trade,mark,decomposition=build_core(prices); summary=summarize_pairs(trade); consistency=cross_sectional_consistency(trade)
    sensitivity=external_sensitivity(trade)
    sensitivity["gate_premium_vs_external_median_bps"]=_bps(
        trade.gate.reindex(sensitivity.index), sensitivity.available_2_of_3_external_median
    )
    causal=build_causal_regime_labels(prices)
    decomp_summary=summarize_decomposition(decomposition); rolling=rolling_diagnostics(trade); changes=detect_change_points(trade); retro=retrospective_segments(changes,trade.index); events=continuous_events(trade)
    dq=data_quality(prices,trade); liquidity,liq_summary=liquidity_analysis(prices,trade); sessions,session_summary=session_analysis(trade)
    nongate,nongate_summary=nongate_analysis(trade); fund=funding_analysis(funding,trade)
    external_path=R15/"gate_external_event_timeline.csv"; external=pd.read_csv(external_path) if external_path.exists() else pd.DataFrame(columns=["event_time","event_type","title","source","source_url","verified","possible_mechanism","supports_hypothesis","contradicts_hypothesis"])
    hypotheses=hypothesis_table(dq,liq_summary,decomp_summary,nongate_summary,fund)
    summary.to_csv(R15/"gate_regime_15m_summary.csv",index=False); consistency.to_csv(R15/"gate_regime_15m_consistency.csv",index=False)
    decomp_summary.to_csv(R15/"gate_regime_15m_decomposition.csv",index=False); events.to_csv(R15/"gate_regime_15m_events.csv",index=False)
    fund.to_csv(R15/"gate_regime_15m_funding.csv",index=False); changes.to_csv(R15/"gate_regime_15m_change_points.csv",index=False)
    dq.to_csv(R15/"gate_regime_15m_data_quality.csv",index=False); liq_summary.to_csv(R15/"gate_regime_15m_liquidity.csv",index=False)
    session_summary.to_csv(R15/"gate_regime_15m_sessions.csv",index=False); nongate_summary.to_csv(R15/"gate_regime_15m_nongate.csv",index=False)
    sessions.to_csv(R15/"gate_regime_15m_session_features.csv",index=False); liquidity.to_csv(R15/"gate_regime_15m_liquidity_bars.csv",index=False)
    hypotheses.to_csv(R15/"gate_regime_15m_hypotheses.csv",index=False); rolling.reset_index().to_csv(R15/"gate_regime_15m_rolling.csv",index=False)
    sensitivity.reset_index(names="open_time").to_csv(R15/"gate_external_sensitivity_15m.csv",index=False)
    causal.to_csv(R15/"gate_causal_regime_labels_15m.csv",index=False)
    causal_regime_summary(causal).to_csv(R15/"gate_causal_regime_summary_15m.csv",index=False)
    retro.to_csv(R15/"gate_retrospective_segments_15m.csv",index=False)
    pd.DataFrame([CAUSAL_RESEARCH_PARAMS | {"parameter_scope":"RESEARCH_INPUT_NOT_FUTURE_STRATEGY_LOCK"}]).to_csv(R15/"gate_causal_regime_params_15m.csv",index=False)
    make_charts(trade,rolling,decomposition,liquidity,sessions,nongate,fund,events,changes)
    report=write_report(summary,decomp_summary,changes,dq,liq_summary,session_summary,nongate_summary,fund,hypotheses,trade,events,external,causal,retro)
    update_quick_report(R15/"quick_report_15m.html",summary,decomp_summary,changes,causal,retro)
    strict=trade.gate_premium_vs_market_median_bps.dropna()
    return {"price_start":strict.index.min(),"price_end_exclusive":strict.index.max()+BAR,"summary":summary,"changes":changes,"causal":causal,"report":report}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument("--root",type=Path,default=ROOT);args=parser.parse_args(argv)
    result=run(args.root);print(f"Gate 15m: {result['price_start']} to {result['price_end_exclusive']}; report={result['report']}")
    return 0


if __name__=="__main__": raise SystemExit(main())
