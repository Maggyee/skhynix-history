from __future__ import annotations

import itertools
import json
import math
import time
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib import font_manager

try: font_manager.fontManager.addfont("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")
except Exception: pass
plt.rcParams["font.family"]=["WenQuanYi Zen Hei","Unifont","DejaVu Sans"]
plt.rcParams["axes.unicode_minus"]=False

from .analysis import symmetric_spread_bps
from .common_windows import gate_regime
from .config import ROOT, load_config
from .download import ENDPOINTS, candle, discover_all, ms
from .history_quality import detailed_session
from .http import CachedHTTP
from .report_15m import write_15m_report

EXCHANGES = ("binance", "bitget", "gate", "hyperliquid", "okx")
BAR = pd.Timedelta(minutes=15)
ANALYSIS_START = pd.Timestamp("2026-06-10T06:00:00Z")
R15 = ROOT / "reports_15m"


def _closed_end(end) -> pd.Timestamp:
    """Exclusive open-time bound: only bars whose complete 15m interval has closed."""
    return pd.Timestamp(end).floor("15min")


def _native(row: dict) -> dict:
    row["close_time"] = pd.Timestamp(row["open_time"]) + BAR - pd.Timedelta(milliseconds=1)
    row["native_interval"] = "15m"
    row["interval_minutes"] = 15
    return row


def _download_binance_15m(start, end, symbol):
    h = CachedHTTP("binance"); rows = []
    for typ, ep, key in [("trade", "/fapi/v1/klines", "symbol"), ("mark", "/fapi/v1/markPriceKlines", "symbol"), ("index", "/fapi/v1/indexPriceKlines", "pair")]:
        cur = ms(start)
        while cur < ms(end):
            data, raw = h.get(ENDPOINTS["binance"] + ep, {key: symbol, "interval": "15m", "startTime": cur, "endTime": ms(end)-1, "limit": 1500})
            if not data: break
            for x in data:
                rows.append(_native(candle("binance", symbol, typ, [*x[:6], x[7] if len(x)>7 else None, x[6]], ep, raw, "array")))
            nxt = int(data[-1][0]) + 900_000
            if nxt <= cur: break
            cur = nxt
    return rows


def _download_bitget_15m(start, end, symbol):
    h = CachedHTTP("bitget"); rows = []
    for typ, ptype in [("trade", "market"), ("mark", "mark"), ("index", "index")]:
        cursor = ms(end)-1
        while cursor >= ms(start):
            data, raw = h.get(ENDPOINTS["bitget"] + "/api/v3/market/history-candles", {"category":"USDT-FUTURES", "symbol":symbol, "interval":"15m", "endTime":cursor, "limit":100, "type":ptype})
            arr = data.get("data", [])
            if not arr: break
            for x in arr:
                if ms(start) <= int(x[0]) < ms(end):
                    rows.append(_native(candle("bitget", symbol, typ, [*x[:7], int(x[0])+899_999], "/api/v3/market/history-candles", raw, "array")))
            oldest = min(int(x[0]) for x in arr)
            if oldest >= cursor or oldest <= ms(start): break
            cursor = oldest - 1
    return rows


def _download_okx_15m(start, end, symbol):
    h = CachedHTTP("okx"); rows = []
    specs = [("trade", "/api/v5/market/history-candles", symbol), ("mark", "/api/v5/market/history-mark-price-candles", symbol), ("index", "/api/v5/market/history-index-candles", symbol.replace("-SWAP", ""))]
    for typ, ep, inst in specs:
        cursor = None
        while True:
            params = {"instId":inst, "bar":"15m", "limit":100}
            if cursor is not None: params["after"] = cursor
            data, raw = h.get(ENDPOINTS["okx"] + ep, params); arr = data.get("data", [])
            if not arr: break
            for x in arr:
                t = int(x[0])
                confirmed = not (len(x) >= 9 and str(x[-1]) == "0")
                if ms(start) <= t < ms(end) and confirmed:
                    v = x[5] if typ == "trade" and len(x)>5 else None
                    q = x[7] if typ == "trade" and len(x)>7 else None
                    rows.append(_native(candle("okx", symbol, typ, [x[0],x[1],x[2],x[3],x[4],v,q,t+899_999], ep, raw, "array")))
            oldest = min(int(x[0]) for x in arr)
            if oldest <= ms(start) or str(oldest) == str(cursor): break
            cursor = str(oldest)
    return rows


def _download_gate_15m(start, end, symbol):
    h = CachedHTTP("gate"); rows = []; ep = "/futures/usdt/candlesticks"
    for typ, prefix in [("trade", ""), ("mark", "mark_"), ("index", "index_")]:
        cur = int(pd.Timestamp(start).timestamp()); end_s = int(pd.Timestamp(end).timestamp())
        while cur < end_s:
            upto = min(end_s-1, cur + 1900*900-1)
            data, raw = h.get(ENDPOINTS["gate"] + ep, {"contract":prefix+symbol, "from":cur, "to":upto, "interval":"15m"})
            for x in data if isinstance(data, list) else []:
                rows.append(_native(candle("gate", symbol, typ, x, ep, raw, "dict")))
            cur = upto + 1
    return rows


def _download_hyperliquid_15m(start, end, symbol):
    h = CachedHTTP("hyperliquid")
    data, raw = h.post(ENDPOINTS["hyperliquid"], {"type":"candleSnapshot", "req":{"coin":symbol, "interval":"15m", "startTime":ms(start), "endTime":ms(end)-1}})
    return [_native(candle("hyperliquid", symbol, "trade", x, "POST /info candleSnapshot", raw, "dict")) for x in data]


NATIVE_DOWNLOADERS = {
    "binance": _download_binance_15m, "bitget": _download_bitget_15m,
    "gate": _download_gate_15m, "hyperliquid": _download_hyperliquid_15m,
    "okx": _download_okx_15m,
}


def download_native_prices_15m(start, end) -> pd.DataFrame:
    start = max(pd.Timestamp(start), ANALYSIS_START).ceil("15min"); closed_end = _closed_end(end)
    if start >= closed_end: raise ValueError("No complete 15m bars in requested interval")
    meta, errors = discover_all(); all_rows = []
    old_path = ROOT/"data/normalized/prices_15m.parquet"
    if old_path.exists(): all_rows.extend(pd.read_parquet(old_path).to_dict("records"))
    for ex in EXCHANGES:
        try:
            symbol = meta.loc[(meta.exchange==ex)&(meta.status!="failed"), "resolved_symbol"].iloc[0]
            all_rows.extend(NATIVE_DOWNLOADERS[ex](start, closed_end, symbol))
        except Exception as exc:
            errors[f"{ex}_15m"] = str(exc)
    out = pd.DataFrame(all_rows)
    if out.empty: raise RuntimeError(f"No native 15m prices downloaded: {errors}")
    out["open_time"] = pd.to_datetime(out.open_time, utc=True)
    out["close_time"] = pd.to_datetime(out.close_time, utc=True)
    out = out[(out.open_time>=start)&(out.open_time<closed_end)&(out.close>0)]
    out = out[(out.open_time.dt.minute%15==0)&(out.open_time.dt.second==0)]
    out = out.sort_values(["exchange","price_type","open_time"]).drop_duplicates(["exchange","symbol","price_type","open_time"], keep="last")
    out.to_parquet(old_path, index=False)
    (ROOT/"data/raw/download_errors_15m.json").write_text(json.dumps(errors, ensure_ascii=False, indent=2))
    return out


def funding_events_in_position_window(funding, start, end):
    """Real settlements strictly after entry and before exit; never prorated into bars."""
    t = pd.to_datetime(funding.funding_time, utc=True)
    return funding[(t > pd.Timestamp(start)) & (t < pd.Timestamp(end))].copy()


def _funding_bounds(funding):
    missing = [x for x in EXCHANGES if x not in set(funding.exchange)]
    if missing: raise ValueError(f"Missing funding exchanges: {missing}")
    first = funding.groupby("exchange").funding_time.min()
    last = funding.groupby("exchange").funding_time.max()
    start = pd.Timestamp(first.max()).ceil("h")
    end = pd.Timestamp(last.min()).floor("h")
    if start >= end: raise ValueError("Funding common window is empty")
    return start, end, first, last


def build_funding_global_outputs(funding: pd.DataFrame, start=None, end=None, charts=True):
    """FUNDING_ONLY_GLOBAL_WINDOW. This function has no price-data dependency."""
    funding = funding.copy(); funding["funding_time"] = pd.to_datetime(funding.funding_time, utc=True)
    auto_start, auto_end, first, last = _funding_bounds(funding)
    start = pd.Timestamp(start) if start is not None else auto_start
    end = pd.Timestamp(end) if end is not None else auto_end
    duration_hours = (end-start).total_seconds()/3600
    selected = funding_events_in_position_window(funding, start, end)
    coverage = []
    for ex in EXCHANGES:
        g = selected[selected.exchange==ex].sort_values("funding_time")
        diffs = g.funding_time.diff().dt.total_seconds().div(3600).dropna()
        typical = float(diffs.median()) if len(diffs) else np.nan
        missing_count = int((diffs > typical*1.5).sum()) if pd.notna(typical) else 0
        warnings = []
        if g.funding_time.duplicated().any(): warnings.append("duplicate_settlement")
        if missing_count: warnings.append(f"{missing_count}_gaps_over_1.5x_median")
        coverage.append({"analysis_scope":"FUNDING_ONLY_GLOBAL_WINDOW", "exchange":ex,
            "global_start":start, "global_end":end, "duration_hours":duration_hours,
            "available_first_event":first[ex], "available_last_event":last[ex],
            "event_count":len(g), "median_event_interval_hours":typical,
            "missing_gap_count":missing_count, "duplicate_count":int(g.funding_time.duplicated().sum()),
            "data_quality":"OK" if not warnings else "CHECK", "warnings":"|".join(warnings)})
    coverage = pd.DataFrame(coverage)
    rows = []
    sums = selected.groupby("exchange").funding_rate.sum()
    counts = selected.groupby("exchange").size()
    for long_ex, short_ex in itertools.permutations(EXCHANGES, 2):
        net = -float(sums.get(long_ex, 0)) + float(sums.get(short_ex, 0))
        cash = net*10_000; per_day = cash/(duration_hours/24)
        warnings = "|".join(coverage.loc[coverage.exchange.isin([long_ex,short_ex]) & (coverage.data_quality!="OK"), "warnings"].tolist())
        rows.append({"analysis_scope":"FUNDING_ONLY_GLOBAL_WINDOW", "long_exchange":long_ex, "short_exchange":short_ex,
            "global_start":start, "global_end":end, "duration_hours":duration_hours,
            "long_event_count":int(counts.get(long_ex,0)), "short_event_count":int(counts.get(short_ex,0)),
            "sum_long_funding":float(sums.get(long_ex,0)), "sum_short_funding":float(sums.get(short_ex,0)),
            "net_funding_rate":net, "cashflow_10000usd":cash, "cashflow_per_day_10000usd":per_day,
            "simple_apr_not_compounded":net/(duration_hours/24)*365,
            "data_quality":"OK" if not warnings else "CHECK", "warnings":warnings})
    matrix = pd.DataFrame(rows)
    daily = matrix[["analysis_scope","long_exchange","short_exchange","global_start","global_end","duration_hours","cashflow_per_day_10000usd","simple_apr_not_compounded","data_quality","warnings"]].copy()
    (ROOT/"reports").mkdir(exist_ok=True); (ROOT/"reports/charts").mkdir(exist_ok=True)
    coverage.to_csv(ROOT/"reports/funding_global_common_window.csv", index=False)
    matrix.to_csv(ROOT/"reports/funding_global_matrix.csv", index=False)
    daily.to_csv(ROOT/"reports/funding_global_daily_equivalent_matrix.csv", index=False)
    if charts:
        _matrix_chart(matrix, "cashflow_10000usd", ROOT/"reports/charts/funding_global_common_window_matrix.png",
            "五家统一资金窗口：做多行/做空列每$10,000累计资金现金流", start, end)
        _matrix_chart(matrix, "cashflow_per_day_10000usd", ROOT/"reports/charts/funding_daily_equivalent_matrix.png",
            "FUNDING_ONLY_GLOBAL_WINDOW：每$10,000日均资金现金流", start, end)
    return coverage, matrix, daily


def analyze_funding_global_from_file(path=None):
    path = path or ROOT/"data/normalized/funding_events.parquet"
    return build_funding_global_outputs(pd.read_parquet(path))


def _strict_trade_intersection(prices):
    validate_native_prices_15m(prices)
    trade = prices[(prices.price_type=="trade") & (prices.exchange.isin(EXCHANGES))].copy()
    common = None
    for ex in EXCHANGES:
        times = set(trade.loc[trade.exchange==ex, "open_time"])
        common = times if common is None else common & times
    common = sorted(common or [])
    if not common: raise ValueError("No all-five native 15m trade-close intersection")
    aligned = trade[trade.open_time.isin(common)].copy()
    aligned["analysis_scope"] = "ALL_FIVE_TRADE_CLOSE_15M"
    aligned["comparison_price_type"] = "trade_close_15m"
    return aligned, pd.DatetimeIndex(common)


def validate_native_prices_15m(prices):
    required={"exchange","price_type","open_time","close","native_interval","interval_minutes","source_endpoint"}
    missing=required-set(prices.columns)
    if missing: raise ValueError(f"15m dataset missing provenance columns: {sorted(missing)}")
    times=pd.to_datetime(prices.open_time,utc=True)
    if not (prices.native_interval.eq("15m").all() and prices.interval_minutes.eq(15).all()):
        raise ValueError("Non-native-15m rows are forbidden; never aggregate incomplete 1m history")
    if not ((times.dt.minute%15==0)&(times.dt.second==0)&(times.dt.microsecond==0)).all():
        raise ValueError("15m open timestamps must align to 00/15/30/45 minutes")
    if prices.source_endpoint.astype(str).str.contains("resample|aggregate|prices_1m",case=False,regex=True).any():
        raise ValueError("Synthetic 15m data derived from 1m is forbidden")
    return True


def _pair_frames(aligned):
    wide = aligned.pivot(index="open_time", columns="exchange", values="close").sort_index()
    for a,b in itertools.combinations(EXCHANGES,2):
        z = wide[[a,b]].dropna().copy()
        z["spread_bps"] = symmetric_spread_bps(z[a], z[b])
        z["abs_spread_bps"] = z.spread_bps.abs(); z["pair"] = f"{a}/{b}"
        yield a,b,z.reset_index()


def _events_15m(z, threshold, allow_one_missing=False):
    z = z.sort_values("open_time").reset_index(drop=True)
    active = z[z.abs_spread_bps >= threshold].copy()
    if active.empty: return []
    source_times=set(z.open_time)
    def breaks(prev,cur):
        if pd.isna(prev): return True
        gap=(cur-prev).total_seconds()/60
        if gap==15: return False
        # Sensitivity merges exactly one absent source bar, never a real bar
        # that exists but fell below the event threshold.
        return not (allow_one_missing and gap==30 and prev+BAR not in source_times)
    flags=[];prev=pd.NaT
    for cur in active.open_time:
        flags.append(breaks(prev,cur));prev=cur
    active["group"] = pd.Series(flags,index=active.index).cumsum()
    rows=[]
    for _,g in active.groupby("group"):
        peak = g.loc[g.abs_spread_bps.idxmax()]; last = g.iloc[-1]
        duration_bars = len(g)
        status = "OPEN" if last.open_time == z.open_time.max() and last.abs_spread_bps >= threshold else "COMPLETED"
        rows.append({"event_start":g.open_time.min(), "event_end":g.open_time.max(), "duration_bars":duration_bars,
            "duration_minutes":duration_bars*15, "status":status, "peak_abs_spread_bps":peak.abs_spread_bps,
            "peak_time":peak.open_time, "peak_spread_bps":peak.spread_bps,
            "start_session":detailed_session(g.open_time.min()), "start_regime":gate_regime(g.open_time.min()),
            "comparison_quality":"ALLOW_ONE_MISSING_15M_BAR" if allow_one_missing else "STRICT_NATIVE_15M_BARS"})
    return rows


def _price_summaries(aligned, start, end):
    rows=[]
    for a,b,z in _pair_frames(aligned):
        s=z.abs_spread_bps
        rows.append({"analysis_scope":"ALL_FIVE_TRADE_CLOSE_15M", "pair":f"{a}/{b}", "exchange_A":a, "exchange_B":b,
            "global_start":start, "global_end":end, "bar_count":len(z), "comparison_price_type":"trade_close_15m",
            "mean_abs_bps":s.mean(), "median_signed_spread_bps":z.spread_bps.median(), "p95_abs_bps":s.quantile(.95), "p99_abs_bps":s.quantile(.99), "max_abs_bps":s.max(),
            "percent_A_higher":100*(z.spread_bps>0).mean(), "data_quality":"strict_all_five_timestamp_intersection"})
    return pd.DataFrame(rows)


def _mark_subset_summary(prices):
    mark = prices[prices.price_type=="mark"]
    available = sorted(ex for ex in EXCHANGES if len(mark[mark.exchange==ex]))
    rows=[]
    for a,b in itertools.combinations(available,2):
        x=mark[mark.exchange==a][["open_time","close"]].merge(mark[mark.exchange==b][["open_time","close"]],on="open_time",suffixes=("_a","_b"))
        if x.empty: continue
        s=pd.Series(symmetric_spread_bps(x.close_a,x.close_b)).abs()
        rows.append({"analysis_scope":"MARK_AVAILABLE_SUBSET_15M", "pair":f"{a}/{b}", "exchange_A":a, "exchange_B":b,
            "start":x.open_time.min(), "end":x.open_time.max()+BAR, "bar_count":len(x), "comparison_price_type":"mark_close_15m",
            "p95_abs_bps":s.quantile(.95), "p99_abs_bps":s.quantile(.99), "max_abs_bps":s.max()})
    return pd.DataFrame(rows)


def _matrix_for_window(funding, start, end):
    selected=funding_events_in_position_window(funding,start,end); sums=selected.groupby("exchange").funding_rate.sum(); counts=selected.groupby("exchange").size();hours=(end-start).total_seconds()/3600
    rows=[]
    for a,b in itertools.permutations(EXCHANGES,2):
        net=-float(sums.get(a,0))+float(sums.get(b,0))
        rows.append({"analysis_scope":"PRICE_FUNDING_15M_GLOBAL_WINDOW", "long_exchange":a,"short_exchange":b,"global_start":start,"global_end":end,"duration_hours":hours,"long_event_count":int(counts.get(a,0)),"short_event_count":int(counts.get(b,0)),"sum_long_funding":float(sums.get(a,0)),"sum_short_funding":float(sums.get(b,0)),"net_funding_rate":net,"cashflow_10000usd":net*10000,"data_quality":"strict_joint_15m_global_window","warnings":"real_settlement_events_only_not_prorated"})
    return pd.DataFrame(rows)


def _joint_events(aligned, base_events, funding, joint_start, joint_end):
    events=[]
    strict=base_events[(base_events.comparison_quality=="STRICT_NATIVE_15M_BARS") & (base_events.event_start>=joint_start) & (base_events.event_end<joint_end)]
    pair_map={f"{a}/{b}":z.set_index("open_time") for a,b,z in _pair_frames(aligned)}
    for r in strict.itertuples():
        z=pair_map[r.pair]; a,b=r.pair.split("/"); entry=z.loc[r.event_start]
        exit_time=min(pd.Timestamp(r.event_end)+BAR, joint_end-BAR)
        exit_row=z.loc[exit_time] if exit_time in z.index else z.loc[r.event_end]
        if entry.spread_bps>=0: long_ex,short_ex=b,a
        else: long_ex,short_ex=a,b
        fe=funding_events_in_position_window(funding,r.event_start,exit_time+BAR)
        lf=fe[fe.exchange==long_ex]; sf=fe[fe.exchange==short_ex]
        funding_bps=(-lf.funding_rate.sum()+sf.funding_rate.sum())*10000
        gross=max(0,float(r.peak_abs_spread_bps)-float(exit_row.abs_spread_bps))
        rec={"analysis_scope":"PRICE_FUNDING_15M_GLOBAL_WINDOW", "base_event_id":r.base_event_id,"pair":r.pair,"threshold_bps":r.threshold_bps,
            "entry_time":r.event_start,"exit_time":exit_time,"long_exchange":long_ex,"short_exchange":short_ex,
            "gross_price_convergence_bps":gross,"funding_cashflow_bps":funding_bps,"long_funding_event_count":len(lf),"short_funding_event_count":len(sf),
            "combined_gross_bps":gross+funding_bps,"data_quality":"native_15m_trade_close_plus_real_settlements"}
        for cost in [20,40,80]: rec[f"net_after_cost_{cost}bps"] = rec["combined_gross_bps"]-cost
        events.append(rec)
    return pd.DataFrame(events)


def _charts(aligned, summary, events, start, end):
    (R15/"charts").mkdir(parents=True, exist_ok=True); sns.set_theme(style="whitegrid",font="WenQuanYi Zen Hei",rc={"axes.unicode_minus":False})
    plt.figure(figsize=(14,6))
    for a,b,z in _pair_frames(aligned): plt.plot(z.open_time,z.spread_bps,label=f"{a}/{b}",linewidth=.65)
    plt.axhline(0,color="black",linewidth=.5);plt.legend(ncol=2,fontsize=8);plt.ylabel("symmetric spread bps");plt.title(f"ALL_FIVE_TRADE_CLOSE_15M [{start}, {end})");plt.tight_layout();plt.savefig(R15/"charts/spread_timeseries_all_five_15m.png",dpi=150);plt.close()
    plt.figure(figsize=(9,5)); strict=events[events.comparison_quality=="STRICT_NATIVE_15M_BARS"]
    if len(strict): sns.ecdfplot(strict,x="duration_minutes",hue="threshold_bps")
    plt.title("Native 15m spread-event duration ECDF");plt.tight_layout();plt.savefig(R15/"charts/event_duration_ecdf_15m.png",dpi=150);plt.close()
    plt.figure(figsize=(7,6)); mat=pd.DataFrame(np.nan,index=EXCHANGES,columns=EXCHANGES)
    for r in summary.itertuples(): mat.loc[r.exchange_A,r.exchange_B]=mat.loc[r.exchange_B,r.exchange_A]=r.p95_abs_bps
    sns.heatmap(mat,annot=True,fmt=".2f",cmap="YlOrRd");plt.title("五家统一窗口：15分钟成交收盘价绝对价差P95（bps）\n非BBO；可能受低流动性、陈旧成交价和指数定义差异影响");plt.tight_layout();plt.savefig(R15/"charts/pairwise_p95_heatmap_15m.png",dpi=150);plt.close()


def _matrix_chart(matrix, value, path, title, start, end):
    sns.set_theme(style="whitegrid",font="WenQuanYi Zen Hei",rc={"axes.unicode_minus":False});plt.figure(figsize=(8,7));mat=matrix.pivot(index="long_exchange",columns="short_exchange",values=value)
    sns.heatmap(mat,annot=True,fmt=".2f",center=0,cmap="RdYlGn");plt.title(title+f"\n[{start}, {end})");plt.tight_layout();plt.savefig(path,dpi=150,bbox_inches="tight");plt.close()


def analyze_native_15m(prices, funding):
    R15.mkdir(exist_ok=True); (R15/"charts").mkdir(exist_ok=True)
    prices=prices.copy();prices["open_time"]=pd.to_datetime(prices.open_time,utc=True)
    aligned,times=_strict_trade_intersection(prices); start=times.min(); end=times.max()+BAR
    aligned.to_parquet(ROOT/"data/normalized/aligned_prices_15m.parquet",index=False)
    coverage=[]
    requested_start=max(ANALYSIS_START,prices.open_time.min()); expected=max(0,int((_closed_end(prices.close_time.max()+pd.Timedelta(milliseconds=1))-requested_start)/BAR))
    for ex in EXCHANGES:
        g=prices[(prices.exchange==ex)&(prices.price_type=="trade")]
        expected_ex=max(0,int((_closed_end(g.open_time.max()+BAR)-ANALYSIS_START)/BAR)) if len(g) else 0
        coverage.append({"analysis_scope":"PRICE_FUNDING_15M_GLOBAL_WINDOW","exchange":ex,"price_type":"trade_close_15m","first_open_time":g.open_time.min() if len(g) else None,"last_open_time":g.open_time.max() if len(g) else None,"row_count":len(g),"expected_count":expected_ex,"coverage_percent":100*len(g)/expected_ex if expected_ex else 0,"native_interval":"15m","strict_common_bar_count":len(times),"warnings":"" if len(g) else "missing_trade_15m"})
    pd.DataFrame(coverage).to_csv(R15/"exchange_coverage_15m.csv",index=False)
    pd.DataFrame([{"analysis_scope":"ALL_FIVE_TRADE_CLOSE_15M","price_15m_global_start":start,"price_15m_global_end":end,"duration_hours":(end-start).total_seconds()/3600,"strict_common_bar_count":len(times),"price_type":"trade_close_15m","intersection_rule":"timestamp present on all five exchanges"}]).to_csv(R15/"global_common_window_15m.csv",index=False)
    summary=_price_summaries(aligned,start,end);summary.to_csv(R15/"pairwise_price_summary_global_15m.csv",index=False)
    marks=_mark_subset_summary(prices);marks.to_csv(R15/"mark_15m_subsets.csv",index=False)
    event_rows=[]
    for a,b,z in _pair_frames(aligned):
        for threshold in [20,50,100,150,200]:
            for sensitivity in [False,True]:
                for ev in _events_15m(z,threshold,sensitivity):
                    event_rows.append({"pair":f"{a}/{b}","threshold_bps":threshold,**ev})
    events=pd.DataFrame(event_rows)
    events.insert(0,"base_event_id",[f"15m-{i:07d}" for i in range(1,len(events)+1)])
    events.insert(1,"analysis_scope","ALL_FIVE_TRADE_CLOSE_15M")
    # Keep the all-five strict-intersection study separate from the long-history
    # robustness base events produced by duration_analysis.  They have different
    # coverage and event semantics and must never overwrite one another.
    events.to_csv(R15/"base_spread_events_global_five_15m.csv",index=False)
    eds=events.groupby(["comparison_quality","threshold_bps"],as_index=False).agg(event_count=("base_event_id","size"),completed_count=("status",lambda x:(x=="COMPLETED").sum()),median_duration_minutes=("duration_minutes","median"),p95_duration_minutes=("duration_minutes",lambda x:x.quantile(.95)),max_duration_minutes=("duration_minutes","max"))
    eds.insert(0,"analysis_scope","ALL_FIVE_TRADE_CLOSE_15M");eds.to_csv(R15/"event_duration_summary_15m.csv",index=False)
    funding=funding.copy();funding["funding_time"]=pd.to_datetime(funding.funding_time,utc=True)
    funding_start,funding_end,_,_=_funding_bounds(funding);joint_start=max(start,funding_start);joint_end=min(end,funding_end)
    fm=_matrix_for_window(funding,joint_start,joint_end);fm.to_csv(R15/"funding_global_matrix_15m_window.csv",index=False)
    joint=_joint_events(aligned,events,funding,joint_start,joint_end);joint.to_csv(R15/"joint_strategy_events_15m.csv",index=False)
    if len(joint):
        js=[]
        for cost in [20,40,80]:
            for pair,g in joint.groupby("pair"):
                x=g[f"net_after_cost_{cost}bps"]
                js.append({"analysis_scope":"PRICE_FUNDING_15M_GLOBAL_WINDOW","pair":pair,"cost_bps":cost,"event_count":len(g),"positive_event_count":int((x>0).sum()),"win_rate":float((x>0).mean()),"total_net_bps":x.sum(),"median_net_bps":x.median(),"joint_start":joint_start,"joint_end":joint_end})
        joint_summary=pd.DataFrame(js)
    else: joint_summary=pd.DataFrame(columns=["analysis_scope","pair","cost_bps","event_count","positive_event_count","win_rate","total_net_bps","median_net_bps","joint_start","joint_end"])
    joint_summary.to_csv(R15/"joint_strategy_summary_15m.csv",index=False)
    _charts(aligned,summary,events,start,end)
    text=_write_15m_report(start,end,funding_start,funding_end,summary,joint_summary,events,aligned)
    _write_15m_html(text,summary,joint_summary)
    return {"price_start":start,"price_end":end,"joint_start":joint_start,"joint_end":joint_end,"summary":summary,"joint_summary":joint_summary}


def _fmt_top(df,col,n=3):
    return "; ".join(f"{r.pair} {getattr(r,col):.2f} bps" for r in df.sort_values(col,ascending=False).head(n).itertuples())


def _write_15m_report(start,end,funding_start,funding_end,summary,joint_summary,events,aligned):
    fm=pd.read_csv(ROOT/"reports/funding_global_matrix.csv");fc=pd.read_csv(ROOT/"reports/funding_global_common_window.csv")
    topf=fm.sort_values("cashflow_10000usd",ascending=False).head(3)
    bo=fm[(fm.long_exchange=="bitget")&(fm.short_exchange=="okx")].iloc[0];bg=fm[(fm.long_exchange=="bitget")&(fm.short_exchange=="gate")].iloc[0]
    gate_pre=[];hl_pre=[]
    for a,b,z in _pair_frames(aligned):
        if "gate" in (a,b): gate_pre.extend(z.loc[z.open_time<pd.Timestamp("2026-07-16T00:00Z"),"abs_spread_bps"].tolist())
        if "hyperliquid" in (a,b): hl_pre.extend(z.loc[z.open_time<pd.Timestamp("2026-07-19T00:00Z"),"spread_bps"].tolist())
    cost_lines=[]
    for c in [20,40,80]:
        x=joint_summary[joint_summary.cost_bps==c]
        cost_lines.append(f"- {c} bps：{int(x.event_count.sum()) if len(x) else 0} 个 pair-event，正收益 {int(x.positive_event_count.sum()) if len(x) else 0}，合计净值 {x.total_net_bps.sum() if len(x) else 0:.2f} bps。")
    text=f"""# SKHYNIX 五家统一窗口研究（15m 全历史主分析）

口径分为 `FUNDING_ONLY_GLOBAL_WINDOW`、`PRICE_FUNDING_15M_GLOBAL_WINDOW` 与 `RECENT_1M_MICROSTRUCTURE_ANALYSIS`。五家主价格榜统一使用 `ALL_FIVE_TRADE_CLOSE_15M`；`MARK_AVAILABLE_SUBSET_15M` 单列且不混榜。

## 直接回答

1. 五家统一资金窗口：`[{fc.global_start.iloc[0]}, {fc.global_end.iloc[0]})`，窗口 {fc.duration_hours.iloc[0]:.2f} 小时；入场时刻的结算事件排除，只计 `start < funding_time < end` 的真实事件。
2. 事件数：{'; '.join(f'{r.exchange}={int(r.event_count)}' for r in fc.itertuples())}；缺失检查：{'; '.join(f'{r.exchange}={r.data_quality}({r.warnings if isinstance(r.warnings,str) and r.warnings else "无警告"})' for r in fc.itertuples())}。
3. 统一资金窗口前三：{'; '.join(f'long {r.long_exchange}/short {r.short_exchange} ${r.cashflow_10000usd:.2f}' for r in topf.itertuples())}。
4. 原 long Bitget/short OKX `$578.88` 在统一资金窗口变为 **${bo.cashflow_10000usd:.2f}**。
5. 原 long Bitget/short Gate `$90.48` 在统一资金窗口变为 **${bg.cashflow_10000usd:.2f}**。
6. 排名变化主要来自旧矩阵每个 pair 的价格+资金联合窗口长度不同，而不是同一窗口内对费率做了重采样；新矩阵没有把事件摊到小时或15分钟。
7. 五家原生15分钟成交收盘价严格共同窗口：`[{start}, {end})`，共同闭合桶 {aligned.open_time.nunique():,} 个。
8. 15m trade-close P95 前三：{_fmt_top(summary,'p95_abs_bps')}。P99 前三：{_fmt_top(summary,'p99_abs_bps')}。
9. 15m 价格+资金联合策略窗口的成本结果：
{chr(10).join(cost_lines)}
10. Gate 在 7月16日前已有原生15m数据；相关组合此前绝对价差 P95/P99/最大为 {pd.Series(gate_pre).quantile(.95):.2f}/{pd.Series(gate_pre).quantile(.99):.2f}/{pd.Series(gate_pre).max():.2f} bps，因此可直接检查异常溢价是否早已存在。
11. Hyperliquid 在7月19日前已有原生15m trade 数据；相对其他交易所的有向价差中位/P95/P99为 {pd.Series(hl_pre).median():.2f}/{pd.Series(hl_pre).quantile(.95):.2f}/{pd.Series(hl_pre).quantile(.99):.2f} bps（符号随 pair 字母顺序，精确组合见 CSV）。
12. 20/40/80 bps 成本结果见第9项和 `joint_strategy_summary_15m.csv`；资金只在真实结算时记账。
13. 仅1m近期窗口成立的结论：秒近似尖峰形态、1–14分钟持续时间和局部微观 regime。Gate 1m 从 2026-07-16 18:34 开始，Hyperliquid 1m 从 2026-07-19 16:05 开始，不能用于6月10日起的五家排名。
14. 15m完整历史仍成立的结论：上述 P95/P99 排名、Gate/Hyperliquid 在各自1m起点之前的价格层级，以及统一15m联合策略成本敏感性。15m事件持续时间只能按15分钟桶解释。

## 方法与限制

- 原生15m接口响应按请求哈希缓存；没有从1m拼接、没有上采样为1m、没有未来填充；末根未闭合K线被排除。
- 严格事件遇到缺失15m桶即断开；`ALLOW_ONE_MISSING_15M_BAR` 是单列敏感性分析。
- 价格是成交K线收盘，不是历史BBO；联合策略不含盘口深度、实际滑点、容量、排队和强平规则。
- 资金统一窗口为 `[{funding_start}, {funding_end})` 的保守干净UTC边界；事件边界采用严格入场后、离场前计数。
"""
    (R15/"EXECUTIVE_SUMMARY_15M.md").write_text(text)
    return text


def _write_15m_html(text,summary,joint):
    # ``text``, ``summary`` and ``joint`` remain in the signature for backwards
    # compatibility.  The renderer intentionally reloads the persisted artifacts,
    # making the HTML reproducible and allowing missing-file degradation tests.
    return write_15m_report(ROOT, R15)


def run_fifteen_minute_analysis(start=ANALYSIS_START,end=None):
    end=pd.Timestamp.now(tz="UTC") if end is None else pd.Timestamp(end)
    funding=pd.read_parquet(ROOT/"data/normalized/funding_events.parquet")
    build_funding_global_outputs(funding)
    prices=download_native_prices_15m(start,end)
    return analyze_native_15m(prices,funding)
