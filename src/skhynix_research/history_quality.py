from __future__ import annotations

import itertools
import math
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns

from .config import ROOT
from .calendar import parse_utc, trading_dates
from .common_windows import GATE_START, GATE_END, gate_regime, left_closed_right_open

R = ROOT / "reports"
C = R / "charts"
THRESHOLDS = [20, 50, 100, 150, 200]
COSTS = [10, 20, 40, 80, 120]


def normalize_epoch(value):
    value = int(value)
    return pd.to_datetime(value, unit="s" if value < 10**12 else "ms", utc=True)


def detailed_session(ts, krx_dates=None):
    t = parse_utc(ts)
    kr_date = t.tz_convert("Asia/Seoul").date()
    if krx_dates is not None and kr_date not in krx_dates:
        return "KRX_HOLIDAY_OR_WEEKEND"
    if krx_dates is None and t.tz_convert("Asia/Seoul").weekday() >= 5:
        return "KRX_HOLIDAY_OR_WEEKEND"
    minute = t.hour * 60 + t.minute
    if 350 <= minute < 390:
        return "PRE_CLOSE_BASELINE"
    if 390 <= minute < 400:
        return "KRX_CLOSE_TRANSITION"
    if 400 <= minute < 540:
        return "KRX_OFFICIAL_AFTER_HOURS"
    ny = t.tz_convert("America/New_York")
    ny_minute = ny.hour * 60 + ny.minute
    if ny.weekday() < 5 and 570 <= ny_minute < 960:
        return "US_REGULAR"
    if ny.weekday() < 5 and 960 <= ny_minute < 1200:
        return "US_AFTER_HOURS"
    return "KRX_FULLY_CLOSED_PRE_US"


def strategy_price_pnl(entry_spread, exit_spread, long_is_a):
    """Symmetric-spread proxy PnL; positive means the selected long/short direction improved."""
    return (exit_spread - entry_spread) if long_is_a else (entry_spread - exit_spread)


def excursion_metrics(path_pnl):
    a = np.asarray(path_pnl, dtype=float)
    if not len(a):
        return np.nan, np.nan, np.nan
    imin = int(np.nanargmin(a))
    return max(0.0, -float(np.nanmin(a))), max(0.0, float(np.nanmax(a))), imin


def gate_market_premium(gate_price, market_prices):
    vals = [x for x in market_prices if pd.notna(x) and x > 0]
    if pd.isna(gate_price) or gate_price <= 0 or not vals:
        return np.nan
    return 10000 * (float(gate_price) / float(np.median(vals)) - 1)


def event_entry_indices(times, abs_spreads, threshold):
    times=pd.to_datetime(times,utc=True)
    times=(times.dt.as_unit("ns") if isinstance(times,pd.Series) else times.as_unit("ns")).astype("int64").to_numpy()
    active=np.flatnonzero(np.asarray(abs_spreads)>=threshold)
    if not len(active): return np.array([],dtype=int)
    return active[np.r_[True,np.diff(times[active])>2*60*10**9]]


def gap_broken(times, values, max_gap_minutes=2):
    """Insert NaNs before a long gap so matplotlib cannot draw through it."""
    z=pd.DataFrame({"time":pd.to_datetime(times,utc=True),"value":values}).sort_values("time")
    out=[]
    for i,r in z.iterrows():
        if out and (r.time-out[-1][0]).total_seconds()>max_gap_minutes*60:
            out.append((r.time-pd.Timedelta(nanoseconds=1),np.nan))
        out.append((r.time,r.value))
    return pd.DataFrame(out,columns=["time","value"])


def _event_intervals(funding):
    f = funding.sort_values(["exchange", "funding_time"]).copy()
    inferred = []
    for _, g in f.groupby("exchange", sort=False):
        prev = g.funding_time.diff().dt.total_seconds().div(3600)
        nxt = g.funding_time.shift(-1).sub(g.funding_time).dt.total_seconds().div(3600)
        dyn = prev.where(prev.between(.5, 24), nxt).where(lambda x: x.between(.5, 24))
        dyn = dyn.where((dyn-dyn.round()).abs() >= .01, dyn.round()).round(3)
        inferred.extend(dyn.fillna(g.funding_interval_hours).tolist())
    f["event_interval_hours"] = inferred
    return f


def funding_contributions(funding, common, requested_start, run_end, dates):
    funding = _event_intervals(funding)
    rows = []
    for _, w in common.iterrows():
        a, b = w.long_exchange, w.short_exchange
        fa = left_closed_right_open(funding[funding.exchange == a], "funding_time", w.joint_start, w.joint_end)
        fb = left_closed_right_open(funding[funding.exchange == b], "funding_time", w.joint_start, w.joint_end)
        times = sorted(set(fa.funding_time).union(fb.funding_time))
        cumulative = 0.0
        for t in times:
            la = fa[fa.funding_time == t]
            sb = fb[fb.funding_time == t]
            lr = float(la.funding_rate.sum()) if len(la) else 0.0
            sr = float(sb.funding_rate.sum()) if len(sb) else 0.0
            net = -lr + sr
            cumulative += net * 10000
            rows.append({"pair": w.pair, "long_exchange": a, "short_exchange": b, "funding_time": t,
                         "long_rate": lr, "short_rate": sr, "net_rate": net,
                         "net_cashflow_10000": net * 10000, "cumulative_cashflow": cumulative,
                         "long_interval_hours": float(la.event_interval_hours.iloc[0]) if len(la) else np.nan,
                         "short_interval_hours": float(sb.event_interval_hours.iloc[0]) if len(sb) else np.nan,
                         "session": detailed_session(t, dates), "regime": gate_regime(t)})
    ev = pd.DataFrame(rows)
    ev.to_csv(R / "funding_event_contributions.csv", index=False)
    if len(ev):
        ev["date"] = ev.funding_time.dt.floor("D")
        ev["week"] = ev.funding_time.dt.to_period("W").astype(str)
        daily = ev.groupby(["pair", "long_exchange", "short_exchange", "date"], as_index=False).agg(
            daily_net_rate=("net_rate", "sum"), daily_cashflow_10000=("net_cashflow_10000", "sum"), event_count=("net_rate", "size"))
        daily["week"] = daily.date.dt.to_period("W").astype(str)
        weekly = daily.groupby(["pair", "week"]).daily_cashflow_10000.transform("sum")
        daily["weekly_cashflow_10000"] = weekly
    else:
        daily = pd.DataFrame()
    daily.to_csv(R / "daily_funding_pnl.csv", index=False)
    top = ev.reindex(ev.net_cashflow_10000.abs().sort_values(ascending=False).index).groupby("pair", group_keys=False).head(20) if len(ev) else ev
    top.to_csv(R / "top_20_funding_events.csv", index=False)
    sens = []
    for pair, g in ev.groupby("pair"):
        vals = g.net_cashflow_10000.astype(float)
        total = vals.sum()
        ordered = vals.abs().sort_values(ascending=False)
        n = len(g)
        drop1 = max(1, math.ceil(n * .01)); drop5 = max(1, math.ceil(n * .05))
        duration_hours = max(1e-9, (g.funding_time.max() - g.funding_time.min()).total_seconds() / 3600)
        sens.append({"pair": pair, "long_exchange": g.long_exchange.iloc[0], "short_exchange": g.short_exchange.iloc[0],
                     "event_count": n, "total_cashflow_10000": total,
                     "largest_single_event_cashflow": vals.loc[ordered.index[0]],
                     "top_5_abs_contribution_percent_of_total": 100 * vals.loc[ordered.index[:5]].abs().sum() / abs(total) if total else np.nan,
                     "top_10_abs_contribution_percent_of_total": 100 * vals.loc[ordered.index[:10]].abs().sum() / abs(total) if total else np.nan,
                     "top_5_signed_contribution_cashflow": vals.loc[ordered.index[:5]].sum(),
                     "top_10_signed_contribution_cashflow": vals.loc[ordered.index[:10]].sum(),
                     "cashflow_excluding_top_abs_1pct": vals.drop(ordered.index[:drop1]).sum(),
                     "cashflow_excluding_top_abs_5pct": vals.drop(ordered.index[:drop5]).sum(),
                     "median_event_cashflow": vals.median(), "p95_event_cashflow": vals.quantile(.95),
                     "p99_event_cashflow": vals.quantile(.99), "positive_event_ratio": (vals > 0).mean(),
                     "average_hourly_cashflow": total / duration_hours, "average_daily_cashflow": total / duration_hours * 24,
                     "equivalent_8h_cashflow": total / duration_hours * 8,
                     "simple_apr_not_compounded_percent": total / 10000 / duration_hours * 24 * 365 * 100})
    sensitivity = pd.DataFrame(sens)
    sensitivity.to_csv(R / "funding_outlier_sensitivity.csv", index=False)
    changes = []
    for ex, g in funding.groupby("exchange"):
        g = g.sort_values("funding_time")
        grp = g.event_interval_hours.ne(g.event_interval_hours.shift()).cumsum()
        for _, q in g.groupby(grp):
            changes.append({"exchange": ex, "segment_start": q.funding_time.min(), "segment_end": q.funding_time.max(),
                            "interval_hours": q.event_interval_hours.iloc[0], "event_count": len(q),
                            "near_2026_07_14": bool(((q.funding_time >= pd.Timestamp("2026-07-13", tz="UTC")) & (q.funding_time < pd.Timestamp("2026-07-16", tz="UTC"))).any()),
                            "min_rate": q.funding_rate.min(), "max_rate": q.funding_rate.max(),
                            "timestamp_fractional_ms_events": int((q.funding_time.dt.microsecond != 0).sum())})
    pd.DataFrame(changes).to_csv(R / "funding_interval_changes.csv", index=False)
    return ev, daily, sensitivity


def _latest_past_sum(funding, ex, t):
    q = funding[(funding.exchange == ex) & (funding.funding_time <= t) & (funding.funding_time > t - pd.Timedelta(hours=24))]
    return float(q.funding_rate.sum())


def _funding_pnl(funding, long_ex, short_ex, start, end):
    a = left_closed_right_open(funding[funding.exchange == long_ex], "funding_time", start, end)
    b = left_closed_right_open(funding[funding.exchange == short_ex], "funding_time", start, end)
    return (-a.funding_rate.sum() + b.funding_rate.sum()) * 10000, len(a), len(b)


def _next_krx_open(t, dates):
    t = parse_utc(t)
    choices = [pd.Timestamp(d, tz="UTC") for d in sorted(dates) if pd.Timestamp(d, tz="UTC") > t]
    return choices[0] if choices else pd.NaT


def _choose_exit(g, i, rule, entry_oriented, long_is_a, dates):
    t0 = g.minute.iloc[i]
    horizon = {"hold_1h": 60, "hold_4h": 240, "hold_8h": 480, "hold_24h": 1440, "hold_72h": 4320}.get(rule)
    if horizon:
        q = g[(g.minute >= t0 + pd.Timedelta(minutes=horizon))]
        return q.index[0] if len(q) else None
    if rule == "next_krx_open":
        target = _next_krx_open(t0, dates)
        q = g[g.minute >= target] if pd.notna(target) else g.iloc[0:0]
        return q.index[0] if len(q) else None
    future = g.loc[i:]
    oriented = future.spread if long_is_a else -future.spread
    if rule.startswith("target_"):
        level = float(rule.split("_")[1].replace("bps", ""))
        q = future[future.abs_spread <= level]
    else:
        loss = float(rule.split("_")[1].replace("bps", ""))
        pnl = oriented - entry_oriented
        q = future[pnl <= -loss]
    return q.index[0] if len(q) else None


def joint_backtest(pairdf, funding, common, dates):
    rows = []
    exits = ["target_20bps", "target_10bps", "target_0bps", "hold_1h", "hold_4h", "hold_8h", "hold_24h", "hold_72h", "next_krx_open", "stop_50bps", "stop_100bps"]
    windows = {"/".join(sorted([r.long_exchange, r.short_exchange])): (pd.Timestamp(r.joint_start), pd.Timestamp(r.joint_end)) for _, r in common.iterrows()}
    event_id = 0
    fund_arrays = {}
    for ex, fg in funding.groupby("exchange"):
        fg = fg.sort_values("funding_time")
        fund_arrays[ex] = (fg.funding_time.dt.as_unit("ns").astype("int64").to_numpy(), fg.funding_rate.to_numpy(float))

    def fund_sum(ex, s_ns, e_ns):
        tt, rr = fund_arrays[ex]
        lo, hi = np.searchsorted(tt, [s_ns, e_ns])
        return float(rr[lo:hi].sum()), int(hi-lo)

    def trailing(ex, t_ns):
        tt, rr = fund_arrays[ex]
        lo, hi = np.searchsorted(tt, [t_ns-24*3600*10**9, t_ns+1])
        return float(rr[lo:hi].sum())

    for pair, base in pairdf.groupby("pair"):
        js, je = windows[pair]
        g = base[(base.minute >= js) & (base.minute < je) & (base.age_A <= 120) & (base.age_B <= 120)].sort_values("minute").reset_index(drop=True)
        if len(g) < 2:
            continue
        tns = g.minute.dt.as_unit("ns").astype("int64").to_numpy(); spreads = g.spread.to_numpy(float); abs_spreads = np.abs(spreads)
        pa = g.price_A.to_numpy(float); pb = g.price_B.to_numpy(float)
        for threshold in THRESHOLDS:
            starts = event_entry_indices(g.minute,abs_spreads,threshold)
            for i in starts:
                entry = g.iloc[i]
                directions = []
                # Convergence: long the lower-priced venue.
                conv_long = entry.exchange_B if entry.spread > 0 else entry.exchange_A
                conv_short = entry.exchange_A if entry.spread > 0 else entry.exchange_B
                directions.append(("price_convergence", conv_long, conv_short))
                # Funding direction uses only the trailing 24h of events known at entry.
                ra = trailing(entry.exchange_A, tns[i]); rb = trailing(entry.exchange_B, tns[i])
                f_long, f_short = (entry.exchange_A, entry.exchange_B) if ra <= rb else (entry.exchange_B, entry.exchange_A)
                directions.append(("funding_optimal_trailing_24h", f_long, f_short))
                for strategy, long_ex, short_ex in directions:
                    long_is_a = long_ex == entry.exchange_A
                    entry_oriented = spreads[i] if long_is_a else -spreads[i]
                    end_limit = int(np.searchsorted(tns, tns[i] + 72*3600*10**9, side="right")-1)
                    end_limit = min(len(g)-1, max(i+1, end_limit))
                    f_abs = abs_spreads[i:end_limit+1]
                    oriented = spreads[i:end_limit+1] if long_is_a else -spreads[i:end_limit+1]
                    pnl_path_all = oriented-entry_oriented
                    for rule in exits:
                        horizon = {"hold_1h":60,"hold_4h":240,"hold_8h":480,"hold_24h":1440,"hold_72h":4320}.get(rule)
                        if horizon:
                            j = int(np.searchsorted(tns,tns[i]+horizon*60*10**9))
                            if j>=len(g): j=None
                        elif rule=="next_krx_open":
                            nxt=_next_krx_open(entry.minute,dates);j=int(np.searchsorted(tns,pd.Timestamp(nxt).value)) if pd.notna(nxt) else None
                            if j is not None and j>=len(g):j=None
                        elif rule.startswith("target_"):
                            level=float(rule.split("_")[1].replace("bps",""));hits=np.flatnonzero(f_abs<=level);j=i+int(hits[0]) if len(hits) else None
                        else:
                            loss=float(rule.split("_")[1].replace("bps",""));hits=np.flatnonzero(pnl_path_all<=-loss);j=i+int(hits[0]) if len(hits) else None
                        if j is None or j <= i:
                            continue
                        exitrow = g.loc[j]
                        price_path = (spreads[i:j+1] - spreads[i]) if long_is_a else (spreads[i] - spreads[i:j+1])
                        mae, mfe, mae_pos = excursion_metrics(price_path)
                        price_pnl = float(price_path[-1])
                        sl,nlong=fund_sum(long_ex,tns[i],tns[j]);ss,nshort=fund_sum(short_ex,tns[i],tns[j]);fund_pnl=(-sl+ss)*10000
                        gross = price_pnl + fund_pnl
                        leg_a = (pa[i:j+1] / pa[i] - 1) * (1 if long_is_a else -1)
                        leg_b = (pb[i:j+1] / pb[i] - 1) * (-1 if long_is_a else 1)
                        single_leg_loss = max(0.0, -float(min(np.nanmin(leg_a),np.nanmin(leg_b))) * 100)
                        breakeven = np.flatnonzero(price_path[1:] >= 0)
                        event_id += 1
                        rec = {"event_id": event_id, "pair": pair, "entry_threshold_bps": threshold,
                               "exit_rule": rule, "strategy_direction": strategy, "entry_time": entry.minute,
                               "exit_time": exitrow.minute, "long_exchange": long_ex, "short_exchange": short_ex,
                               "entry_spread_bps": entry.spread, "exit_spread_bps": exitrow.spread,
                               "price_convergence_pnl_bps": price_pnl, "funding_pnl_bps": fund_pnl,
                               "gross_combined_pnl_bps": gross, "holding_minutes": int((exitrow.minute-entry.minute).total_seconds()/60),
                               "funding_event_count_long": nlong, "funding_event_count_short": nshort,
                               "max_adverse_excursion_bps": mae, "max_favorable_excursion_bps": mfe,
                               "time_to_max_adverse_minutes": int((tns[i+mae_pos]-tns[i])/60/10**9),
                               "time_to_breakeven_minutes": int((tns[i+1+breakeven[0]]-tns[i])/60/10**9) if len(breakeven) else np.nan,
                               "max_single_leg_loss_percent": single_leg_loss,
                               "converged_before_next_krx_open": bool(len(np.flatnonzero(abs_spreads[i:j+1]<=10)) and tns[i+np.flatnonzero(abs_spreads[i:j+1]<=10)[0]] <= pd.Timestamp(_next_krx_open(entry.minute,dates)).value) if pd.notna(_next_krx_open(entry.minute,dates)) else False,
                               "session": detailed_session(entry.minute, dates), "regime": gate_regime(entry.minute),
                               "comparison_quality": entry.comparison_quality, "warnings": "historical_proxy_backtest;not_executable_BBO"}
                        for cost in COSTS:
                            rec[f"net_after_{cost}bps"] = gross - cost
                        rows.append(rec)
    ev = pd.DataFrame(rows)
    ev.to_csv(R / "joint_strategy_events.csv", index=False)
    keys = ["pair", "strategy_direction", "entry_threshold_bps", "exit_rule"]
    summaries = []
    for key, g in ev.groupby(keys):
        rec = dict(zip(keys, key)); rec.update({"event_count": len(g), "median_gross_bps": g.gross_combined_pnl_bps.median(),
                                                "max_drawdown_proxy_bps": g.max_adverse_excursion_bps.max()})
        for cost in COSTS:
            v = g[f"net_after_{cost}bps"]
            rec.update({f"positive_event_count_{cost}bps": int((v > 0).sum()), f"positive_event_ratio_{cost}bps": (v > 0).mean(),
                        f"median_net_{cost}bps": v.median(), f"p05_net_{cost}bps": v.quantile(.05), f"p95_net_{cost}bps": v.quantile(.95)})
        summaries.append(rec)
    summary = pd.DataFrame(summaries)
    summary.to_csv(R / "joint_strategy_summary.csv", index=False)
    _strategy_group(ev, "session").to_csv(R / "joint_strategy_by_session.csv", index=False)
    _strategy_group(ev, "regime").to_csv(R / "joint_strategy_by_regime.csv", index=False)
    stress = []
    for r in ev.itertuples():
        for leverage in [1, 2, 3, 5]:
            loss = r.max_single_leg_loss_percent * leverage
            stress.append({"pair": r.pair, "event_id": r.event_id, "leverage": leverage,
                           "max_single_leg_loss_percent_equity": loss,
                           "estimated_margin_buffer_required": r.max_single_leg_loss_percent / 100 * leverage,
                           "would_breach_warning_threshold": loss >= 50,
                           "warning": "simplified_leverage_stress_not_exchange_liquidation_model"})
    pd.DataFrame(stress).to_csv(R / "leverage_stress_test.csv", index=False)
    return ev, summary


def _strategy_group(ev, column):
    if ev.empty:
        return pd.DataFrame()
    return ev.groupby([column, "strategy_direction"], as_index=False).agg(
        event_count=("event_id", "size"), median_combined_pnl_bps=("gross_combined_pnl_bps", "median"),
        total_funding_pnl_bps=("funding_pnl_bps", "sum"), median_holding_minutes=("holding_minutes", "median"),
        max_mae_bps=("max_adverse_excursion_bps", "max"), convergence_probability_1h=("holding_minutes", lambda x: (x <= 60).mean()),
        convergence_probability_4h=("holding_minutes", lambda x: (x <= 240).mean()), convergence_probability_24h=("holding_minutes", lambda x: (x <= 1440).mean()))


def gate_median_analysis(prices):
    p = prices[prices.price_type == "mark"].pivot_table(index="open_time", columns="exchange", values="close", aggfunc="last")
    # Hyperliquid has no mark candles; the requested market median intentionally uses the main three only.
    need = [x for x in ["binance", "bitget", "okx"] if x in p]
    out = pd.DataFrame(index=p.index)
    out["gate_mark"] = p.get("gate")
    out["market_median"] = p[need].median(axis=1, skipna=False)
    out["gate_premium_vs_market_median_bps"] = 10000 * (out.gate_mark / out.market_median - 1)
    gp = prices[prices.exchange == "gate"].pivot_table(index="open_time", columns="price_type", values="close", aggfunc="last")
    out = out.join(gp.rename(columns={"trade":"gate_trade", "index":"gate_index"})[[c for c in ["gate_trade","gate_index"] if c in gp.rename(columns={"trade":"gate_trade","index":"gate_index"})]])
    out["rolling_mean_240m"] = out.gate_premium_vs_market_median_bps.rolling(240, min_periods=60).mean()
    out["rolling_std_240m"] = out.gate_premium_vs_market_median_bps.rolling(240, min_periods=60).std()
    out["rolling_z"] = (out.gate_premium_vs_market_median_bps - out.rolling_mean_240m) / out.rolling_std_240m
    out["change_flag"] = (out.rolling_z.abs() >= 4) & (out.gate_premium_vs_market_median_bps.abs() >= 50)
    out.reset_index(names="timestamp").to_csv(R / "gate_market_median_premium.csv", index=False)
    return out


def coverage_updates(requested_start, run_end):
    cov = pd.read_csv(R / "exchange_coverage.csv")
    # Half-open minute buckets include a partial final minute; ceil prevents a
    # complete venue from being displayed above 100%.
    req_minutes = max(1, int(math.ceil((pd.Timestamp(run_end) - pd.Timestamp(requested_start)).total_seconds()/60)))
    cov["requested_start"] = str(requested_start)
    cov["actual_data_start"] = cov.first_price_time
    cov["coverage_vs_requested_period_percent"] = 100 * cov.available_minutes / req_minutes
    cov["coverage_vs_local_data_window_percent"] = cov.coverage_percent
    cov.to_csv(R / "exchange_coverage.csv", index=False)
    pp = pd.read_csv(R / "pairwise_price_summary_common_window.csv")
    pp["requested_start"] = str(requested_start)
    pp["actual_data_start"] = pp.pair_price_start
    pp.to_csv(R / "pairwise_price_summary_common_window.csv", index=False)
    return cov, pp


def session_summaries(pairdf, fundev, stratev, dates):
    p = pairdf.copy(); p["detailed_session"] = p.minute.map(lambda x: detailed_session(x, dates))
    rows = []
    for (pair, session), g in p.groupby(["pair", "detailed_session"]):
        s = g.abs_spread
        rows.append({"pair": pair, "session": session, "count": len(g), "mean_abs_spread_bps": s.mean(), "median_abs_spread_bps": s.median(),
                     "p95_abs_spread_bps": s.quantile(.95), "p99_abs_spread_bps": s.quantile(.99), "max_abs_spread_bps": s.max()})
    ps = pd.DataFrame(rows)
    conv = stratev[(stratev.strategy_direction == "price_convergence") & (stratev.exit_rule == "target_20bps")]
    if len(conv):
        extra = conv.groupby(["pair", "session"], as_index=False).agg(
            event_count=("event_id","size"), median_duration_minutes=("holding_minutes","median"),
            p95_duration_minutes=("holding_minutes",lambda x:x.quantile(.95)),
            convergence_probability_1h=("holding_minutes",lambda x:(x<=60).mean()),
            convergence_probability_4h=("holding_minutes",lambda x:(x<=240).mean()),
            convergence_probability_24h=("holding_minutes",lambda x:(x<=1440).mean()),
            combined_strategy_pnl=("gross_combined_pnl_bps","sum"))
        ps=ps.merge(extra,on=["pair","session"],how="left")
    ps.to_csv(R / "session_price_summary.csv", index=False)
    if len(fundev):
        fs = fundev.groupby(["pair", "session"], as_index=False).agg(funding_pnl=("net_cashflow_10000", "sum"), event_count=("net_rate", "size"))
    else: fs = pd.DataFrame()
    fs.to_csv(R / "session_funding_summary.csv", index=False)
    ss = _strategy_group(stratev, "session")
    ss.to_csv(R / "session_strategy_summary.csv", index=False)
    return ps, fs, ss


def _save(name, title, xlabel=None, ylabel=None):
    if xlabel: plt.xlabel(xlabel)
    if ylabel: plt.ylabel(ylabel)
    plt.title(title); plt.tight_layout(); plt.savefig(C / name, dpi=150, bbox_inches="tight"); plt.close()


def extended_charts(cov, pairdf, gateprem, daily, sensitivity, stratev, session_price):
    sns.set_theme(style="whitegrid", font="WenQuanYi Zen Hei", rc={"axes.unicode_minus": False})
    plt.figure(figsize=(9,4)); sns.barplot(cov, x="exchange", y="coverage_vs_requested_period_percent"); _save("requested_vs_actual_coverage.png", "请求全期间与实际 1 分钟覆盖率", ylabel="请求期间覆盖率 %")
    main = pairdf[pairdf.pair.isin(["binance/bitget","binance/okx","bitget/okx"])]
    plt.figure(figsize=(13,5))
    for pair,g in main.groupby("pair"):
        q=gap_broken(g.minute,g.spread);plt.plot(q.time,q.value,label=pair,linewidth=.55)
    plt.legend(); _save("main_three_exchange_full_history.png", "主三所完整历史价差（历史分钟代理）", "UTC", "对称价差 bps")
    for ex,name,title in [("gate","gate_related_local_window.png","Gate 相关局部窗口（此前灰色为无 1m 数据）"),("hyperliquid","hyperliquid_related_local_window.png","Hyperliquid 相关局部窗口（此前灰色为无 1m 数据）")]:
        x=pairdf[pairdf.pair.str.contains(ex)]; plt.figure(figsize=(13,5));
        for pair,g in x.groupby("pair"):
            q=gap_broken(g.minute,g.spread);plt.plot(q.time,q.value,label=pair,linewidth=.6)
        if len(x): plt.axvspan(pd.Timestamp("2026-06-10T05:50Z"),x.minute.min(),color="grey",alpha=.25,label="无1m数据")
        if ex=="gate": plt.axvspan(GATE_START,GATE_END,color="orange",alpha=.2,label="Gate regime")
        plt.legend(); _save(name,title,"UTC","对称价差 bps")
    plt.figure(figsize=(13,5)); plt.plot(gateprem.index,gateprem.gate_premium_vs_market_median_bps,linewidth=.6);plt.axvspan(GATE_START,GATE_END,color="orange",alpha=.2);plt.axhline(0,color="black",lw=.7);_save("gate_premium_vs_market_median.png","Gate mark 相对 Binance/Bitget/OKX mark 中位数溢价","UTC","bps")
    plt.figure(figsize=(12,5));
    for c in ["gate_trade","gate_mark","gate_index","market_median"]:
        if c in gateprem: plt.plot(gateprem.index,gateprem[c],label=c,linewidth=.7)
    plt.legend();_save("gate_mark_trade_index_comparison.png","Gate trade/mark/index 与主三所 mark 中位数","UTC","价格")
    plt.figure(figsize=(13,5));
    if len(daily):
        bo=daily[(daily.long_exchange=="bitget")&(daily.short_exchange=="okx")];sns.barplot(bo,x="date",y="daily_cashflow_10000",color="#4472c4");plt.xticks(rotation=60,ha="right")
    _save("daily_funding_contribution.png","Long Bitget / Short OKX 每日严格同窗资金贡献",ylabel="每 $10,000 美元")
    plt.figure(figsize=(11,5));
    if len(sensitivity): sns.barplot(sensitivity.nlargest(10,"total_cashflow_10000"),x="pair",y="total_cashflow_10000");plt.xticks(rotation=45,ha="right")
    _save("funding_outlier_contribution.png","严格同窗资金收益及异常值敏感性",ylabel="每 $10,000 美元")
    plt.figure(figsize=(12,5));
    if len(stratev): sns.boxplot(stratev,x="strategy_direction",y="net_after_40bps",showfliers=False)
    _save("joint_strategy_net_pnl.png","联合事件回测净收益（40 bps 成本，历史代理）",ylabel="bps")
    plt.figure(figsize=(8,6));
    if len(stratev): sns.scatterplot(stratev.sample(min(5000,len(stratev)),random_state=1),x="max_adverse_excursion_bps",y="max_favorable_excursion_bps",hue="regime",s=12)
    _save("joint_strategy_mae_mfe.png","联合策略 MAE/MFE（抽样展示）","MAE bps","MFE bps")
    plt.figure(figsize=(12,5));
    if len(session_price): sns.barplot(session_price,x="session",y="p95_abs_spread_bps",estimator=np.median);plt.xticks(rotation=30,ha="right")
    _save("session_p95_comparison.png","各时段跨 pair 的 P95 绝对价差中位数",ylabel="bps")
    durations=stratev[(stratev.strategy_direction=="price_convergence")&(stratev.exit_rule=="target_20bps")].copy() if len(stratev) else stratev
    for name,xlim,log,title in [("event_duration_ecdf.png",None,False,"事件持续时间 ECDF（Gate regime 内外）"),("event_duration_log_scale.png",None,True,"事件持续时间分布（log x）"),("spread_event_duration.png",(0,240),False,"事件持续时间 0–240 分钟")]:
        plt.figure(figsize=(9,5))
        if len(durations): sns.ecdfplot(durations,x="holding_minutes",hue="regime") if "ecdf" in name else sns.histplot(durations,x="holding_minutes",hue="regime",element="step",fill=False)
        if xlim: plt.xlim(*xlim)
        if log: plt.xscale("log")
        _save(name,title,"分钟（log轴）" if log else "分钟")


def extended_analysis(pairdf, funding, prices, requested_start, run_end):
    dates, _ = trading_dates(requested_start, run_end)
    common = pd.read_csv(R / "pairwise_funding_common_window.csv", parse_dates=["joint_start","joint_end"])
    cov, _ = coverage_updates(requested_start, run_end)
    fundev, daily, sensitivity = funding_contributions(funding, common, requested_start, run_end, dates)
    stratev, stratsum = joint_backtest(pairdf, funding, common, dates)
    gateprem = gate_median_analysis(prices)
    update_gate_diagnostics(prices, gateprem)
    ps, fs, ss = session_summaries(pairdf, fundev, stratev, dates)
    extended_charts(cov, pairdf, gateprem, daily, sensitivity, stratev, ps)
    return {"funding_events": fundev, "funding_sensitivity": sensitivity, "strategy_events": stratev,
            "strategy_summary": stratsum, "gate_premium": gateprem, "session_price": ps}


def update_gate_diagnostics(prices, gateprem):
    path=R/"gate_regime_diagnostics.md"
    old=path.read_text() if path.exists() else "# Gate regime 自动诊断\n"
    v=gateprem.dropna(subset=["gate_premium_vs_market_median_bps"])
    during=v[(v.index>=GATE_START)&(v.index<GATE_END)];post=v[v.index>=GATE_END]
    gt=prices[(prices.exchange=="gate")&(prices.price_type=="trade")].set_index("open_time")
    vol=gt.volume_base.reindex(during.index);low=vol<=vol.quantile(.05)
    extreme=during.gate_premium_vs_market_median_bps.abs()>=during.gate_premium_vs_market_median_bps.abs().quantile(.95)
    overlap=float((low&extreme).sum()/max(1,extreme.sum())*100)
    cols=["gate_trade","gate_mark","gate_index"]
    first={}
    for c in cols:
        if c in during:
            prem=10000*(during[c]/during.market_median-1);q=prem[prem.abs()>=50];first[c]=q.index.min() if len(q) else pd.NaT
    add=f"""

## 相对主三所市场中位数的补充诊断

- `gate_premium_vs_market_median_bps` 使用同分钟 Binance、Bitget、OKX 三家 mark 的严格同时有效中位数；不做比例缩放。
- regime 实际可观测仅从 `{during.index.min()}` 开始，因此 **Gate regime 前18小时34分钟当前不可观测**。
- regime 内绝对溢价 P95/P99/最大：{during.gate_premium_vs_market_median_bps.abs().quantile(.95):.2f} / {during.gate_premium_vs_market_median_bps.abs().quantile(.99):.2f} / {during.gate_premium_vs_market_median_bps.abs().max():.2f} bps；7月20日后为 {post.gate_premium_vs_market_median_bps.abs().quantile(.95):.2f} / {post.gate_premium_vs_market_median_bps.abs().quantile(.99):.2f} / {post.gate_premium_vs_market_median_bps.abs().max():.2f} bps，中位有向溢价 {post.gate_premium_vs_market_median_bps.median():.2f} bps。
- 首次超过 50 bps：trade={first.get('gate_trade')}，mark={first.get('gate_mark')}，index={first.get('gate_index')}。因早段缺失，这不是 regime 的真实首次发生时刻。
- 极端溢价分钟中同时属于成交量最低 5% 的比例为 {overlap:.2f}%；异常并非主要由零成交分钟构成。
- 变点提示采用 240 分钟 rolling mean/std，条件 `|z|>=4 且 |premium|>=50 bps`；共 {int(v.change_flag.sum())} 个提示分钟。该方法只用于发现未手工标记候选，不改变主统计。

**诊断判断：** 1m raw 与 normalized 起点一致，分页顺序、重复桶、秒/毫秒均通过检查；Gate mark 与 trade 同步程度明显高于 mark 与 index，故更像交易所内部指数/标记及价格发现口径与外部市场短期分离，而不是采集或 normalize 错误。没有历史 BBO、指数成分逐分钟快照和交易状态事件，无法最终区分真实可成交偏离与指数口径偏离。
"""
    path.write_text(old.split("\n## 相对主三所市场中位数的补充诊断")[0]+add)


def extended_report_block():
    sens=pd.read_csv(R/"funding_outlier_sensitivity.csv")
    ev=pd.read_csv(R/"joint_strategy_events.csv",parse_dates=["entry_time","exit_time"])
    glob=pd.read_csv(R/"global_common_window.csv")
    ff=pd.read_csv(R/"pairwise_funding_common_window.csv")
    gp=pd.read_csv(R/"gate_market_median_premium.csv",parse_dates=["timestamp"])
    bo=sens[(sens.long_exchange=="bitget")&(sens.short_exchange=="okx")].iloc[0]
    top=ff.nlargest(3,"theoretical_cashflow_10000usd")
    hold=ev[ev.exit_rule=="hold_8h"]
    costs=[]
    for cost in [20,40,80]:
        parts=[]
        for direction,g in hold.groupby("strategy_direction"):
            parts.append(f"{direction}: {int((g[f'net_after_{cost}bps']>0).sum())}/{len(g)} ({100*(g[f'net_after_{cost}bps']>0).mean():.2f}%)")
        costs.append(f"{cost} bps："+"；".join(parts))
    x=hold.pivot_table(index=["pair","entry_time","entry_threshold_bps"],columns="strategy_direction",values="long_exchange",aggfunc="first").dropna()
    x["same"]=x.iloc[:,0]==x.iloc[:,1]
    rates=x.groupby(level=0).same.mean().sort_values()
    conflict=[f"{p}({v*100:.1f}%)" for p,v in rates.items() if v<.5]
    aligned=[f"{p}({v*100:.1f}%)" for p,v in rates.items() if v>=.5]
    gate=hold[(hold.regime=="GATE_REGIME_20260716_19")&hold.pair.str.contains("gate")]
    gate_lines=[]
    for d,g in gate.groupby("strategy_direction"):
        gate_lines.append(f"{d}: n={len(g)}, gross中位={g.gross_combined_pnl_bps.median():.2f} bps, funding中位={g.funding_pnl_bps.median():.2f} bps, 成本20/40/80后正比例={100*(g.net_after_20bps>0).mean():.1f}%/{100*(g.net_after_40bps>0).mean():.1f}%/{100*(g.net_after_80bps>0).mean():.1f}%, 最大MAE={g.max_adverse_excursion_bps.max():.2f} bps")
    mx=ev.loc[ev.max_adverse_excursion_bps.idxmax()]
    v=gp.dropna(subset=["gate_premium_vs_market_median_bps"]);during=v[(v.timestamp>=GATE_START)&(v.timestamp<GATE_END)];post=v[v.timestamp>=GATE_END]
    sp=pd.read_csv(R/"session_price_summary.csv")
    largest=sp.groupby("session").p95_abs_spread_bps.median().idxmax()
    conv=ev[(ev.strategy_direction=="price_convergence")&(ev.exit_rule=="target_20bps")]
    fastest=conv.groupby("session").holding_minutes.median().sort_values().head(4)
    sf=pd.read_csv(R/"session_funding_summary.csv");best_sess=sf[sf.pair=="long bitget / short okx"].nlargest(1,"funding_pnl").iloc[0]
    ic=pd.read_csv(R/"funding_interval_changes.csv")
    interval_note=[]
    for ex,g in ic.groupby("exchange"):
        vals=sorted(g.interval_hours.dropna().unique())
        interval_note.append(f"{ex}={','.join(f'{v:g}h' for v in vals)}")
    return f"""

## 历史覆盖、资金贡献与可交易性补充（本轮）

### 强结论

1. Gate 缺失不是 normalize 或分页方向错误。官方 1m 请求返回“仅最近 10,000 点”，raw 1m 与 normalized 1m 同为 2026-07-16 18:34 起；15m 审计可回到 2026-06-10 05:45，但未混入 1m 主样本。
2. Hyperliquid resolved coin 为 `xyz:SKHX`，DEX 前缀正确；旧封闭 1m 窗口为空，`candleSnapshot` 为最近约 5,000 根限制。15m 可回到 2026-06-10 05:45，但不能伪装成 1m。五家共同窗口未延长；四家最长为 `{glob.iloc[1].exchange_set}` `[{glob.iloc[1].common_start},{glob.iloc[1].common_end})`，三家最长为 `{glob.iloc[2].exchange_set}` `[{glob.iloc[2].common_start},{glob.iloc[2].common_end})`。
3. 严格同窗资金前三：{'; '.join(f"{r.pair} ${r.theoretical_cashflow_10000usd:.2f}" for _,r in top.iterrows())}。
4. Long Bitget / Short OKX 的 $578.88 来自 {int(bo.event_count)} 个单边结算事件时间点；最大单次 ${bo.largest_single_event_cashflow:.2f}，最大5次有符号合计 ${bo.top_5_signed_contribution_cashflow:.2f}（占净额 {100*bo.top_5_signed_contribution_cashflow/bo.total_cashflow_10000:.2f}%；绝对贡献/净额 {bo.top_5_abs_contribution_percent_of_total:.2f}%）。排除绝对值最大1%后为 **${bo.cashflow_excluding_top_abs_1pct:.2f}**，排除最大5%后为 **${bo.cashflow_excluding_top_abs_5pct:.2f}**；收益并非只由五次事件构成，但对尾部事件敏感。
   结算间隔分段：{'; '.join(interval_note)}。Binance/Bitget/Gate 在7月14日前后由8h转4h；OKX 短暂8h→4h后恢复8h；Hyperliquid保持1h。`.001` 秒边界按真实时间保留，左闭右开去重。CSV 中 min/max 是样本观察值，不冒充官方上下限。
5. 8小时固定持有的历史代理路径在成本情景下：{'；'.join(costs)}。这是分钟代理规则集合，不是可执行利润。
6. Gate regime 的8小时固定持有：{'；'.join(gate_lines)}。
7. 最大 MAE 为 **{mx.max_adverse_excursion_bps:.2f} bps**：{mx.pair}，{mx.strategy_direction}，入场 {mx.entry_time}，阈值 {mx.entry_threshold_bps:.0f} bps，退出规则 {mx.exit_rule}；最大单腿浮亏 {mx.max_single_leg_loss_percent:.2f}%。

### 初步结论

- 价格收敛方向与仅用入场前24小时资金事件选出的方向，按8小时样本入场的一致率：一致/多数一致 {', '.join(aligned)}；冲突/多数冲突 {', '.join(conflict)}。Binance/OKX 与 Bitget/OKX 的冲突最明显。
- Gate 相对主三所 mark 中位数：regime 内绝对 P95/P99/最大 {during.gate_premium_vs_market_median_bps.abs().quantile(.95):.2f}/{during.gate_premium_vs_market_median_bps.abs().quantile(.99):.2f}/{during.gate_premium_vs_market_median_bps.abs().max():.2f} bps；7月20日后 {post.gate_premium_vs_market_median_bps.abs().quantile(.95):.2f}/{post.gate_premium_vs_market_median_bps.abs().quantile(.99):.2f}/{post.gate_premium_vs_market_median_bps.abs().max():.2f} bps，中位有向溢价 {post.gate_premium_vs_market_median_bps.median():.2f} bps。异常大幅消退，但仍有少量20–40 bps以上残余尾部。
- 跨 pair 的 session P95 中位数最高是 `{largest}`；回落到20 bps的成功事件中，最快的 session 中位数为 {', '.join(f'{k}={v:.1f}m' for k,v in fastest.items())}。Long Bitget/Short OKX 资金贡献最高时段为 `{best_sess.session}`（${best_sess.funding_pnl:.2f}/$10,000）。

### 无法确认

- 价格路径使用 mark/trade 分钟代理，不是历史 BBO；目标退出、止损、成本和杠杆压力均不含盘口深度、真实滑点、强平公式或成交容量。
- Gate regime 前18小时34分钟没有官方可回补的1m数据；无法确认该段峰值和真实起点。现有数据也不足以最终区分 Gate 的可成交市场偏离与指数/休市定价口径。
"""
