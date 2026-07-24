from __future__ import annotations

import html
import itertools
from pathlib import Path

import numpy as np
import pandas as pd

from .analysis import symmetric_spread_bps
from .config import ROOT
from .live_1m import BAR, EXCHANGES, read_prices


R1 = ROOT / "reports_live_1m"
SCOPE = "LIVE_ALL_FIVE_TRADE_CLOSE_1M_STRICT_COMMON_100PCT"
THRESHOLDS = (20, 50, 100, 150, 200)


def strict_common_segments(prices: pd.DataFrame):
    """Return all contiguous all-five trade segments and the longest segment."""
    trade = prices[(prices.price_type == "trade") & prices.exchange.isin(EXCHANGES)].copy()
    trade["open_time"] = pd.to_datetime(trade.open_time, utc=True)
    wide = trade.pivot_table(index="open_time", columns="exchange", values="close", aggfunc="last")
    common = wide.dropna(subset=list(EXCHANGES)).sort_index()[list(EXCHANGES)]
    if common.empty:
        raise ValueError("No all-five 1m trade-close timestamps are available")
    group_id = common.index.to_series().diff().ne(BAR).cumsum()
    segment_rows = []
    segment_frames = {}
    for number, (_, group) in enumerate(common.groupby(group_id), start=1):
        segment_id = f"COMMON_1M_{number:04d}"
        segment_frames[segment_id] = group
        start, last = group.index.min(), group.index.max()
        expected = int((last-start)/BAR)+1
        segment_rows.append({"analysis_scope":SCOPE, "segment_id":segment_id, "start":start,
            "end_exclusive":last+BAR, "last_open_time":last, "duration_minutes":expected,
            "common_bar_count":len(group), "expected_bar_count":expected,
            "all_five_coverage_pct":100*len(group)/expected, "internal_missing_minutes":expected-len(group)})
    segments = pd.DataFrame(segment_rows)
    chosen_row = segments.sort_values(["duration_minutes", "start"], ascending=[False, False]).iloc[0]
    return common, segments, segment_frames[chosen_row.segment_id], chosen_row


def _event_counts(active: pd.Series):
    if not active.any(): return 0, 0
    groups = active.ne(active.shift(fill_value=False)).cumsum()
    lengths = active[active].groupby(groups[active]).size()
    return len(lengths), int(lengths.max())


def build_live_1m_statistics(prices: pd.DataFrame):
    common, segments, window, chosen = strict_common_segments(prices)
    start = window.index.min(); last = window.index.max(); end = last+BAR; expected = len(window)
    coverage_rows = []
    trade = prices[(prices.price_type=="trade") & prices.exchange.isin(EXCHANGES)].copy()
    trade["open_time"] = pd.to_datetime(trade.open_time, utc=True)
    for exchange in EXCHANGES:
        g = trade[(trade.exchange==exchange)&trade.open_time.between(start,last,inclusive="both")]
        present = g.open_time.nunique()
        coverage_rows.append({"analysis_scope":SCOPE, "exchange":exchange, "global_start":start,
            "global_end_exclusive":end, "last_open_time":last, "expected_minutes":expected,
            "observed_minutes":present, "coverage_pct":100*present/expected,
            "missing_minutes":expected-present, "data_quality":"OK" if present==expected else "FAIL"})
    coverage = pd.DataFrame(coverage_rows)
    exchange_rows = []
    for exchange in EXCHANGES:
        values = window[exchange]
        exchange_rows.append({"analysis_scope":SCOPE, "exchange":exchange, "global_start":start,
            "global_end_exclusive":end, "observations":len(values), "first_close":values.iloc[0],
            "last_close":values.iloc[-1], "min_close":values.min(), "median_close":values.median(),
            "max_close":values.max(), "window_return_bps":10_000*(values.iloc[-1]/values.iloc[0]-1)})
    exchange_summary = pd.DataFrame(exchange_rows)
    pair_rows = []; threshold_rows = []
    for a,b in itertools.combinations(EXCHANGES,2):
        spread = pd.Series(symmetric_spread_bps(window[a],window[b]), index=window.index)
        absolute = spread.abs(); peak_time = absolute.idxmax(); peak_spread = spread.loc[peak_time]
        pair = f"{a}/{b}"
        pair_rows.append({"analysis_scope":SCOPE, "pair":pair, "exchange_a":a, "exchange_b":b,
            "global_start":start, "global_end_exclusive":end, "observations":len(spread),
            "mean_spread_bps":spread.mean(), "median_spread_bps":spread.median(),
            "std_spread_bps":spread.std(), "p05_spread_bps":spread.quantile(.05),
            "p95_spread_bps":spread.quantile(.95), "mean_abs_spread_bps":absolute.mean(),
            "median_abs_spread_bps":absolute.median(), "p90_abs_spread_bps":absolute.quantile(.90),
            "p95_abs_spread_bps":absolute.quantile(.95), "p99_abs_spread_bps":absolute.quantile(.99),
            "max_abs_spread_bps":absolute.max(), "max_abs_spread_time":peak_time,
            "spread_at_max_bps":peak_spread, "higher_exchange_at_max":a if peak_spread>0 else b,
            "lower_exchange_at_max":b if peak_spread>0 else a})
        for threshold in THRESHOLDS:
            active = absolute >= threshold; event_count,max_run = _event_counts(active)
            threshold_rows.append({"analysis_scope":SCOPE, "pair":pair, "threshold_bps":threshold,
                "global_start":start, "global_end_exclusive":end, "observations":len(active),
                "bars_at_or_above_threshold":int(active.sum()), "share_pct":100*active.mean(),
                "event_count":event_count, "max_consecutive_minutes":max_run})
    pair_summary = pd.DataFrame(pair_rows)
    thresholds = pd.DataFrame(threshold_rows)
    window_info = pd.DataFrame([{"analysis_scope":SCOPE, "selection_rule":"LONGEST_CONTIGUOUS_ALL_FIVE_TRADE_CLOSE_1M_100PCT",
        "global_start":start, "global_end_exclusive":end, "last_open_time":last,
        "duration_minutes":expected, "all_five_common_bars":expected, "expected_bars":expected,
        "all_five_coverage_pct":100.0, "internal_missing_minutes":0,
        "available_common_segments":len(segments), "selected_segment_id":chosen.segment_id}])
    return {"window":window_info, "segments":segments, "coverage":coverage,
        "exchange":exchange_summary, "pairs":pair_summary, "thresholds":thresholds, "aligned":window.reset_index()}


def _fmt_table(frame, columns, digits=3):
    display = frame[columns].copy()
    numeric = display.select_dtypes(include=[np.number]).columns
    display[numeric] = display[numeric].round(digits)
    def cell(value):
        if pd.isna(value): return ""
        return str(value).replace("|", "\\|").replace("\n", " ")
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    rows = ["| " + " | ".join(cell(value) for value in row) + " |" for row in display.itertuples(index=False, name=None)]
    return "\n".join([header, divider, *rows])


def generate_live_1m_report(prices=None):
    prices = read_prices() if prices is None else prices
    result = build_live_1m_statistics(prices); R1.mkdir(parents=True, exist_ok=True)
    outputs = {
        "common_window_1m.csv":"window", "common_segments_1m.csv":"segments",
        "exchange_coverage_1m.csv":"coverage", "exchange_price_summary_1m.csv":"exchange",
        "pairwise_spread_summary_1m.csv":"pairs", "threshold_exceedance_1m.csv":"thresholds",
    }
    for filename,key in outputs.items(): result[key].to_csv(R1/filename,index=False)
    result["aligned"].to_parquet(R1/"aligned_trade_close_1m_100pct.parquet",index=False)
    window=result["window"].iloc[0]; pairs=result["pairs"].sort_values("p95_abs_spread_bps",ascending=False)
    thresholds20=result["thresholds"][result["thresholds"].threshold_bps==20].sort_values("share_pct",ascending=False)
    coverage=result["coverage"]
    largest_p95=pairs.iloc[0]; closest=pairs.sort_values("p95_abs_spread_bps").iloc[0]
    peak=pairs.sort_values("max_abs_spread_bps",ascending=False).iloc[0]
    count_50plus=int(result["thresholds"].loc[result["thresholds"].threshold_bps>=50,"bars_at_or_above_threshold"].sum())
    returns=result["exchange"].window_return_bps
    summary=f"""# 实时五家严格共同覆盖 1 分钟报告

**分析口径：{SCOPE}**

**统一窗口：[ {window.global_start}, {window.global_end_exclusive} )**

**最后一根 K 线 open time：{window.last_open_time}**

本报告只使用 Binance、Bitget、Gate、Hyperliquid、OKX 五家同时存在的原生 `trade close 1m`，并选择最长连续、内部无缺分钟的共同区间。窗口共 **{int(window.duration_minutes)} 分钟**，五家共同覆盖率 **100%**，内部缺失 **0 分钟**。不使用 `mark/index`，不前向填充，也不跨缺口拼接。

## 关键结果

- 五家窗口收益介于 **{returns.min():.2f} 至 {returns.max():.2f} bps**。
- 绝对价差 P95 最大的是 **{largest_p95.pair}：{largest_p95.p95_abs_spread_bps:.2f} bps**；最小的是 **{closest.pair}：{closest.p95_abs_spread_bps:.2f} bps**。
- 全窗口最大绝对价差为 **{peak.max_abs_spread_bps:.2f} bps**，出现在 **{peak.pair} / {peak.max_abs_spread_time}**。
- 50/100/150/200 bps 阈值的超限分钟合计为 **{count_50plus}**。
- 部分 pair 长时间高于 20 bps，可能反映平台报价层级或合约微观结构差异；仅凭 K 线收盘价不能认定为可成交套利。

## 五家覆盖证明

{_fmt_table(coverage,["exchange","observed_minutes","expected_minutes","coverage_pct","missing_minutes","data_quality"],2)}

## 各平台价格统计

{_fmt_table(result['exchange'],["exchange","first_close","last_close","min_close","median_close","max_close","window_return_bps"],3)}

## Pair 价差统计

正价差表示 `exchange_a` 高于 `exchange_b`；绝对价差用于跨 pair 比较。

{_fmt_table(pairs,["pair","observations","median_spread_bps","mean_abs_spread_bps","p95_abs_spread_bps","p99_abs_spread_bps","max_abs_spread_bps","max_abs_spread_time","higher_exchange_at_max"],3)}

## 20 bps 超限统计

{_fmt_table(thresholds20,["pair","bars_at_or_above_threshold","share_pct","event_count","max_consecutive_minutes"],3)}

完整的 20/50/100/150/200 bps 统计见 `threshold_exceedance_1m.csv`。本报告是实时观察窗口，不替代 15 分钟全历史主分析，也不包含资金费率。
"""
    (R1/"EXECUTIVE_SUMMARY_1M.md").write_text(summary)
    css="body{font-family:system-ui,sans-serif;max-width:1400px;margin:30px auto;padding:0 20px;color:#18202a}table{border-collapse:collapse;width:100%;font-size:13px;margin:16px 0 30px}th,td{border:1px solid #d8dee6;padding:7px;text-align:right}th:first-child,td:first-child{text-align:left}th{background:#eef3f8}.ok{padding:12px;background:#e8f7ed;border-left:5px solid #26944b}"
    html_doc=f"""<!doctype html><html><head><meta charset='utf-8'><title>实时五家严格共同覆盖 1m 报告</title><style>{css}</style></head><body>
<h1>实时五家严格共同覆盖 1 分钟报告</h1><div class='ok'><b>{html.escape(SCOPE)}</b><br>窗口：[ {window.global_start}, {window.global_end_exclusive} )；{int(window.duration_minutes)} 分钟；五家覆盖率 100%；内部缺失 0。</div>
<h2>关键结果</h2><ul><li>五家窗口收益：{returns.min():.2f} 至 {returns.max():.2f} bps</li><li>最大绝对价差 P95：{largest_p95.pair}，{largest_p95.p95_abs_spread_bps:.2f} bps</li><li>全窗口峰值：{peak.pair}，{peak.max_abs_spread_bps:.2f} bps，{peak.max_abs_spread_time}</li><li>50 bps 及以上阈值超限分钟：{count_50plus}</li></ul>
<h2>五家覆盖证明</h2>{coverage.to_html(index=False,border=0,float_format=lambda x:f'{x:.3f}')}
<h2>各平台价格统计</h2>{result['exchange'].to_html(index=False,border=0,float_format=lambda x:f'{x:.3f}')}
<h2>Pair 价差统计</h2>{pairs.to_html(index=False,border=0,float_format=lambda x:f'{x:.3f}')}
<h2>阈值超限统计</h2>{result['thresholds'].to_html(index=False,border=0,float_format=lambda x:f'{x:.3f}')}
<p>只使用五家原生 trade close 1m 严格连续交集；不填充、不跨缺口、不包含资金费率。</p></body></html>"""
    (R1/"quick_report_1m.html").write_text(html_doc)
    return result, R1/"EXECUTIVE_SUMMARY_1M.md", R1/"quick_report_1m.html"
