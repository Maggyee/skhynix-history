from __future__ import annotations

import hashlib
import itertools
import math
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns

from .analysis import detect_scale_mismatch, symmetric_spread_bps
from .calendar import trading_dates
from .common_windows import GATE_END, GATE_START, gate_regime
from .config import ROOT

R = ROOT / "reports"
C = R / "charts"
R15 = ROOT / "reports_15m"
C15 = R15 / "charts"
THRESHOLDS = (20, 50, 100, 150, 200)
HORIZONS = (5, 15, 30, 60, 240, 1440)
EXIT_RULES = (
    "target_20bps", "target_10bps", "target_0bps", "hold_1h", "hold_4h",
    "hold_8h", "hold_24h", "hold_72h", "next_krx_open", "stop_50bps", "stop_100bps",
)
COSTS = (10, 20, 40, 80, 120)


def _stable_id(prefix: str, *parts) -> str:
    raw = "|".join(str(x) for x in parts)
    return f"{prefix}-{hashlib.sha256(raw.encode()).hexdigest()[:20]}"


def _side_names(row, signed):
    if signed >= 0:
        return row.exchange_A, row.exchange_B
    return row.exchange_B, row.exchange_A


def _ns(series):
    x = pd.to_datetime(series, utc=True)
    return (x.dt.as_unit("ns") if isinstance(x, pd.Series) else x.as_unit("ns")).astype("int64").to_numpy()


def _first_hit_index(values, condition):
    hits = np.flatnonzero(condition(values))
    return int(hits[0]) if len(hits) else None


def build_base_events(pairdf: pd.DataFrame, dates: set, thresholds=THRESHOLDS, frequency_minutes: int = 1) -> pd.DataFrame:
    """Build one row per pair/threshold market event, independent of strategy scenarios."""
    from .history_quality import detailed_session

    rows = []
    step_ns = frequency_minutes * 60 * 10**9
    for pair, original in pairdf.groupby("pair", sort=True):
        g = original.sort_values("minute").drop_duplicates("minute", keep="last").reset_index(drop=True)
        if g.empty:
            continue
        times = _ns(g.minute)
        spreads = g.spread.to_numpy(float)
        abs_spreads = np.abs(spreads)
        pair_start = g.minute.iloc[0]
        pair_end = g.minute.iloc[-1] + pd.Timedelta(minutes=frequency_minutes)
        for threshold in thresholds:
            # Scan the complete observed timeline.  A genuinely missing bucket
            # may be bridged once, but an observed below-threshold bucket must
            # end the event immediately.  Filtering to active rows first would
            # incorrectly treat a real threshold crossing as missing data.
            event_groups = []
            current = []
            statuses = []
            for idx in range(len(g)):
                is_active = abs_spreads[idx] >= threshold
                if not current:
                    if is_active:
                        current = [idx]
                    continue
                delta = times[idx] - times[current[-1]]
                if not is_active:
                    event_groups.append(np.asarray(current, dtype=int))
                    statuses.append("DATA_GAP_CENSORED" if delta > 2 * step_ns else "COMPLETED")
                    current = []
                elif delta <= 2 * step_ns:
                    current.append(idx)
                else:
                    event_groups.append(np.asarray(current, dtype=int))
                    statuses.append("DATA_GAP_CENSORED")
                    current = [idx]
            if current:
                event_groups.append(np.asarray(current, dtype=int))
                statuses.append("RIGHT_CENSORED")

            for active_idx, status in zip(event_groups, statuses):
                start_i, last_i = int(active_idx[0]), int(active_idx[-1])
                start_t, last_t = g.minute.iloc[start_i], g.minute.iloc[last_i]

                peak_i = int(active_idx[np.argmax(abs_spreads[active_idx])])
                start_row, peak_row, end_row = g.iloc[start_i], g.iloc[peak_i], g.iloc[last_i]
                high_start, low_start = _side_names(start_row, spreads[start_i])
                high_peak, low_peak = _side_names(peak_row, spreads[peak_i])

                # Observation for convergence stops before the first long data gap,
                # pair end, or the largest requested 1-day horizon.
                obs_end_i = start_i
                max_ns = times[start_i] + max(HORIZONS) * 60 * 10**9
                for j in range(start_i + 1, len(g)):
                    if times[j] - times[j - 1] > 2 * step_ns or times[j] > max_ns:
                        break
                    obs_end_i = j
                obs_spreads = spreads[start_i:obs_end_i + 1]
                obs_abs = np.abs(obs_spreads)
                rel_times = (times[start_i:obs_end_i + 1] - times[start_i]) / 60 / 10**9

                hit20 = _first_hit_index(obs_abs[1:], lambda x: x < 20)
                hit10 = _first_hit_index(obs_abs[1:], lambda x: x < 10)
                entry_sign = 1 if spreads[start_i] >= 0 else -1
                hit0 = _first_hit_index(obs_spreads[1:], lambda x: entry_sign * x <= 0)
                hit20 = hit20 + 1 if hit20 is not None else None
                hit10 = hit10 + 1 if hit10 is not None else None
                hit0 = hit0 + 1 if hit0 is not None else None
                minutes20 = float(rel_times[hit20]) if hit20 is not None else np.nan
                minutes10 = float(rel_times[hit10]) if hit10 is not None else np.nan
                minutes0 = float(rel_times[hit0]) if hit0 is not None else np.nan
                observed_minutes = float(rel_times[-1]) if len(rel_times) else 0.0
                if obs_end_i == len(g) - 1:
                    observation_status = "RIGHT_CENSORED"
                elif times[obs_end_i + 1] - times[obs_end_i] > 2 * step_ns:
                    observation_status = "DATA_GAP_CENSORED"
                else:
                    observation_status = "FULL_1440M_OBSERVED" if observed_minutes >= 1440 else "COMPLETED_BEFORE_HORIZON"

                event_id = _stable_id("BE", pair, threshold, pd.Timestamp(start_t).isoformat(), frequency_minutes)
                rec = {
                    "base_event_id": event_id, "pair": pair, "threshold_bps": threshold,
                    "event_start": start_t, "event_end": last_t, "last_observed_time": last_t,
                    "duration_observed_minutes": int(round((last_t - start_t).total_seconds() / 60)) + frequency_minutes,
                    "status": status, "is_right_censored": status == "RIGHT_CENSORED",
                    "is_data_gap_censored": status == "DATA_GAP_CENSORED",
                    "start_spread_bps": spreads[start_i], "peak_abs_spread_bps": abs_spreads[peak_i],
                    "peak_signed_spread_bps": spreads[peak_i], "peak_time": g.minute.iloc[peak_i],
                    "end_spread_bps": spreads[last_i], "higher_exchange_at_start": high_start,
                    "lower_exchange_at_start": low_start, "higher_exchange_at_peak": high_peak,
                    "lower_exchange_at_peak": low_peak, "start_session": detailed_session(start_t, dates),
                    "peak_session": detailed_session(g.minute.iloc[peak_i], dates),
                    "start_regime": gate_regime(start_t), "peak_regime": gate_regime(g.minute.iloc[peak_i]),
                    "comparison_quality": start_row.comparison_quality,
                    "pair_common_start": pair_start, "pair_common_end": pair_end,
                    "warning": "historical_mark_trade_proxy_not_executable_BBO" + (f";{status}" if status != "COMPLETED" else ""),
                    "minutes_to_below_20bps": minutes20, "minutes_to_below_10bps": minutes10,
                    "minutes_to_zero_or_cross": minutes0, "observation_minutes_available": observed_minutes,
                    "observation_status": observation_status, "frequency_minutes": frequency_minutes,
                }
                for target, minutes in [(20, minutes20), (10, minutes10), (0, minutes0)]:
                    for horizon in HORIZONS:
                        rec[f"hit_{target}bps_within_{horizon}m"] = bool(pd.notna(minutes) and minutes <= horizon)
                rows.append(rec)
    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values(["pair", "threshold_bps", "event_start"]).drop_duplicates("base_event_id").reset_index(drop=True)
    return out


def _duration_stats(group_type, group_value, g, population="ALL_OBSERVED"):
    d = g.duration_observed_minutes.astype(float)
    n = len(g)
    rec = {
        "group_type": group_type, "group_value": group_value, "population": population, "threshold_bps": 20,
        "event_count_total": n, "event_count_completed": int((g.status == "COMPLETED").sum()),
        "event_count_right_censored": int((g.status == "RIGHT_CENSORED").sum()),
        "event_count_data_gap_censored": int((g.status == "DATA_GAP_CENSORED").sum()),
        "censoring_ratio": float((g.status != "COMPLETED").mean()) if n else np.nan,
    }
    for name, value in [("min_minutes", d.min()), ("p25_minutes", d.quantile(.25)), ("median_minutes", d.median()),
                        ("p75_minutes", d.quantile(.75)), ("p90_minutes", d.quantile(.90)),
                        ("p95_minutes", d.quantile(.95)), ("p99_minutes", d.quantile(.99)),
                        ("max_observed_minutes", d.max())]:
        rec[name] = value if n else np.nan
    for horizon in (5, 15, 30, 60, 240):
        rec[f"count_le_{horizon}m"] = int((d <= horizon).sum())
        rec[f"ratio_le_{horizon}m"] = float((d <= horizon).mean()) if n else np.nan
    rec["count_gt_240m"] = int((d > 240).sum())
    rec["ratio_gt_240m"] = float((d > 240).mean()) if n else np.nan
    rec["count_gt_1440m"] = int((d > 1440).sum())
    rec["ratio_gt_1440m"] = float((d > 1440).mean()) if n else np.nan
    return rec


def duration_summary(base_events, output_dir=R, suffix=""):
    output_dir.mkdir(parents=True, exist_ok=True)
    e = base_events[base_events.threshold_bps == 20].copy()
    rows = [_duration_stats("ALL", "ALL_OBSERVED", e)]
    completed = e[e.status == "COMPLETED"]
    rows.append(_duration_stats("STATUS_SCOPE", "COMPLETED_ONLY", completed, "COMPLETED_ONLY"))
    rows.append(_duration_stats("STATUS_SCOPE", "ALL_OBSERVED", e))
    for col, kind in [("pair", "PAIR"), ("start_session", "START_SESSION"), ("start_regime", "START_REGIME")]:
        for value, g in e.groupby(col, dropna=False):
            rows.append(_duration_stats(kind, str(value), g))
    for (pair, regime), g in e.groupby(["pair", "start_regime"]):
        rows.append(_duration_stats("PAIR_REGIME", f"{pair}|{regime}", g))
    gate_related = e.pair.str.contains("gate")
    rows.append(_duration_stats("GATE_RELATED", "GATE_RELATED", e[gate_related]))
    rows.append(_duration_stats("GATE_RELATED", "NON_GATE", e[~gate_related]))
    summary = pd.DataFrame(rows)
    summary.to_csv(output_dir / f"event_duration_summary_20bps{suffix}.csv", index=False)
    longest = e.nlargest(50, "duration_observed_minutes").copy().reset_index(drop=True)
    longest.insert(0, "rank", np.arange(1, len(longest) + 1))
    keep = ["rank", "base_event_id", "pair", "event_start", "event_end", "duration_observed_minutes", "status",
            "peak_abs_spread_bps", "start_session", "start_regime", "higher_exchange_at_peak", "lower_exchange_at_peak", "warning"]
    longest[keep].to_csv(output_dir / f"longest_spread_events_20bps{suffix}.csv", index=False)
    return summary, longest


def convergence_summary(base_events, output_dir=R):
    e = base_events[base_events.threshold_bps == 20].copy()
    rows = []
    groups = [("ALL", "ALL", e)]
    groups += [("PAIR", k, g) for k, g in e.groupby("pair")]
    groups += [("START_SESSION", k, g) for k, g in e.groupby("start_session")]
    groups += [("START_REGIME", k, g) for k, g in e.groupby("start_regime")]
    for group_type, group_value, g in groups:
        for target in (20, 10, 0):
            minutes_col = {20:"minutes_to_below_20bps", 10:"minutes_to_below_10bps", 0:"minutes_to_zero_or_cross"}[target]
            for horizon in HORIZONS:
                hit = g[minutes_col].notna() & (g[minutes_col] <= horizon)
                eligible = hit | (g.observation_minutes_available >= horizon)
                rows.append({
                    "group_type": group_type, "group_value": group_value, "threshold_bps": 20,
                    "target_bps": target, "horizon_minutes": horizon, "event_count_total": len(g),
                    "hit_count": int(hit.sum()), "naive_hit_rate": float(hit.mean()) if len(g) else np.nan,
                    "eligible_event_count": int(eligible.sum()),
                    "completed_event_count": int((g.status == "COMPLETED").sum()),
                    "censored_event_count": int((g.status != "COMPLETED").sum()),
                    "observed_rate_complete_cases": float(hit[eligible].mean()) if eligible.any() else np.nan,
                    "rate_note": "完整可观察事件比例；不是Kaplan-Meier或无偏总体概率",
                })
    out = pd.DataFrame(rows)
    out.to_csv(output_dir / "event_convergence_summary.csv", index=False)
    return out


def _fund_arrays(funding):
    out = {}
    for ex, g in funding.groupby("exchange"):
        g = g.sort_values("funding_time")
        out[ex] = (_ns(g.funding_time), g.funding_rate.to_numpy(float))
    return out


def _sum_funding(arrays, ex, start_ns, end_ns):
    times, rates = arrays[ex]
    lo, hi = np.searchsorted(times, [start_ns, end_ns])
    return float(rates[lo:hi].sum()), int(hi - lo)


def _next_krx_open(t, dates):
    choices = [pd.Timestamp(d, tz="UTC") for d in sorted(dates) if pd.Timestamp(d, tz="UTC") > pd.Timestamp(t)]
    return choices[0] if choices else pd.NaT


def build_strategy_scenarios(pairdf, funding, base_events, dates, run_end):
    """Expand every base event into retained scenarios, including misses/censoring."""
    from .history_quality import detailed_session, excursion_metrics

    arrays = _fund_arrays(funding)
    pair_groups = {p:g.sort_values("minute").drop_duplicates("minute").reset_index(drop=True) for p,g in pairdf.groupby("pair")}
    rows = []
    for event in base_events.itertuples(index=False):
        g = pair_groups[event.pair]
        times = _ns(g.minute); spreads = g.spread.to_numpy(float); abs_spreads = np.abs(spreads)
        pa = g.price_A.to_numpy(float); pb = g.price_B.to_numpy(float)
        i = int(np.searchsorted(times, pd.Timestamp(event.event_start).value))
        if i >= len(g) or times[i] != pd.Timestamp(event.event_start).value:
            continue
        entry = g.iloc[i]
        entry_ns = times[i]
        horizon_end_ns = entry_ns + 72 * 3600 * 10**9
        contiguous_end = i
        gap_after = False
        for j in range(i + 1, len(g)):
            if times[j] - times[j - 1] > 2 * 60 * 10**9:
                gap_after = True
                break
            if times[j] > horizon_end_ns:
                break
            contiguous_end = j
        reached_72h = times[contiguous_end] >= horizon_end_ns
        at_pair_end = contiguous_end == len(g) - 1

        # Funding-optimal direction uses only trailing, already-known 24h events.
        trailing = {}
        for ex in [entry.exchange_A, entry.exchange_B]:
            t, r = arrays[ex]; lo, hi = np.searchsorted(t, [entry_ns-24*3600*10**9, entry_ns+1]); trailing[ex] = float(r[lo:hi].sum())
        price_long = entry.exchange_B if entry.spread > 0 else entry.exchange_A
        price_short = entry.exchange_A if entry.spread > 0 else entry.exchange_B
        fund_long, fund_short = ((entry.exchange_A, entry.exchange_B) if trailing[entry.exchange_A] <= trailing[entry.exchange_B]
                                  else (entry.exchange_B, entry.exchange_A))
        directions = [("price_convergence", price_long, price_short), ("funding_optimal_trailing_24h", fund_long, fund_short)]

        for strategy, long_ex, short_ex in directions:
            long_is_a = long_ex == entry.exchange_A
            oriented = spreads[i:contiguous_end+1] if long_is_a else -spreads[i:contiguous_end+1]
            price_path = oriented - oriented[0]
            for rule in EXIT_RULES:
                target_hit = stop_hit = False
                exit_rel = None
                scheduled_minutes = {"hold_1h":60,"hold_4h":240,"hold_8h":480,"hold_24h":1440,"hold_72h":4320}.get(rule)
                if rule.startswith("target_"):
                    target = int(rule.split("_")[1].replace("bps", ""))
                    future = abs_spreads[i+1:contiguous_end+1]
                    if target == 0:
                        sign = 1 if spreads[i] >= 0 else -1
                        hits = np.flatnonzero(sign * spreads[i+1:contiguous_end+1] <= 0)
                    else:
                        hits = np.flatnonzero(future < target)
                    if len(hits): exit_rel = int(hits[0]) + 1; target_hit = True
                elif rule.startswith("stop_"):
                    loss = int(rule.split("_")[1].replace("bps", ""))
                    hits = np.flatnonzero(price_path[1:] <= -loss)
                    if len(hits): exit_rel = int(hits[0]) + 1; stop_hit = True
                elif rule == "next_krx_open":
                    nxt = _next_krx_open(entry.minute, dates)
                    if pd.notna(nxt):
                        idx = int(np.searchsorted(times[i:contiguous_end+1], pd.Timestamp(nxt).value))
                        if idx < contiguous_end-i+1: exit_rel = idx
                else:
                    target_ns = entry_ns + scheduled_minutes * 60 * 10**9
                    idx = int(np.searchsorted(times[i:contiguous_end+1], target_ns))
                    if idx < contiguous_end-i+1: exit_rel = idx

                if exit_rel is not None and exit_rel > 0:
                    if target_hit: exit_status = "TARGET_HIT"
                    elif stop_hit: exit_status = "STOP_HIT"
                    else: exit_status = "TIMEOUT"
                    reason = exit_status
                else:
                    exit_rel = contiguous_end - i
                    if gap_after:
                        exit_status = reason = "DATA_GAP"
                    elif reached_72h:
                        exit_status = reason = "TIMEOUT"
                    elif at_pair_end:
                        if pd.Timestamp(event.pair_common_end) >= pd.Timestamp(run_end).floor("min"):
                            exit_status = reason = "END_OF_DATA"
                        else:
                            exit_status = reason = "PAIR_WINDOW_END"
                    else:
                        exit_status = reason = "TIMEOUT"
                j = i + max(0, exit_rel)
                exitrow = g.iloc[j]
                path_pnl = price_path[:exit_rel+1]
                mae, mfe, mae_pos = excursion_metrics(path_pnl)
                sl, nlong = _sum_funding(arrays, long_ex, entry_ns, times[j])
                ss, nshort = _sum_funding(arrays, short_ex, entry_ns, times[j])
                funding_pnl = (-sl + ss) * 10000
                price_pnl = float(path_pnl[-1])
                leg_a = (pa[i:j+1]/pa[i]-1) * (1 if long_is_a else -1)
                leg_b = (pb[i:j+1]/pb[i]-1) * (-1 if long_is_a else 1)
                single_leg_loss = max(0.0, -float(min(np.nanmin(leg_a), np.nanmin(leg_b))) * 100)
                be = np.flatnonzero(path_pnl[1:] >= 0)
                scenario_id = _stable_id("SC", event.base_event_id, strategy, rule)
                rec = {
                    "event_id": scenario_id, "base_event_id": event.base_event_id, "scenario_id": scenario_id,
                    "pair": event.pair, "entry_threshold_bps": event.threshold_bps, "exit_rule": rule,
                    "strategy_direction": strategy, "entry_time": entry.minute, "exit_time": exitrow.minute,
                    "long_exchange": long_ex, "short_exchange": short_ex, "entry_spread_bps": spreads[i],
                    "exit_spread_bps": spreads[j], "price_convergence_pnl_bps": price_pnl,
                    "funding_pnl_bps": funding_pnl, "gross_combined_pnl_bps": price_pnl + funding_pnl,
                    "holding_minutes": int((times[j]-entry_ns)/60/10**9), "funding_event_count_long": nlong,
                    "funding_event_count_short": nshort, "max_adverse_excursion_bps": mae,
                    "max_favorable_excursion_bps": mfe,
                    "time_to_max_adverse_minutes": int((times[i+mae_pos]-entry_ns)/60/10**9),
                    "time_to_breakeven_minutes": int((times[i+1+be[0]]-entry_ns)/60/10**9) if len(be) else np.nan,
                    "max_single_leg_loss_percent": single_leg_loss,
                    "converged_before_next_krx_open": bool(pd.notna(event.minutes_to_below_10bps) and event.minutes_to_below_10bps <= max(0, (_next_krx_open(entry.minute,dates)-entry.minute).total_seconds()/60)) if pd.notna(_next_krx_open(entry.minute,dates)) else False,
                    "session": detailed_session(entry.minute, dates), "regime": gate_regime(entry.minute),
                    "comparison_quality": entry.comparison_quality,
                    "exit_status": exit_status, "target_hit": target_hit, "stop_hit": stop_hit,
                    "is_censored": exit_status in {"END_OF_DATA","DATA_GAP","PAIR_WINDOW_END"},
                    "observation_end_reason": reason,
                    "warnings": "historical_proxy_backtest;not_executable_BBO",
                }
                for cost in COSTS: rec[f"net_after_{cost}bps"] = rec["gross_combined_pnl_bps"] - cost
                rows.append(rec)
    out = pd.DataFrame(rows)
    if len(out):
        out = out.sort_values(["pair","entry_threshold_bps","entry_time","strategy_direction","exit_rule"]).drop_duplicates("scenario_id")
    return out.reset_index(drop=True)


def strategy_summary(scenarios):
    keys = ["pair", "strategy_direction", "entry_threshold_bps", "exit_rule"]
    rows = []
    for key, g in scenarios.groupby(keys):
        rec = dict(zip(keys, key))
        rec.update({"scenario_count":len(g), "base_event_count":g.base_event_id.nunique(),
                    "target_hit_count":int(g.target_hit.sum()), "stop_hit_count":int(g.stop_hit.sum()),
                    "timeout_count":int((g.exit_status=="TIMEOUT").sum()), "censored_count":int(g.is_censored.sum()),
                    "median_gross_bps":g.gross_combined_pnl_bps.median(), "max_drawdown_proxy_bps":g.max_adverse_excursion_bps.max()})
        for cost in COSTS:
            v=g[f"net_after_{cost}bps"]
            rec[f"positive_event_count_{cost}bps"]=int((v>0).sum())
            rec[f"positive_event_ratio_{cost}bps"]=float((v>0).mean())
            rec[f"median_net_{cost}bps"]=v.median();rec[f"p05_net_{cost}bps"]=v.quantile(.05);rec[f"p95_net_{cost}bps"]=v.quantile(.95)
        rows.append(rec)
    return pd.DataFrame(rows)


def duration_bucket_counts(base_events):
    e=base_events[base_events.threshold_bps==20].copy()
    bins=[0,1,2,5,10,15,30,60,120,240,np.inf]
    labels=["1","2","3–5","6–10","11–15","16–30","31–60","61–120","121–240",">240m"]
    e["duration_bucket"]=pd.cut(e.duration_observed_minutes,bins=bins,labels=labels,include_lowest=True,right=True)
    return e.groupby(["duration_bucket","status"],observed=False).size().unstack(fill_value=0).reindex(labels)


def duration_charts(base_events):
    C.mkdir(parents=True, exist_ok=True)
    for obsolete in ["spread_event_duration.png", "event_duration_ecdf.png", "event_duration_log_scale.png"]:
        (C/obsolete).unlink(missing_ok=True)
    sns.set_theme(style="whitegrid", font="WenQuanYi Zen Hei", rc={"axes.unicode_minus":False})
    e=base_events[base_events.threshold_bps==20].copy();completed=e[e.status=="COMPLETED"]
    censored=e[e.status!="COMPLETED"]

    labels=["1","2","3–5","6–10","11–15","16–30","31–60","61–120","121–240",">240m"]
    counts=duration_bucket_counts(e)
    plt.figure(figsize=(12,6));bottom=np.zeros(len(counts))
    colors={"COMPLETED":"#4472C4","RIGHT_CENSORED":"#ED7D31","DATA_GAP_CENSORED":"#A5A5A5"}
    for status in ["COMPLETED","RIGHT_CENSORED","DATA_GAP_CENSORED"]:
        v=counts.get(status,pd.Series(0,index=counts.index)).to_numpy();plt.bar(labels,v,bottom=bottom,label=status,color=colors[status]);bottom+=v
    plt.legend();plt.xlabel("已观测持续时间（分钟）");plt.ylabel("唯一事件数")
    plt.title(f"绝对价差超过 20 bps 的事件持续时间（0–240分钟）\n唯一基础事件；历史分钟价格代理；不含策略场景重复 | n={len(e)}, completed={len(completed)}, censored={len(censored)}")
    plt.tight_layout();plt.savefig(C/"event_duration_20bps_0_240m.png",dpi=160,bbox_inches="tight");plt.close()

    logbins=np.array([1,2,5,10,20,30,60,120,240,480,720,1440,2880,5760,10000],float)
    plt.figure(figsize=(11,6));plt.hist(completed.duration_observed_minutes,bins=logbins,alpha=.72,label=f"COMPLETED n={len(completed)}",edgecolor="white")
    if len(censored):plt.hist(censored.duration_observed_minutes,bins=logbins,alpha=.35,label=f"CENSORED n={len(censored)}",edgecolor="black",hatch="//")
    plt.xscale("log");plt.xlabel("已观测持续分钟（log scale）");plt.ylabel("唯一事件数");plt.legend()
    longest=e.loc[e.duration_observed_minutes.idxmax()];plt.title(f"20 bps 基础事件持续时间全范围（log）\n最长 {longest.duration_observed_minutes:.0f} 分钟：{longest.pair}")
    plt.tight_layout();plt.savefig(C/"event_duration_20bps_log_scale.png",dpi=160,bbox_inches="tight");plt.close()

    plt.figure(figsize=(11,6))
    groups=[("ALL completed",completed), ("Gate regime completed",completed[completed.start_regime=="GATE_REGIME_20260716_19"]),
            ("Post-Gate completed",completed[completed.start_regime=="POST_GATE_REGIME"]),
            ("Non-Gate pairs completed",completed[~completed.pair.str.contains("gate")])]
    for label,g in groups:
        if len(g):sns.ecdfplot(g,x="duration_observed_minutes",label=f"{label} n={len(g)}")
    for x in HORIZONS:plt.axvline(x,color="grey",lw=.6,ls="--")
    plt.xscale("log");plt.xlabel("完整事件持续分钟（log scale）");plt.ylabel("累计事件比例");plt.legend()
    plt.title("已完成20 bps基础事件的持续时间 ECDF\n删失事件不进入曲线；仅为完整案例分布")
    plt.tight_layout();plt.savefig(C/"event_duration_20bps_ecdf.png",dpi=160,bbox_inches="tight");plt.close()

    order=completed.groupby("pair").duration_observed_minutes.median().sort_values().index.tolist()
    plt.figure(figsize=(12,7));palette={p:("#ED7D31" if "gate" in p else "#4472C4") for p in order}
    if len(completed):sns.boxplot(completed,y="pair",x="duration_observed_minutes",order=order,palette=palette,hue="pair",legend=False,showfliers=False)
    plt.xscale("log");plt.xlabel("完整事件持续分钟（log scale）");plt.ylabel("pair")
    ticks=[]
    for p in order:
        g=completed[completed.pair==p];ticks.append(f"{p} (n={len(g)}, med={g.duration_observed_minutes.median():.0f}, P95={g.duration_observed_minutes.quantile(.95):.0f})")
    plt.yticks(range(len(order)),ticks);plt.title("20 bps已完成基础事件：按 pair 的持续时间分布")
    plt.tight_layout();plt.savefig(C/"event_duration_20bps_by_pair.png",dpi=160,bbox_inches="tight");plt.close()

    gate=e[e.pair.str.contains("gate")];regs=["PRE_GATE_REGIME","GATE_REGIME_20260716_19","POST_GATE_REGIME"]
    fig,(ax1,ax2)=plt.subplots(1,2,figsize=(13,5))
    metric=[]
    for reg in regs:
        g=gate[gate.start_regime==reg]
        if len(g):metric.append({"regime":reg,"median":g.duration_observed_minutes.median(),"P90":g.duration_observed_minutes.quantile(.9),"P95":g.duration_observed_minutes.quantile(.95),"n":len(g),"censor":(g.status!="COMPLETED").mean()*100})
    m=pd.DataFrame(metric)
    if len(m):m.set_index("regime")[["median","P90","P95"]].reindex(regs).plot.bar(ax=ax1);ax2.bar(m.regime,m.n,color="#4472C4");ax2b=ax2.twinx();ax2b.plot(m.regime,m.censor,color="#C00000",marker="o",label="censor %")
    if "PRE_GATE_REGIME" not in set(m.regime if len(m) else []):ax1.text(.03,.93,"PRE_GATE_REGIME: NO 1M DATA",transform=ax1.transAxes,color="#C00000",weight="bold")
    ax1.set_ylabel("分钟");ax1.set_title("中位数/P90/P95");ax2.set_ylabel("唯一事件数");ax2.set_title("事件数与删失率");ax1.tick_params(axis="x",rotation=25);ax2.tick_params(axis="x",rotation=25)
    fig.suptitle("Gate相关20 bps基础事件：regime前/中/后持续时间");fig.tight_layout();fig.savefig(C/"event_duration_20bps_gate_regime_comparison.png",dpi=160,bbox_inches="tight");plt.close(fig)


def _build_15m_pairdf(prices_1m, dates):
    frames=[]
    # Main venues and local portions are downsampled to genuine 15-minute buckets.
    for ex,g in prices_1m.groupby("exchange"):
        mark=g[g.price_type=="mark"]
        trade=g[g.price_type=="trade"]
        src=mark if len(mark) else trade
        if not len(src):continue
        q=src.sort_values("open_time").set_index("open_time").close.resample("15min").last().dropna().reset_index()
        q["exchange"]=ex;q["quality"]="downsampled_mark_15m" if len(mark) else "downsampled_trade_15m"
        frames.append(q.rename(columns={"open_time":"minute","close":"price"}))
    # Official 15m audit bars replace/downstream extend Gate and Hyperliquid.
    for ex in ["gate","hyperliquid"]:
        path=ROOT/"data"/"normalized"/f"{ex}_history_15m_audit.parquet"
        if path.exists():
            q=pd.read_parquet(path)[["open_time","close"]].rename(columns={"open_time":"minute","close":"price"})
            q["exchange"]=ex;q["quality"]="official_15m_audit"
            frames.append(q)
    allp=pd.concat(frames,ignore_index=True).sort_values(["exchange","minute"])
    allp["priority"]=allp.quality.eq("official_15m_audit").astype(int)
    allp=allp.sort_values(["exchange","minute","priority"]).drop_duplicates(["exchange","minute"],keep="last")
    pairs=[]
    for a,b in itertools.combinations(sorted(allp.exchange.unique()),2):
        aa=allp[allp.exchange==a][["minute","price","quality"]].rename(columns={"price":"price_A","quality":"quality_A"})
        bb=allp[allp.exchange==b][["minute","price","quality"]].rename(columns={"price":"price_B","quality":"quality_B"})
        z=aa.merge(bb,on="minute",how="inner").dropna()
        mismatch,_=detect_scale_mismatch(z.price_A,z.price_B)
        if mismatch or z.empty:continue
        z["exchange_A"],z["exchange_B"],z["pair"]=a,b,f"{a}/{b}"
        z["spread"]=symmetric_spread_bps(z.price_A,z.price_B);z["abs_spread"]=z.spread.abs()
        z["comparison_quality"]="15m_robustness_proxy"
        pairs.append(z)
    return pd.concat(pairs,ignore_index=True) if pairs else pd.DataFrame()


def robustness_15m(prices_1m, one_minute_events, requested_start, run_end):
    R15.mkdir(exist_ok=True);C15.mkdir(exist_ok=True)
    dates,_=trading_dates(requested_start,run_end)
    pair15=_build_15m_pairdf(prices_1m,dates)
    base15=build_base_events(pair15,dates,frequency_minutes=15)
    base15.to_csv(R15/"base_spread_events_15m.csv",index=False)
    summary15,_=duration_summary(base15,R15,"_15m")
    e=base15[base15.threshold_bps==20];comp=e[e.status=="COMPLETED"]
    plt.figure(figsize=(10,5))
    for label,g in [("ALL",comp),("Gate pairs",comp[comp.pair.str.contains("gate")]),("Hyperliquid pairs",comp[comp.pair.str.contains("hyperliquid")])]:
        if len(g):sns.ecdfplot(g,x="duration_observed_minutes",label=f"{label} n={len(g)}")
    plt.xscale("log");plt.xlabel("持续分钟（15m桶，log）");plt.ylabel("累计比例");plt.legend();plt.title("15m robustness analysis：已完成20 bps事件ECDF")
    plt.tight_layout();plt.savefig(C15/"event_duration_20bps_ecdf_15m.png",dpi=160,bbox_inches="tight");plt.close()
    gate=e[e.pair.str.contains("gate")];plt.figure(figsize=(10,5))
    if len(gate):sns.boxplot(gate,x="start_regime",y="duration_observed_minutes",showfliers=False)
    plt.yscale("log");plt.xticks(rotation=20);plt.ylabel("已观测分钟（15m桶，log）");plt.title("15m Gate事件持续时间：regime前/中/后")
    plt.tight_layout();plt.savefig(C15/"gate_regime_duration_15m.png",dpi=160,bbox_inches="tight");plt.close()

    one=one_minute_events[one_minute_events.threshold_bps==20]
    gate_pre=gate[gate.start_regime=="PRE_GATE_REGIME"]
    hyper_pre=e[e.pair.str.contains("hyperliquid") & (e.event_start<pd.Timestamp("2026-07-19T16:05Z"))]
    allrow=summary15[(summary15.group_type=="ALL")&(summary15.group_value=="ALL_OBSERVED")].iloc[0] if len(summary15) else None
    onecomp=one[one.status=="COMPLETED"]
    summary=f"""# SKHYNIX 15分钟长期稳健性分析

**标记：15m robustness analysis。** Gate/Hyperliquid 使用官方15m审计K线，其他交易所由1m数据向下聚合；没有将15m上采样或伪装成1m。持续时间按真实15分钟桶计算。

- 15m、20 bps唯一基础事件：{len(e)}；完成 {int((e.status=='COMPLETED').sum())}，右删失 {int((e.status=='RIGHT_CENSORED').sum())}，数据缺口删失 {int((e.status=='DATA_GAP_CENSORED').sum())}。
- 15m全样本已观测持续时间中位数/P95：{allrow.median_minutes if allrow is not None else np.nan:.1f}/{allrow.p95_minutes if allrow is not None else np.nan:.1f} 分钟；1m局部样本完成事件中位数/P95：{onecomp.duration_observed_minutes.median():.1f}/{onecomp.duration_observed_minutes.quantile(.95):.1f} 分钟。
- Gate在2026-07-16前的20 bps事件：{len(gate_pre)} 个，其中持续超过1天 {int((gate_pre.duration_observed_minutes>1440).sum())} 个，最长 {gate_pre.duration_observed_minutes.max() if len(gate_pre) else np.nan} 分钟。
- Hyperliquid在2026-07-19 16:05前的20 bps事件：{len(hyper_pre)} 个，其中持续超过1天 {int((hyper_pre.duration_observed_minutes>1440).sum())} 个，最长 {hyper_pre.duration_observed_minutes.max() if len(hyper_pre) else np.nan} 分钟。

## 判断

15m长期样本用于判断长尾和结构性基差是否早于1m留存窗口存在；它不提供分钟级可执行路径。历史K线仍非BBO，且Gate/Hyperliquid 15m使用成交代理时，不能确认当时盘口可交易性。
"""
    (R15/"EXECUTIVE_SUMMARY_15m.md").write_text(summary)
    return base15,summary15,summary


def duration_report_block():
    base=pd.read_csv(R/"base_spread_events.csv",parse_dates=["event_start","event_end","peak_time"])
    summary=pd.read_csv(R/"event_duration_summary_20bps.csv")
    e=base[base.threshold_bps==20];allrow=summary[(summary.group_type=="ALL")&(summary.group_value=="ALL_OBSERVED")].iloc[0]
    completed_row=summary[(summary.group_type=="STATUS_SCOPE")&(summary.group_value=="COMPLETED_ONLY")].iloc[0]
    longest=e.loc[e.duration_observed_minutes.idxmax()]
    gate=e[e.pair.str.contains("gate")]
    during=gate[gate.start_regime=="GATE_REGIME_20260716_19"];post=gate[gate.start_regime=="POST_GATE_REGIME"]
    nogate=e[(~e.pair.str.contains("gate"))|(e.start_regime!="GATE_REGIME_20260716_19")]
    old_count=0
    legacy=R/"spread_events_legacy_before_duration_fix.csv"
    if legacy.exists():
        old=pd.read_csv(legacy);old_count=len(old[old.threshold_bps==20])
    return f"""

## 唯一基础价差事件与持续时间修正版

### 强结论

- 20 bps唯一 `base_event` 共 **{len(e)}** 个：完成 {int((e.status=='COMPLETED').sum())}，右删失 {int((e.status=='RIGHT_CENSORED').sum())}，数据缺口删失 {int((e.status=='DATA_GAP_CENSORED').sum())}。修正前20 bps市场事件也是 {old_count} 个（数量差 {len(e)-old_count:+d}），但旧表没有删失状态和稳定ID；策略方向、退出规则和成本不再进入持续时间计数。
- 已观测持续时间中位数/P75/P90/P95/P99：**{allrow.median_minutes:.1f}/{allrow.p75_minutes:.1f}/{allrow.p90_minutes:.1f}/{allrow.p95_minutes:.1f}/{allrow.p99_minutes:.1f} 分钟**。
- 仅已完成事件在5/15/30/60/240分钟内结束的比例：{completed_row.ratio_le_5m:.2%}/{completed_row.ratio_le_15m:.2%}/{completed_row.ratio_le_30m:.2%}/{completed_row.ratio_le_60m:.2%}/{completed_row.ratio_le_240m:.2%}；全部已观测事件中超过1天 {int(allrow.count_gt_1440m)} 个。
- 最长事件：{longest.pair}，`[{longest.event_start}, {longest.event_end}]`，已观测 {longest.duration_observed_minutes} 分钟，状态 {longest.status}，开始regime={longest.start_regime}；删失事件不称为完整真实持续时间。
- Gate regime相关事件中位数/P95：{during.duration_observed_minutes.median() if len(during) else np.nan:.1f}/{during.duration_observed_minutes.quantile(.95) if len(during) else np.nan:.1f} 分钟；7月20日后：{post.duration_observed_minutes.median() if len(post) else np.nan:.1f}/{post.duration_observed_minutes.quantile(.95) if len(post) else np.nan:.1f} 分钟。

### 初步结论

- 排除Gate regime后，P95={nogate.duration_observed_minutes.quantile(.95):.1f}、P99={nogate.duration_observed_minutes.quantile(.99):.1f}、最长已观测={nogate.duration_observed_minutes.max():.0f}分钟，说明长尾是否仍存在应以这些数字判断，而不是由旧全范围直方图目测。
- 数日级、完成后仍长期高于20 bps的事件更接近结构性基差候选；详见 `longest_spread_events_20bps.csv`。是否可交易仍无法仅凭分钟mark/trade确认。
- 收敛率现在使用真实目标命中；`observed_rate_complete_cases` 仅基于完整可观察事件，不是Kaplan–Meier或无偏总体概率。

### 无法确认

所有持续时间均来自历史mark/trade分钟代理，不是BBO；数据缺口删失、右删失、盘口深度和真实成交状态仍限制套利解释。
"""


def run_duration_analysis(pairdf, funding, prices, requested_start, run_end):
    dates,_=trading_dates(requested_start,run_end)
    base=build_base_events(pairdf,dates)
    base.to_csv(R/"base_spread_events.csv",index=False)
    # Preserve the old compatibility output once for before/after auditing.
    old=R/"spread_events.csv";legacy=R/"spread_events_legacy_before_duration_fix.csv"
    if old.exists() and not legacy.exists():legacy.write_bytes(old.read_bytes())
    base.to_csv(old,index=False)
    ds,longest=duration_summary(base)
    conv=convergence_summary(base)
    scenarios=build_strategy_scenarios(pairdf,funding,base,dates,run_end)
    scenarios.to_csv(R/"joint_strategy_events.csv",index=False)
    ss=strategy_summary(scenarios);ss.to_csv(R/"joint_strategy_summary.csv",index=False)
    duration_charts(base)
    base15,summary15,text15=robustness_15m(prices,base,requested_start,run_end)
    return {"base_events":base,"duration_summary":ds,"convergence":conv,"scenarios":scenarios,
            "strategy_summary":ss,"base_events_15m":base15,"duration_summary_15m":summary15}
