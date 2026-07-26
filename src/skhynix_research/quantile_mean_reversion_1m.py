"""Causal native-1m cross-exchange quantile mean-reversion study.

Signals use completed trade-OHLC closes and executions use the next contiguous
minute open.  This is a historical OHLC proxy study, not an executable backtest.
Missing observations are never filled or synthesized.
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
import html
import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports_1m_quantile_mean_reversion"
CHART_DIR = REPORT_DIR / "charts"
EXCHANGES = ("binance", "bitget", "gate", "okx")
PAIRS = tuple(combinations(EXCHANGES, 2))
SCOPES = ("PAIR_NATIVE_WINDOW", "COMMON_FOUR_WINDOW")
HISTORY_MODELS = {
    "EXPANDING_PAST": None,
    "ROLLING_24H": 1440,
    "ROLLING_72H": 4320,
    "ROLLING_7D": 10080,
}
SIDE_POLICIES = ("UPPER_P75_ONLY", "TWO_SIDED_P75_P25")
ENTRY_POLICIES = ("RECHECK_AT_NEXT_OPEN", "LOCKED_SIGNAL_ENTRY")
EXIT_POLICIES = ("FROZEN_ENTRY_MEAN", "DYNAMIC_CAUSAL_MEAN", "FROZEN_ENTRY_MEDIAN", "DYNAMIC_CAUSAL_MEDIAN")
MAX_HOLDS = (None, 1440, 4320, 10080)
REALIZED_STATUSES = ("REALIZED", "MAX_HOLD")

EVENT_COLUMNS = [
    "pair", "data_scope", "history_model", "history_window_minutes",
    "strategy_side_policy", "entry_execution_policy", "exit_center_policy",
    "max_holding_minutes", "signal_time", "signal_close_spread_bps",
    "historical_p25_at_signal", "historical_p75_at_signal",
    "historical_mean_at_signal", "historical_median_at_signal", "historical_iqr_at_signal",
    "observation_count_at_signal", "tail_excess_bps", "tail_score",
    "entry_exec_time", "entry_open_spread_bps", "frozen_entry_mean_bps",
    "frozen_entry_median_bps", "entry_direction", "long_exchange", "short_exchange",
    "entry_long_price", "entry_short_price", "exit_target_at_entry_bps",
    "exit_signal_time", "exit_signal_spread_bps", "exit_target_at_signal_bps",
    "exit_exec_time", "exit_open_spread_bps", "exit_long_price", "exit_short_price",
    "holding_minutes", "leg_sum_price_pnl_bps", "gross_account_price_pnl_bps",
    "assumed_cost_bps", "net_account_price_pnl_bps", "long_leg_funding_bps",
    "short_leg_funding_bps", "net_funding_account_bps", "net_combined_account_pnl_bps",
    "mae_account_price_pnl_bps", "mfe_account_price_pnl_bps",
    "max_abs_spread_during_hold_bps", "close_reason", "status",
]
TIME_COLUMNS = ["signal_time", "entry_exec_time", "exit_signal_time", "exit_exec_time"]
TEXT_EVENT_COLUMNS = {"pair", "data_scope", "history_model", "strategy_side_policy",
                      "entry_execution_policy", "exit_center_policy", "entry_direction",
                      "long_exchange", "short_exchange", "close_reason", "status"}
SUMMARY_KEYS = ["pair", "history_model", "strategy_side_policy", "entry_execution_policy",
                "exit_center_policy", "max_holding_minutes", "data_scope"]


def spread_bps(a, b):
    """Symmetric directed A/B spread in basis points."""
    return 20_000.0 * (a - b) / (a + b)


def _utc(values):
    return pd.to_datetime(values, utc=True, errors="coerce")


def _valid_ohlc(frame: pd.DataFrame, run_time: pd.Timestamp) -> pd.Series:
    cols = ["open", "high", "low", "close"]
    values = frame[cols].apply(pd.to_numeric, errors="coerce")
    finite = np.isfinite(values).all(axis=1) & values.gt(0).all(axis=1)
    ordered = (values.high >= values[["open", "close"]].max(axis=1)) & (values.low <= values[["open", "close"]].min(axis=1)) & (values.high >= values.low)
    opened = _utc(frame.open_time)
    closed = _utc(frame.close_time)
    native = opened.dt.second.eq(0) & opened.dt.microsecond.eq(0)
    complete = (opened < run_time.floor("min")) & (closed >= opened + pd.Timedelta(seconds=59))
    if "native_interval" in frame:
        native &= frame.native_interval.fillna("1m").eq("1m")
    if "interval_minutes" in frame:
        native &= pd.to_numeric(frame.interval_minutes, errors="coerce").fillna(1).eq(1)
    return finite & ordered & native & complete


def _read_parquet_tree(path: Path) -> list[pd.DataFrame]:
    if not path.exists():
        return []
    return [pd.read_parquet(file) for file in sorted(path.rglob("*.parquet"))]


def load_prices(root: Path = ROOT, run_time: pd.Timestamp | None = None) -> pd.DataFrame:
    """Merge normalized and live bars, newest valid retrieval wins per key."""
    run_time = pd.Timestamp.now(tz="UTC") if run_time is None else pd.Timestamp(run_time)
    if run_time.tzinfo is None:
        run_time = run_time.tz_localize("UTC")
    frames = []
    historical = root / "data/normalized/prices_1m.parquet"
    if historical.exists():
        frames.append(pd.read_parquet(historical))
    frames.extend(_read_parquet_tree(root / "data/live_1m/prices"))
    if not frames:
        raise FileNotFoundError("no native 1m price data")
    data = pd.concat(frames, ignore_index=True, sort=False)
    data = data[data.exchange.isin(EXCHANGES) & data.price_type.eq("trade")].copy()
    data["open_time"] = _utc(data.open_time)
    data["close_time"] = _utc(data.close_time)
    data["_retrieved"] = _utc(data.get("retrieved_at", pd.Series(pd.NaT, index=data.index)))
    data = data[_valid_ohlc(data, run_time)].sort_values(["exchange", "open_time", "_retrieved"])
    data = data.drop_duplicates(["exchange", "open_time"], keep="last").drop(columns="_retrieved")
    absent = set(EXCHANGES) - set(data.exchange.unique())
    if absent:
        raise ValueError(f"no valid native trade bars for: {sorted(absent)}")
    return data.sort_values(["exchange", "open_time"]).reset_index(drop=True)


def load_funding(root: Path = ROOT) -> pd.DataFrame:
    frames = []
    historical = root / "data/normalized/funding_events.parquet"
    if historical.exists():
        frames.append(pd.read_parquet(historical))
    frames.extend(_read_parquet_tree(root / "data/live_1m/funding"))
    if not frames:
        return pd.DataFrame(columns=["exchange", "funding_time", "funding_rate"])
    data = pd.concat(frames, ignore_index=True, sort=False)
    data = data[data.exchange.isin(EXCHANGES)].copy()
    data["funding_time"] = _utc(data.funding_time)
    data["funding_rate"] = pd.to_numeric(data.funding_rate, errors="coerce")
    data["_retrieved"] = _utc(data.get("retrieved_at", pd.Series(pd.NaT, index=data.index)))
    data = data.dropna(subset=["funding_time", "funding_rate"]).sort_values(["exchange", "funding_time", "_retrieved"])
    return data.drop_duplicates(["exchange", "funding_time"], keep="last").drop(columns="_retrieved")


@dataclass(frozen=True)
class PreparedData:
    trade: pd.DataFrame
    wide: pd.DataFrame
    common_four: pd.DatetimeIndex
    gate_start: pd.Timestamp
    run_time: pd.Timestamp
    data_coverage: pd.DataFrame
    pair_coverage: pd.DataFrame


def _longest_run(index: pd.DatetimeIndex) -> int:
    if not len(index):
        return 0
    groups = pd.Series(index, index=index).diff().ne(pd.Timedelta(minutes=1)).cumsum()
    return int(groups.value_counts().max())


def prepare_data(trade: pd.DataFrame, run_time: pd.Timestamp | None = None) -> PreparedData:
    run_time = pd.Timestamp.now(tz="UTC") if run_time is None else pd.Timestamp(run_time)
    if run_time.tzinfo is None:
        run_time = run_time.tz_localize("UTC")
    p = trade.copy()
    p["open_time"] = _utc(p.open_time)
    gate_start = p.loc[p.exchange.eq("gate"), "open_time"].min()
    p = p[p.open_time >= gate_start].copy()
    wide = p.pivot(index="open_time", columns="exchange", values=["open", "high", "low", "close"]).sort_index()
    valid = {ex: pd.DatetimeIndex(p.loc[p.exchange.eq(ex), "open_time"].unique()).sort_values() for ex in EXCHANGES}
    common = valid[EXCHANGES[0]]
    for ex in EXCHANGES[1:]:
        common = common.intersection(valid[ex])
    coverage = []
    for ex in EXCHANGES:
        idx = valid[ex]
        first, last = idx.min(), idx.max()
        expected = int((last - first) / pd.Timedelta(minutes=1)) + 1
        coverage.append({"exchange": ex, "first_valid_minute": first, "last_valid_minute": last,
                         "valid_minutes": len(idx), "missing_minutes": expected - len(idx),
                         "latest_data_lag_minutes": (run_time - (last + pd.Timedelta(minutes=1))).total_seconds() / 60})
    pair_rows = []
    for a, b in PAIRS:
        for scope in SCOPES:
            idx = common if scope == "COMMON_FOUR_WINDOW" else valid[a].intersection(valid[b])
            idx = idx[idx >= gate_start]
            first, last = idx.min(), idx.max()
            expected = int((last - first) / pd.Timedelta(minutes=1)) + 1
            pair_rows.append({"pair": f"{a}/{b}", "data_scope": scope, "start_time": first,
                              "end_time_exclusive": last + pd.Timedelta(minutes=1), "valid_minutes": len(idx),
                              "missing_minutes": expected - len(idx), "longest_contiguous_minutes": _longest_run(idx)})
    return PreparedData(p, wide, common, gate_start, run_time, pd.DataFrame(coverage), pd.DataFrame(pair_rows))


def pair_frame(prepared: PreparedData, a: str, b: str, scope: str) -> pd.DataFrame:
    if scope == "COMMON_FOUR_WINDOW":
        idx = prepared.common_four
    else:
        idx = prepared.wide.dropna(subset=[(f, ex) for f in ("open", "high", "low", "close") for ex in (a, b)]).index
    idx = idx[idx >= prepared.gate_start]
    w = prepared.wide.reindex(idx)
    out = pd.DataFrame(index=idx)
    for field in ("open", "high", "low", "close"):
        out[f"{field}_a"] = w[(field, a)].to_numpy(float)
        out[f"{field}_b"] = w[(field, b)].to_numpy(float)
        out[f"{field}_spread_bps"] = spread_bps(out[f"{field}_a"], out[f"{field}_b"])
    return out


def causal_history(frame: pd.DataFrame, model: str) -> pd.DataFrame:
    """Exact causal statistics; rolling histories restart after every gap."""
    if model not in HISTORY_MODELS:
        raise ValueError(model)
    values = frame.close_spread_bps.astype(float)
    window = HISTORY_MODELS[model]
    if window is None:
        past = values.shift(1)
        roll = past.expanding(min_periods=1440)
        out = pd.DataFrame(index=values.index)
        out["historical_mean"] = roll.mean()
        out["historical_median"] = roll.median()
        out["historical_p25"] = roll.quantile(.25)
        out["historical_p75"] = roll.quantile(.75)
        out["observation_count"] = past.expanding().count()
        ready = out[["historical_mean", "historical_median", "historical_p25", "historical_p75"]].notna().all(axis=1)
        invalid = ready & ((out.historical_p75 < out.historical_p25) | ~np.isfinite(out.historical_mean))
        out["history_status"] = np.where(ready, "READY", "INSUFFICIENT_HISTORY")
        out.loc[invalid, "history_status"] = "INVALID_HISTORY"
        out["historical_iqr"] = out.historical_p75 - out.historical_p25
        return out
    gap = values.index.to_series().diff().ne(pd.Timedelta(minutes=1))
    segment = gap.cumsum()
    parts = []
    for seg_no, group in values.groupby(segment):
        past = group.shift(1)
        roll = past.rolling(window, min_periods=window)
        q = pd.DataFrame(index=group.index)
        q["historical_mean"] = roll.mean()
        q["historical_median"] = roll.median()
        q["historical_p25"] = roll.quantile(.25)
        q["historical_p75"] = roll.quantile(.75)
        q["observation_count"] = past.rolling(window).count()
        ready = q[["historical_mean", "historical_median", "historical_p25", "historical_p75"]].notna().all(axis=1)
        invalid = ready & ((q.historical_p75 < q.historical_p25) | ~np.isfinite(q.historical_mean))
        q["history_status"] = np.where(ready, "READY", "INSUFFICIENT_HISTORY")
        if seg_no > 1:
            q.loc[~ready, "history_status"] = "RESET_AFTER_GAP"
        q.loc[invalid, "history_status"] = "INVALID_HISTORY"
        q["historical_iqr"] = q.historical_p75 - q.historical_p25
        parts.append(q)
    return pd.concat(parts).reindex(frame.index) if parts else pd.DataFrame(index=frame.index)


def funding_attribution(funding: pd.DataFrame | None, long_ex: str, short_ex: str,
                        entry: pd.Timestamp, exit_: pd.Timestamp) -> tuple[float, float, float]:
    """Account-bps attribution for settlements strictly inside the holding interval."""
    if funding is None or funding.empty:
        return 0.0, 0.0, 0.0
    q = funding[(funding.funding_time > entry) & (funding.funding_time < exit_)]
    long_bps = float(-q.loc[q.exchange.eq(long_ex), "funding_rate"].sum() * 10_000)
    short_bps = float(q.loc[q.exchange.eq(short_ex), "funding_rate"].sum() * 10_000)
    return long_bps, short_bps, .5 * (long_bps + short_bps)


def account_pnl(long0: float, short0: float, long1: float, short1: float) -> tuple[float, float]:
    leg_sum = ((long1 / long0 - 1) + (1 - short1 / short0)) * 10_000
    return float(leg_sum), float(.5 * leg_sum)


def _blank_event(a: str, b: str, scope: str, model: str, side_policy: str,
                 entry_policy: str, exit_policy: str, max_hold: int | None,
                 signal_time: pd.Timestamp, signal_spread: float, stats: pd.Series,
                 tail: str) -> dict:
    row = {column: np.nan for column in EVENT_COLUMNS}
    excess = signal_spread - stats.historical_p75 if tail == "UPPER" else stats.historical_p25 - signal_spread
    row.update(pair=f"{a}/{b}", data_scope=scope, history_model=model,
               history_window_minutes=HISTORY_MODELS[model], strategy_side_policy=side_policy,
               entry_execution_policy=entry_policy, exit_center_policy=exit_policy,
               max_holding_minutes=max_hold, signal_time=signal_time,
               signal_close_spread_bps=signal_spread,
               historical_p25_at_signal=stats.historical_p25,
               historical_p75_at_signal=stats.historical_p75,
               historical_mean_at_signal=stats.historical_mean,
               historical_median_at_signal=stats.historical_median,
               historical_iqr_at_signal=stats.historical_iqr,
               observation_count_at_signal=stats.observation_count,
               tail_excess_bps=excess,
               tail_score=excess / max(float(stats.historical_iqr), 1e-9),
               frozen_entry_mean_bps=stats.historical_mean,
               frozen_entry_median_bps=stats.historical_median,
               exit_target_at_entry_bps=stats.historical_median if "MEDIAN" in exit_policy else stats.historical_mean,
               assumed_cost_bps=20.0)
    return row


def _set_direction(row: dict, a: str, b: str, a_price: float, b_price: float, tail: str) -> None:
    """Direction follows relative A/B deviation, including negative thresholds."""
    if tail == "UPPER":
        row.update(entry_direction="SHORT_A_LONG_B", long_exchange=b, short_exchange=a,
                   entry_long_price=b_price, entry_short_price=a_price)
    else:
        row.update(entry_direction="LONG_A_SHORT_B", long_exchange=a, short_exchange=b,
                   entry_long_price=a_price, entry_short_price=b_price)


def _mark_pnl(row: dict, a: str, a_price: float, b_price: float) -> float:
    if row["long_exchange"] == a:
        return account_pnl(row["entry_long_price"], row["entry_short_price"], a_price, b_price)[1]
    return account_pnl(row["entry_long_price"], row["entry_short_price"], b_price, a_price)[1]


def _finish_pnl(row: dict, a: str, a_price: float, b_price: float,
                funding: pd.DataFrame | None, exit_time: pd.Timestamp) -> None:
    if row["long_exchange"] == a:
        long1, short1 = a_price, b_price
    else:
        long1, short1 = b_price, a_price
    leg_sum, gross = account_pnl(row["entry_long_price"], row["entry_short_price"], long1, short1)
    lf, sf, net_funding = funding_attribution(funding, row["long_exchange"], row["short_exchange"], row["entry_exec_time"], exit_time)
    row.update(exit_long_price=long1, exit_short_price=short1,
               leg_sum_price_pnl_bps=leg_sum, gross_account_price_pnl_bps=gross,
               net_account_price_pnl_bps=gross - 20.0,
               long_leg_funding_bps=lf, short_leg_funding_bps=sf,
               net_funding_account_bps=net_funding,
               net_combined_account_pnl_bps=gross - 20.0 + net_funding)


def simulate_pair(frame: pd.DataFrame, history: pd.DataFrame, a: str, b: str,
                  scope: str, model: str, side_policy: str = "UPPER_P75_ONLY",
                  entry_policy: str = "RECHECK_AT_NEXT_OPEN",
                  exit_policy: str = "FROZEN_ENTRY_MEAN", max_hold: int | None = None,
                  funding: pd.DataFrame | None = None) -> pd.DataFrame:
    """Deterministic one-position-per-pair event state machine."""
    if frame.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    times = frame.index
    s = frame.close_spread_bps.to_numpy(float)
    os = frame.open_spread_bps.to_numpy(float)
    oa, ob = frame.open_a.to_numpy(float), frame.open_b.to_numpy(float)
    ca, cb = frame.close_a.to_numpy(float), frame.close_b.to_numpy(float)
    p25, p75 = history.historical_p25.to_numpy(float), history.historical_p75.to_numpy(float)
    rows: list[dict] = []
    upper_armed = lower_armed = True
    i = 1
    while i < len(frame):
        contiguous_prev = times[i] - times[i - 1] == pd.Timedelta(minutes=1)
        if history.iloc[i].history_status != "READY":
            i += 1
            continue
        inside = p25[i] < s[i] < p75[i]
        if inside:
            upper_armed = lower_armed = True
        upper = upper_armed and contiguous_prev and history.iloc[i - 1].history_status == "READY" and s[i - 1] < p75[i - 1] and s[i] >= p75[i]
        lower = (side_policy == "TWO_SIDED_P75_P25" and lower_armed and contiguous_prev and
                 history.iloc[i - 1].history_status == "READY" and s[i - 1] > p25[i - 1] and s[i] <= p25[i])
        if not upper and not lower:
            i += 1
            continue
        tail = "UPPER" if upper else "LOWER"
        upper_armed = False if upper else upper_armed
        lower_armed = False if lower else lower_armed
        row = _blank_event(a, b, scope, model, side_policy, entry_policy, exit_policy,
                           max_hold, times[i], s[i], history.iloc[i], tail)
        if i + 1 >= len(frame) or times[i + 1] - times[i] != pd.Timedelta(minutes=1):
            row.update(close_reason="NO_NEXT_BAR_FOR_ENTRY", status="NO_NEXT_BAR_FOR_ENTRY")
            rows.append(row); i += 1; continue
        entry_i = i + 1
        if not np.isfinite([oa[entry_i], ob[entry_i], os[entry_i]]).all() or min(oa[entry_i], ob[entry_i]) <= 0:
            row.update(entry_exec_time=times[entry_i], close_reason="INVALID_PRICE", status="INVALID_PRICE")
            rows.append(row); i = entry_i + 1; continue
        valid_recheck = os[entry_i] >= p75[i] if upper else os[entry_i] <= p25[i]
        if entry_policy == "RECHECK_AT_NEXT_OPEN" and not valid_recheck:
            row.update(entry_exec_time=times[entry_i], entry_open_spread_bps=os[entry_i],
                       close_reason="ENTRY_SIGNAL_DECAYED", status="ENTRY_SIGNAL_DECAYED")
            rows.append(row); i = entry_i + 1; continue
        row.update(entry_exec_time=times[entry_i], entry_open_spread_bps=os[entry_i])
        _set_direction(row, a, b, oa[entry_i], ob[entry_i], tail)
        marks: list[float] = []
        max_abs = abs(os[entry_i])
        j = entry_i
        finished = False
        while j < len(frame):
            mark = _mark_pnl(row, a, ca[j], cb[j])
            marks.append(mark); max_abs = max(max_abs, abs(s[j]))
            target = (history.iloc[j].historical_median if "MEDIAN" in exit_policy else history.iloc[j].historical_mean) if exit_policy.startswith("DYNAMIC") else row["exit_target_at_entry_bps"]
            hit = s[j] <= target if upper else s[j] >= target
            reached_max = max_hold is not None and (times[j] + pd.Timedelta(minutes=1) - times[entry_i]) >= pd.Timedelta(minutes=max_hold)
            if hit or reached_max:
                row.update(exit_signal_time=times[j], exit_signal_spread_bps=s[j], exit_target_at_signal_bps=target)
                if j + 1 >= len(frame) or times[j + 1] - times[j] != pd.Timedelta(minutes=1):
                    row.update(close_reason="NO_NEXT_BAR_FOR_EXIT", status="NO_NEXT_BAR_FOR_EXIT")
                    rows.append(row); i = j + 1; finished = True; break
                if not np.isfinite([oa[j + 1], ob[j + 1], os[j + 1]]).all() or min(oa[j + 1], ob[j + 1]) <= 0:
                    row.update(exit_exec_time=times[j + 1], close_reason="INVALID_PRICE", status="INVALID_PRICE")
                    rows.append(row); i = j + 2; finished = True; break
                row.update(exit_exec_time=times[j + 1], exit_open_spread_bps=os[j + 1],
                           holding_minutes=(times[j + 1] - times[entry_i]).total_seconds() / 60,
                           mae_account_price_pnl_bps=min(marks), mfe_account_price_pnl_bps=max(marks),
                           max_abs_spread_during_hold_bps=max_abs,
                           close_reason="MAX_HOLD" if reached_max and not hit else "CENTER_REVERSION",
                           status="MAX_HOLD" if reached_max and not hit else "REALIZED")
                _finish_pnl(row, a, oa[j + 1], ob[j + 1], funding, times[j + 1])
                rows.append(row); i = j + 2; finished = True; break
            if j + 1 < len(frame) and times[j + 1] - times[j] != pd.Timedelta(minutes=1):
                row.update(holding_minutes=(times[j] + pd.Timedelta(minutes=1) - times[entry_i]).total_seconds() / 60,
                           mae_account_price_pnl_bps=min(marks), mfe_account_price_pnl_bps=max(marks),
                           max_abs_spread_during_hold_bps=max_abs,
                           close_reason="DATA_GAP_DURING_HOLD", status="DATA_GAP_DURING_HOLD")
                rows.append(row); i = j + 1; finished = True; break
            j += 1
        if not finished:
            row.update(holding_minutes=(times[-1] + pd.Timedelta(minutes=1) - times[entry_i]).total_seconds() / 60,
                       mae_account_price_pnl_bps=min(marks) if marks else np.nan,
                       mfe_account_price_pnl_bps=max(marks) if marks else np.nan,
                       max_abs_spread_during_hold_bps=max_abs,
                       close_reason="RIGHT_CENSORED", status="RIGHT_CENSORED")
            rows.append(row); i = len(frame)
    out = pd.DataFrame(rows, columns=EVENT_COLUMNS)
    for col in TIME_COLUMNS:
        out[col] = _utc(out[col])
    return out


def requested_configurations() -> list[tuple[str, str, str, str, int | None]]:
    """Minimal non-duplicated scenario set covering every requested comparison."""
    configs = set()
    for model in HISTORY_MODELS:
        configs.add((model, "UPPER_P75_ONLY", "RECHECK_AT_NEXT_OPEN", "FROZEN_ENTRY_MEAN", None))
    configs.add(("EXPANDING_PAST", "TWO_SIDED_P75_P25", "RECHECK_AT_NEXT_OPEN", "FROZEN_ENTRY_MEAN", None))
    configs.add(("EXPANDING_PAST", "UPPER_P75_ONLY", "LOCKED_SIGNAL_ENTRY", "FROZEN_ENTRY_MEAN", None))
    for exit_policy in EXIT_POLICIES:
        configs.add(("EXPANDING_PAST", "UPPER_P75_ONLY", "RECHECK_AT_NEXT_OPEN", exit_policy, None))
    for hold in MAX_HOLDS:
        configs.add(("EXPANDING_PAST", "UPPER_P75_ONLY", "RECHECK_AT_NEXT_OPEN", "FROZEN_ENTRY_MEAN", hold))
    return sorted(configs, key=lambda x: tuple("" if value is None else str(value) for value in x))


def run_scenarios(prepared: PreparedData, funding: pd.DataFrame | None) -> tuple[pd.DataFrame, dict]:
    frames: dict[tuple[str, str, str], pd.DataFrame] = {}
    histories: dict[tuple[str, str, str, str], pd.DataFrame] = {}
    results = []
    for a, b in PAIRS:
        for scope in SCOPES:
            frame = pair_frame(prepared, a, b, scope)
            frames[a, b, scope] = frame
            for model in HISTORY_MODELS:
                histories[a, b, scope, model] = causal_history(frame, model)
            for model, side, entry, exit_, hold in requested_configurations():
                results.append(simulate_pair(frame, histories[a, b, scope, model], a, b, scope,
                                             model, side, entry, exit_, hold, funding))
    events = pd.concat(results, ignore_index=True) if results else pd.DataFrame(columns=EVENT_COLUMNS)
    for column in EVENT_COLUMNS:
        if column in TIME_COLUMNS:
            events[column] = _utc(events[column])
        elif column not in TEXT_EVENT_COLUMNS:
            events[column] = pd.to_numeric(events[column], errors="coerce")
    return events, {"frames": frames, "histories": histories}


def _bootstrap_ci(closed: pd.DataFrame, samples: int = 1000, seed: int = 20260726) -> tuple[float, float]:
    if closed.empty:
        return np.nan, np.nan
    daily = (closed.assign(day=_utc(closed.entry_exec_time).dt.floor("D"))
             .groupby("day").net_account_price_pnl_bps.mean().to_numpy(float))
    if len(daily) == 1:
        return float(daily[0]), float(daily[0])
    rng = np.random.default_rng(seed)
    means = rng.choice(daily, size=(samples, len(daily)), replace=True).mean(axis=1)
    return float(np.quantile(means, .025)), float(np.quantile(means, .975))


def summarize_events(events: pd.DataFrame) -> pd.DataFrame:
    if events.empty:
        return pd.DataFrame(columns=SUMMARY_KEYS)
    rows = []
    for key, group in events.groupby(SUMMARY_KEYS, dropna=False, sort=True):
        entered = group[group.entry_long_price.notna()]
        closed = group[group.status.isin(REALIZED_STATUSES)]
        pnl = closed.net_account_price_pnl_bps.dropna()
        lo, hi = _bootstrap_ci(closed)
        wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
        daily = closed.assign(day=_utc(closed.exit_exec_time).dt.floor("D")).groupby("day").net_account_price_pnl_bps.sum()
        n = len(group)
        row = dict(zip(SUMMARY_KEYS, key))
        row.update(signal_count=n, entered_count=len(entered), realized_count=int(group.status.eq("REALIZED").sum()),
                   max_hold_count=int(group.status.eq("MAX_HOLD").sum()),
                   right_censored_count=int(group.status.eq("RIGHT_CENSORED").sum()),
                   data_gap_count=int(group.status.eq("DATA_GAP_DURING_HOLD").sum()),
                   no_next_entry_count=int(group.status.eq("NO_NEXT_BAR_FOR_ENTRY").sum()),
                   no_next_exit_count=int(group.status.eq("NO_NEXT_BAR_FOR_EXIT").sum()),
                   entry_decayed_count=int(group.status.eq("ENTRY_SIGNAL_DECAYED").sum()),
                   invalid_price_count=int(group.status.eq("INVALID_PRICE").sum()),
                   realized_rate=float(group.status.eq("REALIZED").sum() / len(entered)) if len(entered) else np.nan,
                   max_hold_rate=float(group.status.eq("MAX_HOLD").sum() / len(entered)) if len(entered) else np.nan,
                   data_gap_rate=float(group.status.eq("DATA_GAP_DURING_HOLD").sum() / len(entered)) if len(entered) else np.nan,
                   unresolved_rate=float(group.status.isin(["RIGHT_CENSORED", "NO_NEXT_BAR_FOR_EXIT", "DATA_GAP_DURING_HOLD"]).sum() / len(entered)) if len(entered) else np.nan,
                   entry_decay_rate=float(group.status.eq("ENTRY_SIGNAL_DECAYED").sum() / n) if n else np.nan,
                   positive_event_count=int(pnl.gt(0).sum()), win_rate=float(pnl.gt(0).mean()) if len(pnl) else np.nan,
                   sum_net_account_price_pnl_bps=pnl.sum(min_count=1), mean_net_account_price_pnl_bps=pnl.mean(),
                   median_net_account_price_pnl_bps=pnl.median(), p05_net_account_price_pnl_bps=pnl.quantile(.05),
                   p25_net_account_price_pnl_bps=pnl.quantile(.25), p75_net_account_price_pnl_bps=pnl.quantile(.75),
                   p95_net_account_price_pnl_bps=pnl.quantile(.95),
                   mean_gross_account_price_pnl_bps=closed.gross_account_price_pnl_bps.mean(),
                   mean_net_funding_account_bps=closed.net_funding_account_bps.mean(),
                   mean_net_combined_account_pnl_bps=closed.net_combined_account_pnl_bps.mean(),
                   median_holding_minutes=closed.holding_minutes.median(), p90_holding_minutes=closed.holding_minutes.quantile(.9),
                   max_holding_minutes_observed=closed.holding_minutes.max(),
                   mean_mae_account_price_pnl_bps=closed.mae_account_price_pnl_bps.mean(),
                   median_mae_account_price_pnl_bps=closed.mae_account_price_pnl_bps.median(),
                   mean_mfe_account_price_pnl_bps=closed.mfe_account_price_pnl_bps.mean(),
                   median_mfe_account_price_pnl_bps=closed.mfe_account_price_pnl_bps.median(),
                   profit_factor=float(wins / losses) if losses > 0 else (math.inf if wins > 0 else np.nan),
                   day_block_bootstrap_ci_low=lo, day_block_bootstrap_ci_high=hi,
                   profitable_days=int((daily > 0).sum()), total_active_days=len(daily))
        flags = []
        if len(closed) < 10: flags.append("REALIZED_LT_10")
        elif len(closed) < 30: flags.append("REALIZED_LT_30")
        if lo <= 0 <= hi: flags.append("CI_CROSSES_ZERO")
        if pnl.mean() > 0 and pnl.median() < 0: flags.append("POSITIVE_MEAN_NEGATIVE_MEDIAN")
        if len(pnl) and pnl.gt(0).mean() < .4 and pnl.mean() > 0: flags.append("LOW_WIN_RATE_LONG_TAIL")
        if row["unresolved_rate"] > .2: flags.append("HIGH_UNRESOLVED_RATE")
        row["quality_flags"] = "|".join(flags)
        rows.append(row)
    result = pd.DataFrame(rows)
    grid_rows = []
    for a, b in PAIRS:
        for scope in SCOPES:
            for model, side, entry, exit_, hold in requested_configurations():
                grid_rows.append({"pair": f"{a}/{b}", "history_model": model,
                                  "strategy_side_policy": side, "entry_execution_policy": entry,
                                  "exit_center_policy": exit_, "max_holding_minutes": hold,
                                  "data_scope": scope})
    grid = pd.DataFrame(grid_rows)
    grid["_hold"] = grid.max_holding_minutes.fillna(-1)
    result["_hold"] = result.max_holding_minutes.fillna(-1)
    result = result.drop(columns="max_holding_minutes")
    out = grid.merge(result, on=[c for c in SUMMARY_KEYS if c != "max_holding_minutes"] + ["_hold"], how="left")
    out = out.drop(columns="_hold")
    count_columns = [c for c in out.columns if c.endswith("_count") or c in {"signal_count", "entered_count", "realized_count", "profitable_days", "total_active_days"}]
    for column in count_columns:
        out[column] = out[column].fillna(0).astype(int)
    out["quality_flags"] = out.quality_flags.fillna("ZERO_EVENTS")
    return out


def primary_events(events: pd.DataFrame) -> pd.DataFrame:
    return events[(events.data_scope == "PAIR_NATIVE_WINDOW") &
                  (events.history_model == "EXPANDING_PAST") &
                  (events.strategy_side_policy == "UPPER_P75_ONLY") &
                  (events.entry_execution_policy == "RECHECK_AT_NEXT_OPEN") &
                  (events.exit_center_policy == "FROZEN_ENTRY_MEAN") &
                  events.max_holding_minutes.isna()].copy()


def simulate_global(events: pd.DataFrame, window_start: pd.Timestamp, window_end: pd.Timestamp) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    """Chronological one-capital selection using only entry-time tail score."""
    candidates = primary_events(events)
    candidates = candidates[candidates.entry_long_price.notna()].sort_values(["entry_exec_time", "tail_score", "pair"], ascending=[True, False, True])
    selected, rejected = [], 0
    free_at = window_start
    for when, same_time in candidates.groupby("entry_exec_time", sort=True):
        if when < free_at:
            rejected += len(same_time); continue
        row = same_time.sort_values(["tail_score", "pair"], ascending=[False, True]).iloc[0].copy()
        rejected += len(same_time) - 1
        selected.append(row)
        if pd.isna(row.exit_exec_time):
            free_at = window_end; break
        free_at = row.exit_exec_time
    chosen = pd.DataFrame(selected, columns=EVENT_COLUMNS)
    curve_rows = [{"time": window_start, "event_number": 0, "pair": "START", "event_net_return": 0.0,
                   "compounded_equity_usd": 1000.0, "non_compounded_equity_usd": 1000.0}]
    comp = simple = 1000.0
    for number, (_, row) in enumerate(chosen[chosen.status.isin(REALIZED_STATUSES)].sort_values("exit_exec_time").iterrows(), 1):
        ret = float(row.net_account_price_pnl_bps) / 10_000
        comp *= 1 + ret; simple += 1000.0 * ret
        curve_rows.append({"time": row.exit_exec_time, "event_number": number, "pair": row.pair,
                           "event_net_return": ret, "compounded_equity_usd": comp,
                           "non_compounded_equity_usd": simple})
    curve = pd.DataFrame(curve_rows)
    peak = curve.compounded_equity_usd.cummax()
    max_dd = float((curve.compounded_equity_usd / peak - 1).min())
    held = chosen.dropna(subset=["entry_exec_time", "exit_exec_time"])
    held_minutes = (held.exit_exec_time - held.entry_exec_time).dt.total_seconds().sum() / 60 if len(held) else 0
    total_minutes = max(1, (window_end - window_start).total_seconds() / 60)
    metrics = {"selected_events": len(chosen), "rejected_while_occupied": rejected,
               "compounded_final_equity_usd": comp, "non_compounded_final_equity_usd": simple,
               "max_drawdown": max_dd, "capital_utilization": held_minutes / total_minutes}
    return chosen, curve, metrics


def _aggregate(events: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    rows = []
    for key, group in events.groupby(keys, dropna=False, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        closed = group[group.status.isin(REALIZED_STATUSES)]
        pnl = closed.net_account_price_pnl_bps.dropna()
        row = dict(zip(keys, key))
        entered_count = int(group.entry_long_price.notna().sum())
        unresolved_count = int(group.status.isin(["RIGHT_CENSORED", "DATA_GAP_DURING_HOLD", "NO_NEXT_BAR_FOR_EXIT"]).sum())
        row.update(signal_count=len(group), entered_count=entered_count,
                   realized_count=int(group.status.eq("REALIZED").sum()), max_hold_count=int(group.status.eq("MAX_HOLD").sum()),
                   unresolved_count=unresolved_count,
                   realized_rate=float(group.status.eq("REALIZED").sum() / entered_count) if entered_count else np.nan,
                   unresolved_rate=float(unresolved_count / entered_count) if entered_count else np.nan,
                   entry_decay_rate=float(group.status.eq("ENTRY_SIGNAL_DECAYED").sum() / len(group)) if len(group) else np.nan,
                   mean_net_account_price_pnl_bps=pnl.mean(), median_net_account_price_pnl_bps=pnl.median(),
                   win_rate=float(pnl.gt(0).mean()) if len(pnl) else np.nan,
                   median_holding_minutes=closed.holding_minutes.median(),
                   mean_net_funding_account_bps=closed.net_funding_account_bps.mean(),
                   mean_net_combined_account_pnl_bps=closed.net_combined_account_pnl_bps.mean())
        rows.append(row)
    return pd.DataFrame(rows)


def comparison_tables(events: pd.DataFrame, summary: pd.DataFrame) -> dict[str, pd.DataFrame]:
    natural = events[events.max_holding_minutes.isna()]
    primary = primary_events(events)
    primary["gate_group"] = np.where(primary.pair.str.contains("gate"), "GATE_RELATED", "NON_GATE")
    def complete(table, dimensions):
        keys = list(dimensions)
        grid = pd.MultiIndex.from_product([dimensions[k] for k in keys], names=keys).to_frame(index=False)
        out = grid.merge(table, on=keys, how="left")
        for column in ["signal_count", "entered_count", "realized_count", "max_hold_count", "unresolved_count"]:
            out[column] = out[column].fillna(0).astype(int)
        return out
    pairs = [f"{a}/{b}" for a, b in PAIRS]
    history = _aggregate(natural[(natural.strategy_side_policy == "UPPER_P75_ONLY") & (natural.entry_execution_policy == "RECHECK_AT_NEXT_OPEN") & (natural.exit_center_policy == "FROZEN_ENTRY_MEAN")], ["history_model", "data_scope"])
    side = _aggregate(natural[(natural.history_model == "EXPANDING_PAST") & (natural.entry_execution_policy == "RECHECK_AT_NEXT_OPEN") & (natural.exit_center_policy == "FROZEN_ENTRY_MEAN")], ["strategy_side_policy", "data_scope"])
    center = _aggregate(natural[(natural.history_model == "EXPANDING_PAST") & (natural.strategy_side_policy == "UPPER_P75_ONLY") & (natural.entry_execution_policy == "RECHECK_AT_NEXT_OPEN")], ["exit_center_policy", "data_scope"])
    entry = _aggregate(natural[(natural.history_model == "EXPANDING_PAST") & (natural.strategy_side_policy == "UPPER_P75_ONLY") & (natural.exit_center_policy == "FROZEN_ENTRY_MEAN")], ["entry_execution_policy", "data_scope"])
    hold_events = events[(events.history_model == "EXPANDING_PAST") & (events.strategy_side_policy == "UPPER_P75_ONLY") & (events.entry_execution_policy == "RECHECK_AT_NEXT_OPEN") & (events.exit_center_policy == "FROZEN_ENTRY_MEAN")]
    hold = _aggregate(hold_events, ["max_holding_minutes", "data_scope"])
    session = primary.copy()
    session["utc_hour"] = _utc(session.entry_exec_time).dt.hour
    session["stock_session"] = np.where(session.utc_hour.between(0, 6), "KOREA_CASH_OPEN", "KOREA_CASH_CLOSED")
    return {
        "primary_pair_results": complete(_aggregate(primary, ["pair"]), {"pair": pairs}),
        "history_window_comparison": complete(history, {"history_model": list(HISTORY_MODELS), "data_scope": list(SCOPES)}),
        "side_policy_comparison": complete(side, {"strategy_side_policy": list(SIDE_POLICIES), "data_scope": list(SCOPES)}),
        "exit_center_comparison": complete(center, {"exit_center_policy": list(EXIT_POLICIES), "data_scope": list(SCOPES)}),
        "entry_policy_comparison": complete(entry, {"entry_execution_policy": list(ENTRY_POLICIES), "data_scope": list(SCOPES)}),
        "max_hold_comparison": complete(hold, {"max_holding_minutes": [np.nan, 1440.0, 4320.0, 10080.0], "data_scope": list(SCOPES)}),
        "gate_vs_nongate": complete(_aggregate(primary, ["gate_group"]), {"gate_group": ["GATE_RELATED", "NON_GATE"]}),
        "funding_attribution": complete(_aggregate(primary, ["pair"]), {"pair": pairs}),
        "session_comparison": _aggregate(session.dropna(subset=["entry_exec_time"]), ["stock_session"]),
        "utc_hour_comparison": _aggregate(session.dropna(subset=["entry_exec_time"]), ["utc_hour"]),
    }


def _save_chart(filename: str, title: str, xlabel: str = "", ylabel: str = "") -> None:
    plt.title(title + "\n1m trade OHLC proxy; not historical BBO")
    plt.xlabel(xlabel); plt.ylabel(ylabel); plt.tight_layout()
    plt.savefig(CHART_DIR / filename, dpi=135); plt.close()


def make_charts(prepared: PreparedData, events: pd.DataFrame, artifacts: dict,
                tables: dict[str, pd.DataFrame], curve: pd.DataFrame) -> None:
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    primary = primary_events(events)
    closed = primary[primary.status.isin(REALIZED_STATUSES)]
    # 1 and 2: six panels, dynamic expanding bands and execution markers.
    for filename, markers in [("01_spread_quantile_timeseries.png", False), ("02_entries_exits.png", True)]:
        fig, axes = plt.subplots(3, 2, figsize=(14, 10), sharex=False)
        for ax, (a, b) in zip(axes.flat, PAIRS):
            frame = artifacts["frames"][a, b, "PAIR_NATIVE_WINDOW"]
            hist = artifacts["histories"][a, b, "PAIR_NATIVE_WINDOW", "EXPANDING_PAST"]
            sample = slice(None, None, max(1, len(frame) // 12000))
            ax.plot(frame.index[sample], frame.close_spread_bps.iloc[sample], lw=.35, color="#5875a4")
            ax.plot(hist.index[sample], hist.historical_mean.iloc[sample], lw=.7, color="black")
            ax.plot(hist.index[sample], hist.historical_p25.iloc[sample], lw=.55, color="#55a868")
            ax.plot(hist.index[sample], hist.historical_p75.iloc[sample], lw=.55, color="#c44e52")
            if markers:
                q = primary[primary.pair.eq(f"{a}/{b}")]
                ax.scatter(q.signal_time, q.signal_close_spread_bps, s=5, color="#dd8452", label="signal")
                r = q[q.status.isin(REALIZED_STATUSES)]
                ax.scatter(r.exit_signal_time, r.exit_signal_spread_bps, s=5, color="#55a868", label="exit")
            ax.set_title(f"{a}/{b}")
        fig.suptitle(("Entry/exit markers" if markers else "Causal mean and P25/P75") + " — 1m trade OHLC proxy, not historical BBO")
        fig.tight_layout(); fig.savefig(CHART_DIR / filename, dpi=135); plt.close(fig)
    h = tables["history_window_comparison"].query("data_scope == 'PAIR_NATIVE_WINDOW'")
    h.plot.bar(x="history_model", y="signal_count", legend=False); _save_chart("03_event_count_by_history.png", "Events by history window", "history", "events")
    h.set_index("history_model")[["mean_net_account_price_pnl_bps", "median_net_account_price_pnl_bps"]].plot.bar(); _save_chart("04_history_pnl.png", "History-window net PnL", "history", "account bps")
    p = tables["primary_pair_results"].set_index("pair")
    p[["mean_net_account_price_pnl_bps", "median_net_account_price_pnl_bps"]].plot.bar(); _save_chart("05_pair_pnl.png", "Primary PnL by pair", "pair", "account bps")
    tables["gate_vs_nongate"].set_index("gate_group")[["mean_net_account_price_pnl_bps", "median_net_account_price_pnl_bps"]].plot.bar(); _save_chart("06_gate_comparison.png", "Gate-related vs non-Gate", "group", "account bps")
    for name, table, x, filename, title in [
        ("side", tables["side_policy_comparison"], "strategy_side_policy", "07_side_policy.png", "Upper-only vs two-sided"),
        ("center", tables["exit_center_comparison"], "exit_center_policy", "08_mean_median_exit.png", "Mean vs median exit"),
        ("dynamic", tables["exit_center_comparison"], "exit_center_policy", "09_frozen_dynamic_exit.png", "Frozen vs dynamic center")]:
        q = table[table.data_scope.eq("PAIR_NATIVE_WINDOW")].set_index(x)
        q[["mean_net_account_price_pnl_bps", "median_net_account_price_pnl_bps"]].plot.bar()
        _save_chart(filename, title, x, "account bps")
    closed.net_account_price_pnl_bps.hist(bins=40); _save_chart("10_pnl_distribution.png", "Primary net PnL distribution", "account bps", "events")
    for pair, group in closed.groupby("pair"):
        values = np.sort(group.holding_minutes.dropna());
        if len(values): plt.step(values, np.arange(1, len(values) + 1) / len(values), where="post", label=pair)
    plt.legend(fontsize=7); _save_chart("11_holding_ecdf.png", "Holding-time ECDF", "minutes", "ECDF")
    closed[["mae_account_price_pnl_bps", "mfe_account_price_pnl_bps"]].plot.hist(bins=40, alpha=.55); _save_chart("12_mae_mfe.png", "MAE and MFE", "account bps", "events")
    status = primary.groupby(["pair", "status"]).size().unstack(fill_value=0); status.div(status.sum(axis=1), axis=0).plot.bar(stacked=True); _save_chart("13_status_share.png", "Event status shares", "pair", "share")
    closed.plot.scatter(x="holding_minutes", y="net_account_price_pnl_bps", alpha=.5); _save_chart("14_pnl_vs_holding.png", "PnL vs holding time", "minutes", "account bps")
    closed.plot.scatter(x="tail_score", y="net_account_price_pnl_bps", alpha=.5); _save_chart("15_pnl_vs_tail.png", "PnL vs causal tail score", "tail score", "account bps")
    tables["funding_attribution"].set_index("pair")[["mean_net_account_price_pnl_bps", "mean_net_funding_account_bps", "mean_net_combined_account_pnl_bps"]].plot.bar(); _save_chart("16_funding_attribution.png", "Price and funding attribution", "pair", "account bps")
    curve.plot(x="event_number", y=["compounded_equity_usd", "non_compounded_equity_usd"]); _save_chart("17_global_equity.png", "ONE_POSITION_GLOBAL equity", "event", "USD")
    if len(closed):
        session = closed.assign(utc_hour=_utc(closed.entry_exec_time).dt.hour,
                                stock_session=np.where(_utc(closed.entry_exec_time).dt.hour.between(0, 6), "KOREA_CASH_OPEN", "KOREA_CASH_CLOSED"))
        session.groupby("utc_hour").net_account_price_pnl_bps.mean().plot.bar(); _save_chart("18_utc_hour_session.png", "Results by UTC entry hour / Korea cash-session proxy", "UTC hour", "mean account bps")
    else:
        plt.text(.5, .5, "No closed primary events", ha="center"); _save_chart("18_utc_hour_session.png", "Results by UTC entry hour", "UTC hour", "mean account bps")


def _md_table(frame: pd.DataFrame) -> str:
    return "```text\n" + frame.to_string(index=False) + "\n```"


def write_reports(prepared: PreparedData, events: pd.DataFrame, summary: pd.DataFrame,
                  tables: dict[str, pd.DataFrame], global_events: pd.DataFrame,
                  curve: pd.DataFrame, global_metrics: dict) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    prepared.data_coverage.to_csv(REPORT_DIR / "data_coverage.csv", index=False)
    prepared.pair_coverage.to_csv(REPORT_DIR / "pair_coverage.csv", index=False)
    events.to_parquet(REPORT_DIR / "events_1m.parquet", index=False)
    summary.to_csv(REPORT_DIR / "summary_1m.csv", index=False)
    status = events.groupby(SUMMARY_KEYS + ["status"], dropna=False).size().rename("event_count").reset_index()
    status.to_csv(REPORT_DIR / "event_status_summary.csv", index=False)
    for filename, table in tables.items():
        table.to_csv(REPORT_DIR / f"{filename}.csv", index=False)
    global_events.to_csv(REPORT_DIR / "global_one_position_events.csv", index=False)
    curve.to_csv(REPORT_DIR / "global_one_position_equity_curve.csv", index=False)

    methodology = f"""# Methodology

This study uses **native 1-minute trade OHLC bars**. It does not use mark/index
prices, reconstructed 15-minute bars, forward filling, interpolation, or
synthetic timestamps. It is an OHLC execution proxy, not historical BBO and not
an executable backtest.

- Run time: {prepared.run_time.isoformat()}
- Dynamic Gate start: {prepared.gate_start.isoformat()}
- Spread: `20000 * (A - B) / (A + B)` bps.
- Signal: completed close crossing causal P75 (and P25 in the two-sided sensitivity).
- Statistics exclude the current value with `shift(1)`; rolling histories reset after gaps.
- Execution proxy: next contiguous minute open; the primary entry rechecks the frozen threshold.
- Primary exit: crossing the mean frozen at entry, then next contiguous minute open.
- Account PnL: `0.5 * (long_return + short_return) * 10000 - 20 bps`.
- Funding includes only actual events satisfying `entry < funding_time < exit`.
- Natural right-censored and gap events are not assigned terminal PnL.
- `PAIR_NATIVE_WINDOW` is primary; `COMMON_FOUR_WINDOW` is a sensitivity.
- Day-block intervals use deterministic seed 20260726.

The non-duplicated scenario set covers all requested history, side, entry,
center, and maximum-hold comparisons.
"""
    (REPORT_DIR / "methodology.md").write_text(methodology, encoding="utf-8")

    primary = tables["primary_pair_results"].copy()
    main_summary = summary[(summary.data_scope == "PAIR_NATIVE_WINDOW") & summary.max_holding_minutes.isna()]
    robust = main_summary[(main_summary.realized_count >= 30) &
                          (main_summary.mean_net_account_price_pnl_bps > 0) &
                          (main_summary.median_net_account_price_pnl_bps > 0) &
                          (main_summary.day_block_bootstrap_ci_low > 0) &
                          (main_summary.unresolved_rate <= .2)]
    verdict = "ROBUST CANDIDATE(S) FOUND" if len(robust) else "NO ROBUST CANDIDATE"
    primary_closed = primary_events(events)
    primary_closed = primary_closed[primary_closed.status.isin(REALIZED_STATUSES)]
    funding_mean = primary_closed.net_funding_account_bps.mean()
    funding_text = "N/A" if pd.isna(funding_mean) else f"{funding_mean:.4f}"
    if len(primary_closed):
        p90_hold = primary_closed.holding_minutes.quantile(.9)
        long_mean = primary_closed.loc[primary_closed.holding_minutes >= p90_hold, "net_account_price_pnl_bps"].mean()
        short_mean = primary_closed.loc[primary_closed.holding_minutes < p90_hold, "net_account_price_pnl_bps"].mean()
        stability_text = (f"The longest 10% of closed primary events (at least {p90_hold:.1f} minutes) average "
                          f"{long_mean:.2f} account bps versus {short_mean:.2f} bps for the rest. "
                          "This diagnoses whether a small long-duration tail drives the aggregate.")
    else:
        stability_text = "There are no closed primary events for duration-tail attribution."
    sections = [
        "# Executive summary — P75 / historical-center native 1m study",
        "## Verdict",
        f"**{verdict}.** After the specified 20 bps cost, {len(robust)} tested main-scope configurations satisfy all robustness conditions.",
        "This is a 1-minute trade-OHLC historical proxy study, not historical BBO, not an executable backtest, and it does not prove live profitability.",
        f"Run time: `{prepared.run_time.isoformat()}`. Dynamic Gate first valid minute: `{prepared.gate_start.isoformat()}`.",
        "## Exchange coverage and latest-data lag", _md_table(prepared.data_coverage),
        "## Pair-native and common-four windows", _md_table(prepared.pair_coverage),
        "## Primary pair results", _md_table(primary.round(4)),
        "## History windows", _md_table(tables["history_window_comparison"].round(4)),
        "## Upper-only vs two-sided", _md_table(tables["side_policy_comparison"].round(4)),
        "## Frozen/dynamic mean and median exits", _md_table(tables["exit_center_comparison"].round(4)),
        "## Entry execution sensitivity", _md_table(tables["entry_policy_comparison"].round(4)),
        "## Maximum holding sensitivity", _md_table(tables["max_hold_comparison"].round(4)),
        "## Duration-tail stability", stability_text,
        "## Gate and funding", _md_table(tables["gate_vs_nongate"].round(4)),
        f"Mean primary funding attribution is `{funding_text}` account bps per closed event; it is separate from the price conclusion.",
        "## ONE_POSITION_GLOBAL", _md_table(pd.DataFrame([global_metrics]).round(6)),
        "Pair selection counts:", _md_table(global_events.groupby("pair").size().rename("selected_count").reset_index() if len(global_events) else pd.DataFrame(columns=["pair", "selected_count"])),
        "The selector ranks same-minute candidates only by causal tail score, never future holding time or PnL.",
        "## UTC hour and Korea cash-session proxy", _md_table(tables["session_comparison"].round(4)),
        "## Cost and interpretation",
        "Each leg receives half of gross notional. Two leg returns are summed, multiplied by 0.5, and the 20 bps full round-trip cost is deducted once. Unclosed natural events receive no invented terminal PnL.",
    ]
    executive = "\n\n".join(sections) + "\n"
    (REPORT_DIR / "EXECUTIVE_SUMMARY.md").write_text(executive, encoding="utf-8")
    chart_tags = "\n".join(f'<figure><img src="charts/{html.escape(path.name)}"><figcaption>{html.escape(path.stem)}</figcaption></figure>' for path in sorted(CHART_DIR.glob("*.png")))
    report_html = ("<!doctype html><html><head><meta charset='utf-8'><title>P75 1m study</title>"
                   "<style>body{font:15px system-ui;max-width:1200px;margin:auto;padding:24px}img{max-width:100%}figure{margin:28px 0}pre{overflow:auto;background:#f5f5f5;padding:12px}</style></head>"
                   f"<body><h1>P75 / historical-center native 1m study</h1><p><strong>{verdict}</strong></p>"
                   "<p>1m trade OHLC proxy — not historical BBO, not executable backtest.</p>"
                   f"<pre>{html.escape(primary.round(4).to_string(index=False))}</pre>{chart_tags}</body></html>")
    (REPORT_DIR / "report.html").write_text(report_html, encoding="utf-8")


def run_study(root: Path = ROOT, report_dir: Path | None = None,
              run_time: pd.Timestamp | None = None) -> dict:
    """Load, simulate, summarize, chart, and write the reproducible study."""
    global REPORT_DIR, CHART_DIR
    if report_dir is not None:
        REPORT_DIR = Path(report_dir)
        CHART_DIR = REPORT_DIR / "charts"
    run_time = pd.Timestamp.now(tz="UTC") if run_time is None else pd.Timestamp(run_time)
    prices = load_prices(root, run_time)
    funding = load_funding(root)
    prepared = prepare_data(prices, run_time)
    events, artifacts = run_scenarios(prepared, funding)
    summary = summarize_events(events)
    tables = comparison_tables(events, summary)
    window_start = prepared.pair_coverage.start_time.min()
    window_end = prepared.pair_coverage.end_time_exclusive.max()
    global_events, curve, global_metrics = simulate_global(events, window_start, window_end)
    make_charts(prepared, events, artifacts, tables, curve)
    write_reports(prepared, events, summary, tables, global_events, curve, global_metrics)
    return {"prepared": prepared, "events": events, "summary": summary, "tables": tables,
            "global_events": global_events, "global_curve": curve,
            "global_metrics": global_metrics, "output_dir": REPORT_DIR}


if __name__ == "__main__":
    result = run_study()
    print(f"events={len(result['events'])}; output={result['output_dir']}")
