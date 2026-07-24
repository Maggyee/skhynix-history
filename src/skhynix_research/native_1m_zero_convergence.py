"""Native one-minute, zero-convergence OHLC proxy study.

This is deliberately described as a historical trade-OHLC proxy study, not an
executable backtest: signals use a completed bar close and executions use the
next contiguous bar open.  Missing observations are never filled.
"""
from __future__ import annotations

from dataclasses import dataclass
from concurrent.futures import ProcessPoolExecutor
from itertools import combinations
from pathlib import Path
import html
import math
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports_1m_zero_convergence"
CHART_DIR = REPORT_DIR / "charts"
EXCHANGES = ("binance", "bitget", "gate", "okx")
PAIRS = tuple(combinations(EXCHANGES, 2))
THRESHOLDS = (20, 50, 100, 150, 200, 250, 300)
CONFIRMATIONS = ("ONE_BAR_CONFIRM", "TWO_BAR_CONFIRM")
EXIT_POLICIES = ("ZERO_CROSS", "ZERO_BAND_5", "ZERO_BAND_10", "ZERO_BAND_20", "ZERO_CROSS_OR_5BPS")
MAX_HOLDS = (None, 60, 240, 720, 1440)
SCOPES = ("STRICT_ALL_FOUR_INTERSECTION", "STRICT_PAIR_INTERSECTION")
EVENT_STATUSES = ("REALIZED", "RIGHT_CENSORED", "MAX_HOLD", "NO_NEXT_BAR_FOR_ENTRY",
                  "NO_NEXT_BAR_FOR_EXIT", "DATA_GAP_DURING_HOLD", "INVALID_PRICE", "OVERLAP_BLOCKED")
EVENT_COLUMNS = [
    "pair", "data_scope", "confirmation_policy", "threshold_bps", "exit_policy",
    "max_holding_minutes", "signal_time", "signal_close_spread_bps", "entry_exec_time",
    "entry_open_spread_bps", "entry_direction", "long_exchange", "short_exchange",
    "entry_long_price", "entry_short_price", "exit_signal_time",
    "exit_signal_close_spread_bps", "exit_exec_time", "exit_open_spread_bps",
    "exit_long_price", "exit_short_price", "holding_minutes", "gross_price_pnl_bps",
    "funding_pnl_bps", "net_price_pnl_bps", "net_combined_pnl_bps",
    "mae_price_pnl_bps", "mfe_price_pnl_bps", "max_abs_spread_during_hold_bps",
    "close_reason", "status",
]
TIME_COLUMNS = ["signal_time", "entry_exec_time", "exit_signal_time", "exit_exec_time"]
NUMERIC_EVENT_COLUMNS = [
    "threshold_bps", "max_holding_minutes", "signal_close_spread_bps", "entry_open_spread_bps",
    "entry_long_price", "entry_short_price", "exit_signal_close_spread_bps", "exit_open_spread_bps",
    "exit_long_price", "exit_short_price", "holding_minutes", "gross_price_pnl_bps", "funding_pnl_bps",
    "net_price_pnl_bps", "net_combined_pnl_bps", "mae_price_pnl_bps", "mfe_price_pnl_bps",
    "max_abs_spread_during_hold_bps",
]


def normalize_event_types(events: pd.DataFrame) -> pd.DataFrame:
    events=events.copy()
    for c in NUMERIC_EVENT_COLUMNS:
        if c in events: events[c]=pd.to_numeric(events[c],errors="coerce")
    for c in TIME_COLUMNS:
        if c in events: events[c]=pd.to_datetime(events[c],utc=True,errors="coerce")
    return events


def spread_bps(a, b):
    """Symmetric directed spread, scalar or array."""
    return 20_000.0 * (a - b) / (a + b)


def _valid_ohlc(p: pd.DataFrame) -> pd.Series:
    cols = ["open", "high", "low", "close"]
    finite = np.isfinite(p[cols]).all(axis=1)
    positive = p[cols].gt(0).all(axis=1)
    ordered = (p.high >= p[["open", "close"]].max(axis=1)) & (p.low <= p[["open", "close"]].min(axis=1)) & (p.high >= p.low)
    open_time = pd.to_datetime(p.open_time, utc=True, errors="coerce")
    close_time = pd.to_datetime(p.close_time, utc=True, errors="coerce")
    completed = close_time >= open_time + pd.Timedelta(seconds=59)
    # Completion is determined from the native minute's open boundary. Gate's
    # normalized close_time currently carries a known +16h40 source offset, so
    # comparing retrieval time with that field would incorrectly discard valid
    # native bars. The bar is complete once retrieval is at least t+1 minute.
    if "retrieved_at" in p:
        retrieved_at = pd.to_datetime(p.retrieved_at, utc=True, errors="coerce")
        completed &= retrieved_at >= open_time + pd.Timedelta(minutes=1)
    return finite & positive & ordered & completed


@dataclass(frozen=True)
class PreparedData:
    trade: pd.DataFrame
    wide: pd.DataFrame
    all_four: pd.DatetimeIndex
    window_start: pd.Timestamp
    window_end: pd.Timestamp
    coverage: pd.DataFrame


def prepare_prices(prices: pd.DataFrame) -> PreparedData:
    """Filter native trade bars and calculate the actual four-exchange window."""
    required = {"exchange", "price_type", "open_time", "close_time", "open", "high", "low", "close"}
    missing = required - set(prices.columns)
    if missing:
        raise ValueError(f"prices_1m missing columns: {sorted(missing)}")
    p = prices.loc[prices.exchange.isin(EXCHANGES) & prices.price_type.eq("trade")].copy()
    p["open_time"] = pd.to_datetime(p.open_time, utc=True, errors="coerce")
    p["close_time"] = pd.to_datetime(p.close_time, utc=True, errors="coerce")
    p = p[p.open_time.dt.second.eq(0) & p.open_time.dt.microsecond.eq(0)]
    p = p[_valid_ohlc(p)].sort_values(["exchange", "open_time", "retrieved_at"] if "retrieved_at" in p else ["exchange", "open_time"])
    p = p.drop_duplicates(["exchange", "open_time"], keep="last")
    absent = set(EXCHANGES) - set(p.exchange.unique())
    if absent:
        raise ValueError(f"no valid native trade bars for: {sorted(absent)}")
    bounds = p.groupby("exchange").open_time.agg(["min", "max"])
    start = bounds["min"].max()
    end = bounds["max"].min() + pd.Timedelta(minutes=1)
    if start >= end:
        raise ValueError("empty four-exchange time window")
    p = p[(p.open_time >= start) & (p.open_time < end)].copy()
    wide = p.pivot(index="open_time", columns="exchange", values=["open", "high", "low", "close"]).sort_index()
    valid_sets = {ex: pd.DatetimeIndex(p.loc[p.exchange.eq(ex), "open_time"].unique()) for ex in EXCHANGES}
    all_four = valid_sets[EXCHANGES[0]]
    for ex in EXCHANGES[1:]:
        all_four = all_four.intersection(valid_sets[ex])
    all_four = all_four.sort_values()
    expected = max(0, int((end - start) / pd.Timedelta(minutes=1)))
    cov = []
    for ex in EXCHANGES:
        q = p[p.exchange.eq(ex)]
        available = q.open_time.nunique()
        cov.append({"exchange": ex, "first_valid_minute": bounds.loc[ex, "min"],
                    "last_valid_minute": bounds.loc[ex, "max"], "strict_window_start": start,
                    "strict_window_end_exclusive": end, "expected_minutes_in_window": expected,
                    "valid_minutes_in_window": available, "missing_minutes_in_window": expected - available,
                    "coverage_percent": 100 * available / expected if expected else np.nan})
    return PreparedData(p, wide, all_four, start, end, pd.DataFrame(cov))


def pair_frame(prepared: PreparedData, a: str, b: str, scope: str) -> pd.DataFrame:
    idx = prepared.all_four if scope == SCOPES[0] else prepared.wide.dropna(subset=[("open", a), ("open", b), ("close", a), ("close", b)]).index
    w = prepared.wide.reindex(idx)
    out = pd.DataFrame(index=idx)
    for field in ("open", "close"):
        out[f"{field}_a"] = w[(field, a)].to_numpy()
        out[f"{field}_b"] = w[(field, b)].to_numpy()
        out[f"{field}_spread_bps"] = spread_bps(out[f"{field}_a"], out[f"{field}_b"])
    return out


def _exit_hit(value: float, sign: int, policy: str) -> bool:
    cross = value <= 0 if sign > 0 else value >= 0
    if policy == "ZERO_CROSS":
        return cross
    band = {"ZERO_BAND_5": 5, "ZERO_BAND_10": 10, "ZERO_BAND_20": 20}.get(policy)
    if band is not None:
        return abs(value) <= band
    if policy == "ZERO_CROSS_OR_5BPS":
        return cross or abs(value) <= 5
    raise ValueError(policy)


def funding_pnl_bps(funding: pd.DataFrame | None, long_ex: str, short_ex: str,
                    entry_time: pd.Timestamp, exit_time: pd.Timestamp) -> float:
    """Real settlements strictly after entry and strictly before exit."""
    if funding is None or funding.empty:
        return 0.0
    times = pd.to_datetime(funding.funding_time, utc=True, errors="coerce")
    q = funding[(times > entry_time) & (times < exit_time)]
    return float((-q.loc[q.exchange.eq(long_ex), "funding_rate"].sum() +
                  q.loc[q.exchange.eq(short_ex), "funding_rate"].sum()) * 10_000)


def _pnl(sign: int, a0: float, b0: float, a1: float, b1: float) -> float:
    if sign > 0:  # short A, long B
        return float(((b1 / b0 - 1) + (1 - a1 / a0)) * 10_000)
    return float(((a1 / a0 - 1) + (1 - b1 / b0)) * 10_000)


def _blank_event(a, b, scope, confirm, threshold, exit_policy, max_hold, signal_time, signal_spread):
    r = {c: np.nan for c in EVENT_COLUMNS}
    r.update(pair=f"{a}/{b}", data_scope=scope, confirmation_policy=confirm,
             threshold_bps=threshold, exit_policy=exit_policy,
             max_holding_minutes=max_hold, signal_time=signal_time,
             signal_close_spread_bps=signal_spread,
             entry_direction="SHORT_A_LONG_B" if signal_spread > 0 else "LONG_A_SHORT_B",
             long_exchange=b if signal_spread > 0 else a,
             short_exchange=a if signal_spread > 0 else b)
    return r


def simulate_pair(frame: pd.DataFrame, a: str, b: str, scope: str, confirmation: str,
                  threshold: int, exit_policy: str, max_hold: int | None,
                  funding: pd.DataFrame | None = None) -> pd.DataFrame:
    """One-position state machine for a single fully specified scenario."""
    if frame.empty:
        return pd.DataFrame(columns=EVENT_COLUMNS)
    times = frame.index
    open_a=frame.open_a.to_numpy(float); open_b=frame.open_b.to_numpy(float)
    open_spread=frame.open_spread_bps.to_numpy(float)
    close_a=frame.close_a.to_numpy(float); close_b=frame.close_b.to_numpy(float)
    close_spread=frame.close_spread_bps.to_numpy(float)
    rows, i, armed = [], 0, True
    while i < len(frame):
        s = close_spread[i]
        beyond = abs(s) >= threshold
        if not beyond:
            armed = True
            i += 1
            continue
        if not armed:
            i += 1
            continue
        confirmed = confirmation == "ONE_BAR_CONFIRM"
        if not confirmed and i > 0 and times[i] - times[i - 1] == pd.Timedelta(minutes=1):
            prev = close_spread[i-1]
            confirmed = abs(prev) >= threshold and np.sign(prev) == np.sign(s)
        if not confirmed:
            i += 1
            continue
        armed = False
        ev = _blank_event(a, b, scope, confirmation, threshold, exit_policy, max_hold, times[i], s)
        if i + 1 >= len(frame) or times[i + 1] - times[i] != pd.Timedelta(minutes=1):
            ev.update(close_reason="NO_NEXT_BAR_FOR_ENTRY", status="NO_NEXT_BAR_FOR_ENTRY")
            rows.append(ev); i += 1; continue
        entry_i, sign = i + 1, 1 if s > 0 else -1
        a0, b0 = open_a[entry_i], open_b[entry_i]
        if not all(np.isfinite([a0, b0])) or min(a0, b0) <= 0:
            ev.update(entry_exec_time=times[entry_i], close_reason="INVALID_PRICE", status="INVALID_PRICE")
            rows.append(ev); i = entry_i + 1; continue
        ev.update(entry_exec_time=times[entry_i], entry_open_spread_bps=open_spread[entry_i],
                  entry_long_price=b0 if sign > 0 else a0, entry_short_price=a0 if sign > 0 else b0)
        marks, max_abs, j, finished = [], abs(close_spread[entry_i]), entry_i, False
        while j < len(frame):
            marks.append(_pnl(sign, a0, b0, close_a[j], close_b[j]))
            max_abs = max(max_abs, abs(close_spread[j]))
            natural = _exit_hit(close_spread[j], sign, exit_policy)
            reached_max = max_hold is not None and (times[j] + pd.Timedelta(minutes=1) - times[entry_i]) >= pd.Timedelta(minutes=max_hold)
            if natural or reached_max:
                reason = "ZERO_CONVERGENCE" if natural else "MAX_HOLD"
                if j + 1 >= len(frame) or times[j + 1] - times[j] != pd.Timedelta(minutes=1):
                    ev.update(exit_signal_time=times[j], exit_signal_close_spread_bps=close_spread[j],
                              close_reason="NO_NEXT_BAR_FOR_EXIT", status="NO_NEXT_BAR_FOR_EXIT")
                    rows.append(ev); i = j + 1; finished = True; break
                a1, b1 = open_a[j+1], open_b[j+1]
                if not all(np.isfinite([a1, b1])) or min(a1, b1) <= 0:
                    ev.update(exit_signal_time=times[j], exit_signal_close_spread_bps=close_spread[j],
                              exit_exec_time=times[j + 1], close_reason="INVALID_PRICE", status="INVALID_PRICE")
                    rows.append(ev); i = j + 2; finished = True; break
                gross = _pnl(sign, a0, b0, a1, b1)
                fund = funding_pnl_bps(funding, ev["long_exchange"], ev["short_exchange"], times[entry_i], times[j + 1])
                marks.append(gross)
                ev.update(exit_signal_time=times[j], exit_signal_close_spread_bps=close_spread[j],
                          exit_exec_time=times[j + 1], exit_open_spread_bps=open_spread[j+1],
                          exit_long_price=b1 if sign > 0 else a1, exit_short_price=a1 if sign > 0 else b1,
                          holding_minutes=(times[j + 1] - times[entry_i]).total_seconds() / 60,
                          gross_price_pnl_bps=gross, funding_pnl_bps=fund,
                          net_price_pnl_bps=gross - 20, net_combined_pnl_bps=gross + fund - 20,
                          mae_price_pnl_bps=min(marks), mfe_price_pnl_bps=max(marks),
                          max_abs_spread_during_hold_bps=max_abs, close_reason=reason,
                          status="REALIZED" if natural else "MAX_HOLD")
                rows.append(ev); i = j + 2; finished = True; break
            if j + 1 < len(frame) and times[j + 1] - times[j] != pd.Timedelta(minutes=1):
                ev.update(holding_minutes=(times[j] + pd.Timedelta(minutes=1) - times[entry_i]).total_seconds() / 60,
                          mae_price_pnl_bps=min(marks), mfe_price_pnl_bps=max(marks),
                          max_abs_spread_during_hold_bps=max_abs,
                          close_reason="DATA_GAP_DURING_HOLD", status="DATA_GAP_DURING_HOLD")
                rows.append(ev); i = j + 1; finished = True; break
            j += 1
        if not finished:
            ev.update(holding_minutes=(times[-1] + pd.Timedelta(minutes=1) - times[entry_i]).total_seconds() / 60,
                      mae_price_pnl_bps=min(marks), mfe_price_pnl_bps=max(marks),
                      max_abs_spread_during_hold_bps=max_abs,
                      close_reason="RIGHT_CENSORED", status="RIGHT_CENSORED")
            rows.append(ev); i = len(frame)
    return pd.DataFrame(rows, columns=EVENT_COLUMNS)


SUMMARY_KEYS = ["pair", "threshold_bps", "confirmation_policy", "exit_policy", "max_holding_minutes", "data_scope"]


def _bootstrap_ci(realized: pd.DataFrame, samples=500, seed=20260724):
    if realized.empty:
        return np.nan, np.nan
    daily = realized.assign(day=pd.to_datetime(realized.entry_exec_time, utc=True).dt.floor("D")).groupby("day").net_price_pnl_bps.mean().to_numpy()
    if len(daily) == 1:
        return float(daily[0]), float(daily[0])
    rng = np.random.default_rng(seed)
    means = rng.choice(daily, (samples, len(daily)), replace=True).mean(axis=1)
    return tuple(np.quantile(means, [.025, .975]))


def summarize_events(events: pd.DataFrame) -> pd.DataFrame:
    cols = SUMMARY_KEYS + ["total_signal_count", "realized_event_count", "right_censored_count", "censor_rate",
        "positive_event_count", "win_rate", "sum_net_price_pnl_bps", "mean_net_price_pnl_bps",
        "median_net_price_pnl_bps", "p05_net_price_pnl_bps", "p25_net_price_pnl_bps",
        "p75_net_price_pnl_bps", "p95_net_price_pnl_bps", "mean_gross_price_pnl_bps",
        "mean_funding_pnl_bps", "mean_net_combined_pnl_bps", "median_holding_minutes",
        "p90_holding_minutes", "max_holding_minutes_observed", "mean_mae_bps", "median_mae_bps",
        "mean_mfe_bps", "day_block_bootstrap_ci_low", "day_block_bootstrap_ci_high"]
    if events.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for key, g in events.groupby(SUMMARY_KEYS, dropna=False, sort=False):
        r = g[g.status.isin(["REALIZED", "MAX_HOLD"])]
        x = r.net_price_pnl_bps.dropna()
        lo, hi = _bootstrap_ci(r)
        row = dict(zip(SUMMARY_KEYS, key)); n = len(g)
        row.update(total_signal_count=n, realized_event_count=len(r), right_censored_count=int(g.status.eq("RIGHT_CENSORED").sum()),
            censor_rate=float(g.status.eq("RIGHT_CENSORED").sum() / n), positive_event_count=int(x.gt(0).sum()),
            win_rate=float(x.gt(0).mean()) if len(x) else np.nan, sum_net_price_pnl_bps=x.sum(min_count=1),
            mean_net_price_pnl_bps=x.mean(), median_net_price_pnl_bps=x.median(),
            p05_net_price_pnl_bps=x.quantile(.05), p25_net_price_pnl_bps=x.quantile(.25),
            p75_net_price_pnl_bps=x.quantile(.75), p95_net_price_pnl_bps=x.quantile(.95),
            mean_gross_price_pnl_bps=r.gross_price_pnl_bps.mean(), mean_funding_pnl_bps=r.funding_pnl_bps.mean(),
            mean_net_combined_pnl_bps=r.net_combined_pnl_bps.mean(), median_holding_minutes=r.holding_minutes.median(),
            p90_holding_minutes=r.holding_minutes.quantile(.9), max_holding_minutes_observed=r.holding_minutes.max(),
            mean_mae_bps=r.mae_price_pnl_bps.mean(), median_mae_bps=r.mae_price_pnl_bps.median(),
            mean_mfe_bps=r.mfe_price_pnl_bps.mean(), day_block_bootstrap_ci_low=lo, day_block_bootstrap_ci_high=hi)
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def complete_summary_grid(summary: pd.DataFrame) -> pd.DataFrame:
    """Retain all 4,200 requested configurations, including zero-signal cells."""
    pairs=[f"{a}/{b}" for a,b in PAIRS]
    grid=pd.MultiIndex.from_product([pairs,THRESHOLDS,CONFIRMATIONS,EXIT_POLICIES,
        [np.nan if x is None else x for x in MAX_HOLDS],SCOPES],names=SUMMARY_KEYS).to_frame(index=False)
    left=grid.copy();right=summary.copy()
    left["_hold_key"]=left.max_holding_minutes.fillna(-1)
    right["_hold_key"]=right.max_holding_minutes.fillna(-1)
    right=right.drop(columns="max_holding_minutes")
    out=left.merge(right,on=["pair","threshold_bps","confirmation_policy","exit_policy","data_scope","_hold_key"],how="left")
    out=out.drop(columns="_hold_key")
    count_cols=["total_signal_count","realized_event_count","right_censored_count","positive_event_count"]
    for c in count_cols:
        out[c]=out[c].fillna(0).astype(int)
    return out[summary.columns]


def _run_pair_scenarios(args):
    prepared,funding,a,b,scope=args
    out = []
    f=pair_frame(prepared,a,b,scope)
    for confirmation in CONFIRMATIONS:
        for threshold in THRESHOLDS:
            for policy in EXIT_POLICIES:
                for max_hold in MAX_HOLDS:
                    out.append(simulate_pair(f,a,b,scope,confirmation,threshold,policy,max_hold,funding))
    return pd.concat(out,ignore_index=True) if out else pd.DataFrame(columns=EVENT_COLUMNS)


def run_all_scenarios(prepared: PreparedData, funding: pd.DataFrame | None, workers: int | None = None) -> pd.DataFrame:
    jobs=[(prepared,funding,a,b,scope) for a,b in PAIRS for scope in SCOPES]
    workers=min(6,os.cpu_count() or 1) if workers is None else workers
    if workers<=1:
        out=[_run_pair_scenarios(job) for job in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            out=list(pool.map(_run_pair_scenarios,jobs))
    result=pd.concat(out,ignore_index=True) if out else pd.DataFrame(columns=EVENT_COLUMNS)
    return normalize_event_types(result)


def simulate_global(prepared: PreparedData, funding: pd.DataFrame | None,
                    threshold=20, confirmation="ONE_BAR_CONFIRM", max_hold=None) -> pd.DataFrame:
    """Single-capital portfolio for the primary all-four / combined-exit scenario.

    Candidate ranking happens at the common t+1 open, when execution is assumed;
    it never examines an eventual exit or PnL.
    """
    frames = {(a, b): pair_frame(prepared, a, b, SCOPES[0]) for a, b in PAIRS}
    times = prepared.all_four
    selected=[]; i=0
    # A pair that was selected cannot be selected again until its threshold
    # episode has actually reset. Other pairs that were blocked only by the
    # global position remain eligible when scanning resumes.
    selected_episodes: set[tuple[str, str]] = set()
    while i < len(times):
        candidates=[]
        for a,b in PAIRS:
            f=frames[a,b]; s=float(f.iloc[i].close_spread_bps)
            if abs(s) < threshold:
                selected_episodes.discard((a, b))
                continue
            if (a, b) in selected_episodes:
                continue
            confirmed=confirmation=="ONE_BAR_CONFIRM"
            if not confirmed and i>0 and times[i]-times[i-1]==pd.Timedelta(minutes=1):
                prev=float(f.iloc[i-1].close_spread_bps)
                confirmed=abs(prev)>=threshold and np.sign(prev)==np.sign(s)
            if not confirmed: continue
            if i+1>=len(times) or times[i+1]-times[i]!=pd.Timedelta(minutes=1):
                continue
            candidates.append((abs(float(f.iloc[i+1].open_spread_bps)),a,b))
        if not candidates:
            i+=1; continue
        _,a,b=max(candidates,key=lambda x:x[0])
        selected_episodes.add((a, b))
        # Start the isolated state machine exactly at this chosen signal. For
        # two-bar confirmation include its immediately preceding evidence bar.
        start=max(0,i-1) if confirmation=="TWO_BAR_CONFIRM" else i
        q=simulate_pair(frames[a,b].iloc[start:],a,b,SCOPES[0],confirmation,threshold,
                        "ZERO_CROSS_OR_5BPS",max_hold,funding)
        if q.empty:
            i+=1; continue
        row=q.iloc[0].copy(); selected.append(row)
        if pd.isna(row.entry_exec_time):
            i+=1; continue
        if pd.isna(row.exit_exec_time):
            break
        # An exit at this minute's open permits a new signal only at this
        # minute's completed close, never at an earlier close.
        i=int(times.searchsorted(row.exit_exec_time))
    return normalize_event_types(pd.DataFrame(selected,columns=EVENT_COLUMNS))


def global_equity(events: pd.DataFrame, initial=1000.0) -> pd.DataFrame:
    rows = [{"time": pd.NaT, "event_number": 0, "pair": "START", "event_net_return": 0.0,
             "compounded_equity_usd": initial, "non_compounded_equity_usd": initial}]
    comp = simple = initial
    realized = events[events.status.isin(["REALIZED", "MAX_HOLD"])].sort_values("exit_exec_time")
    for n, (_, e) in enumerate(realized.iterrows(), 1):
        # Event PnL is the sum of equal-notional leg returns. The requested
        # capital base is total two-leg gross notional, so divide by two when
        # mapping event bps to a return on that gross-notional account.
        ret = float(e.net_price_pnl_bps) / 20_000
        comp *= 1 + ret; simple += initial * ret
        rows.append({"time": e.exit_exec_time, "event_number": n, "pair": e.pair, "event_net_return": ret,
                     "compounded_equity_usd": comp, "non_compounded_equity_usd": simple})
    out=pd.DataFrame(rows)
    out["time"]=pd.to_datetime(out.time,utc=True)
    return out


def _save_chart(name, title, xlabel="", ylabel=""):
    plt.title(title); plt.xlabel(xlabel); plt.ylabel(ylabel); plt.tight_layout()
    plt.savefig(CHART_DIR / name, dpi=140); plt.close()


def make_charts(prepared, events, summary, global_curve):
    CHART_DIR.mkdir(parents=True, exist_ok=True)
    primary = summary[(summary.data_scope.eq(SCOPES[0])) & summary.exit_policy.eq("ZERO_CROSS_OR_5BPS") &
                      summary.confirmation_policy.eq("ONE_BAR_CONFIRM") & summary.max_holding_minutes.isna()]
    realized = events[events.status.isin(["REALIZED", "MAX_HOLD"])]
    # 1 spread series
    fig, axes = plt.subplots(3, 2, figsize=(13, 10), sharex=True)
    for ax, (a, b) in zip(axes.flat, PAIRS):
        f = pair_frame(prepared, a, b, SCOPES[0]); ax.plot(f.index, f.close_spread_bps, lw=.45); ax.axhline(0, color="black", lw=.5); ax.set_title(f"{a}/{b}")
    fig.suptitle("Directed 1-minute trade-close spreads (strict all-four)"); fig.tight_layout(); fig.savefig(CHART_DIR/"spread_timeseries_six_pairs.png",dpi=140); plt.close(fig)
    specs = [
        ("event_count_by_threshold.png", primary, "threshold_bps", "total_signal_count", "Signals by threshold"),
        ("censor_rate_by_threshold.png", primary, "threshold_bps", "censor_rate", "Right-censor rate by threshold"),
    ]
    for name,d,x,y,title in specs:
        for pair,g in d.groupby("pair"): plt.plot(g[x],g[y],marker="o",label=pair)
        plt.legend(fontsize=7); _save_chart(name,title,x,y)
    agg=primary.groupby("threshold_bps")[["mean_net_price_pnl_bps","median_net_price_pnl_bps"]].mean(); agg.plot(marker="o"); _save_chart("net_pnl_by_threshold.png","Mean and median net price PnL","threshold (bps)","bps")
    q=realized[(realized.data_scope.eq(SCOPES[0])) & realized.exit_policy.eq("ZERO_CROSS_OR_5BPS") & realized.confirmation_policy.eq("ONE_BAR_CONFIRM") & realized.max_holding_minutes.isna()]
    q.net_price_pnl_bps.hist(bins=40); _save_chart("net_pnl_distribution.png","Net price PnL distribution","bps","events")
    for pair,g in q.groupby("pair"):
        x=np.sort(g.holding_minutes.dropna()); plt.step(x,np.arange(1,len(x)+1)/len(x),where="post",label=pair)
    plt.legend(fontsize=7); _save_chart("holding_time_ecdf.png","Holding-time ECDF","minutes","ECDF")
    q[["mae_price_pnl_bps","mfe_price_pnl_bps"]].plot.hist(bins=40,alpha=.55); _save_chart("mae_mfe_distribution.png","MAE / MFE distribution","bps","events")
    for group,col,name,title in [("exit_policy","mean_net_price_pnl_bps","exit_policy_comparison.png","Exit-policy comparison"),("confirmation_policy","mean_net_price_pnl_bps","confirmation_comparison.png","Confirmation comparison"),("data_scope","mean_net_price_pnl_bps","scope_comparison.png","Intersection-scope comparison")]:
        d=summary[(summary.threshold_bps.eq(20)) & summary.max_holding_minutes.isna()].groupby(group)[col].mean(); d.plot.bar(); _save_chart(name,title,group,"mean bps")
    if len(global_curve):
        gc=global_curve
        if "threshold_bps" in gc:
            gc=gc[(gc.threshold_bps.eq(20)) & gc.confirmation_policy.eq("ONE_BAR_CONFIRM") & gc.max_holding_minutes.isna()]
        gc.plot(x="event_number",y=["compounded_equity_usd","non_compounded_equity_usd"]); _save_chart("global_one_position_equity.png","ONE_POSITION_GLOBAL equity (20 bps, one bar, natural)","event","USD")
    attr=q.groupby("pair")[["gross_price_pnl_bps","funding_pnl_bps"]].mean(); attr["cost_bps"]=-20; attr.plot.bar(); _save_chart("pair_pnl_attribution.png","Price, funding and fixed-cost attribution","pair","mean bps")
    # hold sensitivity is the twelfth chart and makes forced exits auditable.
    d=summary[(summary.data_scope.eq(SCOPES[0])) & summary.exit_policy.eq("ZERO_CROSS_OR_5BPS") & summary.confirmation_policy.eq("ONE_BAR_CONFIRM") & summary.threshold_bps.eq(20)].copy(); d["hold_label"]=d.max_holding_minutes.fillna(-1).astype(int).astype(str).replace("-1","natural"); d.groupby("hold_label").mean_net_price_pnl_bps.mean().plot.bar(); _save_chart("max_hold_comparison.png","Maximum-holding sensitivity","maximum minutes","mean bps")


def _fmt(x, digits=2):
    return "N/A" if pd.isna(x) else f"{x:.{digits}f}"


def _markdown_table(frame: pd.DataFrame) -> str:
    """Dependency-free, readable table for Markdown reports."""
    return "```text\n"+frame.to_string(index=False)+"\n```"


def _aggregate_events(events: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Event-weighted compact output for report-facing result tables."""
    cols = keys + ["total_signal_count", "realized_event_count", "right_censored_count",
                   "censor_rate", "positive_event_count", "win_rate",
                   "mean_net_price_pnl_bps", "median_net_price_pnl_bps",
                   "mean_gross_price_pnl_bps", "mean_funding_pnl_bps",
                   "mean_net_combined_pnl_bps", "median_holding_minutes"]
    if events.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for key, g in events.groupby(keys, dropna=False, sort=True):
        key = key if isinstance(key, tuple) else (key,)
        realized = g[g.status.isin(["REALIZED", "MAX_HOLD"])]
        pnl = realized.net_price_pnl_bps.dropna()
        n = len(g)
        censored = int(g.status.eq("RIGHT_CENSORED").sum())
        row = dict(zip(keys, key))
        row.update(total_signal_count=n, realized_event_count=len(realized),
                   right_censored_count=censored,
                   censor_rate=censored / n if n else np.nan,
                   positive_event_count=int(pnl.gt(0).sum()),
                   win_rate=float(pnl.gt(0).mean()) if len(pnl) else np.nan,
                   mean_net_price_pnl_bps=pnl.mean(),
                   median_net_price_pnl_bps=pnl.median(),
                   mean_gross_price_pnl_bps=realized.gross_price_pnl_bps.mean(),
                   mean_funding_pnl_bps=realized.funding_pnl_bps.mean(),
                   mean_net_combined_pnl_bps=realized.net_combined_pnl_bps.mean(),
                   median_holding_minutes=realized.holding_minutes.median())
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def complete_aggregate_grid(table: pd.DataFrame, dimensions: dict[str, tuple | list]) -> pd.DataFrame:
    """Add explicit zero-signal rows to compact report-facing tables."""
    keys=list(dimensions)
    grid=pd.MultiIndex.from_product([dimensions[k] for k in keys],names=keys).to_frame(index=False)
    out=grid.merge(table,on=keys,how="left")
    for c in ["total_signal_count","realized_event_count","right_censored_count","positive_event_count"]:
        if c in out: out[c]=out[c].fillna(0).astype(int)
    return out


def write_reports(prepared, events, summary, global_events, curve):
    primary_events = events[(events.data_scope.eq(SCOPES[0])) & events.exit_policy.eq("ZERO_CROSS_OR_5BPS") &
                            events.confirmation_policy.eq("ONE_BAR_CONFIRM") & events.max_holding_minutes.isna()]
    pairs=[f"{a}/{b}" for a,b in PAIRS]
    by_t = complete_aggregate_grid(_aggregate_events(primary_events, ["threshold_bps"]),{"threshold_bps":THRESHOLDS})
    pair = complete_aggregate_grid(_aggregate_events(primary_events, ["pair"]),{"pair":pairs})
    pair_threshold = complete_aggregate_grid(_aggregate_events(primary_events, ["pair", "threshold_bps"]),{"pair":pairs,"threshold_bps":THRESHOLDS})
    positive = by_t[(by_t.mean_net_price_pnl_bps > 0) & (by_t.median_net_price_pnl_bps > 0)].threshold_bps.tolist()
    min_med = by_t.loc[by_t.median_net_price_pnl_bps > 0, "threshold_bps"].min() if (by_t.median_net_price_pnl_bps > 0).any() else np.nan
    best = pair.sort_values("mean_net_price_pnl_bps",ascending=False).iloc[0] if len(pair) else None
    worst=pair.sort_values("mean_net_price_pnl_bps").iloc[0] if len(pair) else None
    gate = pair.assign(group=np.where(pair.pair.str.contains("gate"), "Gate-related", "non-Gate")).groupby("group").agg(
        pairs=("pair", "count"), mean_net_bps=("mean_net_price_pnl_bps", "mean"),
        median_net_bps=("median_net_price_pnl_bps", "median"), mean_censor_rate=("censor_rate", "mean")).reset_index()
    confirmation = _aggregate_events(events[(events.data_scope.eq(SCOPES[0])) & events.exit_policy.eq("ZERO_CROSS_OR_5BPS") &
                                            events.max_holding_minutes.isna()], ["confirmation_policy"])
    exit_comparison = _aggregate_events(events[(events.data_scope.eq(SCOPES[0])) &
                                                events.confirmation_policy.eq("ONE_BAR_CONFIRM") &
                                                events.max_holding_minutes.isna()], ["exit_policy"])
    hold_comparison = _aggregate_events(events[(events.data_scope.eq(SCOPES[0])) &
                                                events.confirmation_policy.eq("ONE_BAR_CONFIRM") &
                                                events.exit_policy.eq("ZERO_CROSS_OR_5BPS") &
                                                (events.max_holding_minutes.isna() | events.max_holding_minutes.isin([240, 1440]))],
                                       ["max_holding_minutes"])
    scope_comparison = _aggregate_events(events[(events.exit_policy.eq("ZERO_CROSS_OR_5BPS")) &
                                                 events.confirmation_policy.eq("ONE_BAR_CONFIRM") &
                                                 events.max_holding_minutes.isna()], ["data_scope"])
    natural_realized = int(primary_events.status.eq("REALIZED").sum())
    natural_censored = int(primary_events.status.eq("RIGHT_CENSORED").sum())
    natural_gap = int(primary_events.status.eq("DATA_GAP_DURING_HOLD").sum())
    natural_total = len(primary_events)
    observed_convergence_rate = natural_realized / natural_total if natural_total else np.nan
    natural_rate = natural_realized / (natural_realized + natural_censored) if natural_realized + natural_censored else np.nan
    funding_mean = primary_events.loc[primary_events.status.eq("REALIZED"), "funding_pnl_bps"].mean()
    gc=curve
    if len(gc) and "threshold_bps" in gc:
        gc=gc[(gc.threshold_bps.eq(20)) & gc.confirmation_policy.eq("ONE_BAR_CONFIRM") & gc.max_holding_minutes.isna()]
    ge=gc.iloc[-1] if len(gc) else None
    methodology=f"""# Methodology: native 1-minute zero convergence

This is a historical **trade OHLC proxy study, not an executable backtest and not historical BBO**. It does not establish a live-tradable strategy.

- Input: `data/normalized/prices_1m.parquet`; only valid `trade` OHLC for {', '.join(EXCHANGES)}. Hyperliquid, mark and index rows are excluded.
- The input file is the collector's native one-minute product; no 15-minute reconstruction, forward fill, interpolation, or timestamp fabrication is used.
- Actual common window: `{prepared.window_start}` inclusive to `{prepared.window_end}` exclusive, calculated from each exchange's valid rows.
- `STRICT_ALL_FOUR_INTERSECTION` requires all four exchanges at the exact minute; `STRICT_PAIR_INTERSECTION` requires the exact two. Both remain inside the common four-exchange boundary.
- Signal: completed close; execution proxy: next contiguous minute open. One pair has at most one position, and an above-threshold episode must reset below threshold before another entry.
- Cost: 20 bps once per realized round trip. Censored or invalid events have no realized PnL. Event bps use the specified sum of equal-notional leg returns; the $1,000 global curve divides this by two because $1,000 is total two-leg gross notional.
- Funding cashflow is `-long funding + short funding`, using real settlements strictly inside `entry < funding_time < exit`. Combined net is price gross + funding cashflow - 20 bps (the sign follows the stated cashflow definition).
- MAE/MFE are close-marked price PnLs after the entry-open proxy, plus the exit-open observation; they are not intrabar executable excursions.
- Day-block confidence intervals resample daily mean event PnLs (500 deterministic bootstrap draws). Sparse samples should not be treated as asymptotic evidence.
- `sum_net_price_pnl_bps` is an event sum, not account return; overlapping pair results cannot be added as portfolio return.
"""
    REPORT_DIR.joinpath("methodology.md").write_text(methodology,encoding="utf-8")
    answers=f"""# Native 1-minute zero-convergence study

> Historical one-minute trade-OHLC proxy only. This is not historical BBO, not an executable backtest, and not evidence of a live-tradable strategy.

## Window and primary result

The dynamically calculated strict boundary is **{prepared.window_start} inclusive to {prepared.window_end} exclusive**. The main comparison uses strict all-four timestamps, one-bar confirmation, natural holding, and `ZERO_CROSS_OR_5BPS`.

- Lowest tested trigger with positive event median: **{_fmt(min_med,0)} bps**.
- Thresholds whose event-weighted mean and median are both positive: **{positive or 'none'}**.
- Best pair across the seven independently simulated thresholds: **{best['pair'] if best is not None else 'N/A'}**, mean {_fmt(best['mean_net_price_pnl_bps'] if best is not None else np.nan)} bps, median {_fmt(best['median_net_price_pnl_bps'] if best is not None else np.nan)} bps.
- Worst pair: **{worst['pair'] if worst is not None else 'N/A'}**, mean {_fmt(worst['mean_net_price_pnl_bps'] if worst is not None else np.nan)} bps, median {_fmt(worst['median_net_price_pnl_bps'] if worst is not None else np.nan)} bps.
- Observed natural convergence among all primary signals: **{_fmt(100 * observed_convergence_rate)}%** ({natural_realized}/{natural_total}). There are **{natural_censored} right-censored** and **{natural_gap} data-gap-during-hold** events. Conditional on the small subset observable through convergence or window end, convergence is {_fmt(100 * natural_rate)}%; that conditional figure must not be generalized across gap-interrupted signals.
- Mean funding attribution on naturally realized primary events: **{_fmt(funding_mean)} bps** ({'improves' if funding_mean > 0 else 'worsens' if funding_mean < 0 else 'does not change'} the mean combined result).
- ONE_POSITION_GLOBAL ends at compounded **${_fmt(ge.compounded_equity_usd if ge is not None else np.nan)}** and non-compounded **${_fmt(ge.non_compounded_equity_usd if ge is not None else np.nan)}** from $1,000 gross two-leg notional.

## Answers and interpretation

1. The minimum positive-median threshold and all thresholds positive on both metrics are stated above.
2. The threshold table below reports all seven independently, including mean, median, win rate, event count and censoring.
3. Mean-versus-median and win-rate differences show whether tails dominate; P05/P95 remain available per pair/configuration in `summary_1m.csv`.
4. The pair table ranks all six pairs. The ranking averages independent threshold scenarios; it is not a portfolio return.
5. The Gate/non-Gate comparison is shown below. It is descriptive and does not establish a causal venue effect.
6. The observed convergence, right-censor and data-gap shares are stated separately above; data failures are not mislabeled as censoring.
7. Each threshold's censor rate is in the threshold table.
8. The confirmation table quantifies whether two-bar confirmation changes count, mean and median.
9. The exit table compares strict cross, 5/10/20 bps bands, and the primary combined rule.
10. The holding-limit table directly compares natural, 240-minute and 1440-minute rules; 60/720 remain in `summary_1m.csv`.
11. Funding is shown independently above and in every detailed summary; price-only net remains primary.
12. The one-position capital result is stated above. Selection uses only the candidate's actual next-open spread, never its future exit or PnL.
13. The sample can motivate a real-time BBO paper experiment only. Trade OHLC is not BBO and these results are not a live-tradable strategy.

## Primary threshold table

{_markdown_table(by_t)}

## Primary pair table

{_markdown_table(pair)}

## Gate-related versus non-Gate

{_markdown_table(gate)}

## Confirmation comparison

{_markdown_table(confirmation)}

## Exit-policy comparison

{_markdown_table(exit_comparison)}

## Natural versus maximum holding limits

{_markdown_table(hold_comparison)}

## Strict all-four versus pair intersection

{_markdown_table(scope_comparison)}

## Six pairs × seven thresholds

{_markdown_table(pair_threshold)}
"""
    REPORT_DIR.joinpath("EXECUTIVE_SUMMARY.md").write_text(answers,encoding="utf-8")
    imgs="".join(f'<h3>{html.escape(p.stem)}</h3><img src="charts/{html.escape(p.name)}">' for p in sorted(CHART_DIR.glob("*.png")))
    body=f"<!doctype html><html><meta charset='utf-8'><title>Native 1m zero convergence</title><style>body{{max-width:1250px;margin:30px auto;font:15px system-ui;line-height:1.5}}.warn{{background:#fff3cd;padding:12px}}table{{border-collapse:collapse;font-size:12px;margin-bottom:24px}}td,th{{border:1px solid #ddd;padding:4px}}img{{max-width:100%}}</style><h1>Native 1-minute zero-convergence</h1><p class='warn'>Historical trade OHLC proxy—not historical BBO, an executable backtest, or a live-tradable strategy.</p><p>Window: {prepared.window_start} inclusive to {prepared.window_end} exclusive.</p><h2>Thresholds</h2>{by_t.to_html(index=False)}<h2>Pairs</h2>{pair.to_html(index=False)}<h2>Confirmation</h2>{confirmation.to_html(index=False)}<h2>Exit policies</h2>{exit_comparison.to_html(index=False)}<h2>Holding limits</h2>{hold_comparison.to_html(index=False)}<h2>Scopes</h2>{scope_comparison.to_html(index=False)}{imgs}</html>"
    REPORT_DIR.joinpath("report.html").write_text(body,encoding="utf-8")


def run(prices_path=ROOT/"data/normalized/prices_1m.parquet", funding_path=ROOT/"data/normalized/funding_events.parquet", report_dir=REPORT_DIR):
    global REPORT_DIR, CHART_DIR
    REPORT_DIR=Path(report_dir); CHART_DIR=REPORT_DIR/"charts"; CHART_DIR.mkdir(parents=True,exist_ok=True)
    prices=pd.read_parquet(prices_path); funding=pd.read_parquet(funding_path) if Path(funding_path).exists() else pd.DataFrame(columns=["exchange","funding_time","funding_rate"])
    prepared=prepare_prices(prices)
    prepared.coverage.to_csv(REPORT_DIR/"data_coverage.csv",index=False)
    pd.DataFrame({"open_time":prepared.all_four}).to_csv(REPORT_DIR/"strict_common_minutes.csv",index=False)
    events=run_all_scenarios(prepared,funding); events.to_csv(REPORT_DIR/"events_1m.csv",index=False)
    summary=complete_summary_grid(summarize_events(events)); summary.to_csv(REPORT_DIR/"summary_1m.csv",index=False)
    primary_events=events[(events.data_scope.eq(SCOPES[0])) & events.exit_policy.eq("ZERO_CROSS_OR_5BPS") &
                          events.confirmation_policy.eq("ONE_BAR_CONFIRM") & events.max_holding_minutes.isna()]
    pairs=[f"{a}/{b}" for a,b in PAIRS]
    complete_aggregate_grid(_aggregate_events(primary_events,["pair","threshold_bps"]),{"pair":pairs,"threshold_bps":THRESHOLDS}).to_csv(REPORT_DIR/"pair_results_1m.csv",index=False)
    complete_aggregate_grid(_aggregate_events(primary_events,["threshold_bps"]),{"threshold_bps":THRESHOLDS}).to_csv(REPORT_DIR/"threshold_results_1m.csv",index=False)
    exit_events=events[(events.data_scope.eq(SCOPES[0])) & events.max_holding_minutes.isna()]
    complete_aggregate_grid(_aggregate_events(exit_events,["exit_policy","confirmation_policy","threshold_bps"]),{"exit_policy":EXIT_POLICIES,"confirmation_policy":CONFIRMATIONS,"threshold_bps":THRESHOLDS}).to_csv(REPORT_DIR/"exit_policy_results_1m.csv",index=False)
    global_events=[]; curves=[]
    for threshold in THRESHOLDS:
        for confirmation in CONFIRMATIONS:
            for max_hold in (None,240,1440):
                q=simulate_global(prepared,funding,threshold,confirmation,max_hold)
                global_events.append(q)
                c=global_equity(q); c.at[0,"time"]=prepared.window_start
                c["threshold_bps"]=threshold;c["confirmation_policy"]=confirmation;c["max_holding_minutes"]=max_hold
                curves.append(c)
    ge=pd.concat(global_events,ignore_index=True) if global_events else pd.DataFrame(columns=EVENT_COLUMNS)
    ge.to_csv(REPORT_DIR/"global_one_position_events.csv",index=False)
    curve=pd.concat(curves,ignore_index=True); curve.to_csv(REPORT_DIR/"global_one_position_equity_curve.csv",index=False)
    make_charts(prepared,events,summary,curve); write_reports(prepared,events,summary,ge,curve)
    return {"prepared":prepared,"events":events,"summary":summary,"global_events":ge,"equity":curve}


if __name__ == "__main__":
    result=run(); p=result["prepared"]
    print(f"strict_window=[{p.window_start}, {p.window_end}) common_minutes={len(p.all_four):,} events={len(result['events']):,}")
