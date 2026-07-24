"""Decision-oriented, self-contained Chinese HTML report for the 15-minute study.

The report is deliberately a consumer of the generated CSV/Parquet artifacts.  A
missing artifact degrades one section to ``暂无数据`` instead of aborting the run.
"""
from __future__ import annotations

import base64
import html
import itertools
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
from matplotlib import font_manager

try:
    font_manager.fontManager.addfont("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")
except Exception:
    pass
plt.rcParams["font.family"] = ["WenQuanYi Zen Hei", "Unifont", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

EXCHANGES = ("binance", "bitget", "gate", "hyperliquid", "okx")
LABELS = {"binance": "Binance", "bitget": "Bitget", "gate": "Gate", "hyperliquid": "Hyperliquid", "okx": "OKX"}
GATE_START = pd.Timestamp("2026-07-16T00:00:00Z")
GATE_END = pd.Timestamp("2026-07-20T00:00:00Z")


def _read_csv(path: Path) -> pd.DataFrame:
    try:
        return pd.read_csv(path)
    except (FileNotFoundError, pd.errors.EmptyDataError, OSError, ValueError):
        return pd.DataFrame()


def _read_parquet(path: Path) -> pd.DataFrame:
    try:
        return pd.read_parquet(path)
    except (FileNotFoundError, OSError, ValueError, ImportError):
        return pd.DataFrame()


def _finite(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def format_utc(value) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "无可用数据"
    try:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return ts.floor("s").strftime("%Y-%m-%d %H:%M:%S UTC")
    except (TypeError, ValueError):
        return "无可用数据"


def format_bps(value) -> str:
    v = _finite(value)
    return f"{v:,.2f} bps" if v is not None else "无可用数据"


def format_money(value) -> str:
    v = _finite(value)
    return f"${v:,.2f}" if v is not None else "无可用数据"


def format_percent(value, ratio=False) -> str:
    v = _finite(value)
    return f"{v * (100 if ratio else 1):,.2f}%" if v is not None else "无可用数据"


def _format_count(value) -> str:
    v = _finite(value)
    return f"{int(v):,}" if v is not None else "无可用数据"


def _label(value) -> str:
    return LABELS.get(str(value).lower(), str(value).title())


def image_to_data_uri(path: Path) -> str | None:
    try:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError:
        return None


def _table(headers, rows, classes="") -> str:
    if not rows:
        return '<div class="empty">暂无数据</div>'
    head = "".join(f"<th>{html.escape(str(x))}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(x))}</td>" for x in row) + "</tr>" for row in rows)
    return f'<div class="table-wrap"><table class="{classes}"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _figure(report_dir: Path, filename: str, title: str, subtitle: str, explanation: str) -> str:
    uri = image_to_data_uri(report_dir / "charts" / filename)
    media = f'<img src="{uri}" alt="{html.escape(title)}">' if uri else '<div class="empty">暂无数据：图表文件不存在</div>'
    return f'''<figure><h3>{title}</h3><p class="chart-subtitle">{subtitle}</p>{media}
    <figcaption><strong>怎么看：</strong>{explanation}</figcaption></figure>'''


def _wide_prices(aligned: pd.DataFrame) -> pd.DataFrame:
    if aligned.empty or not {"open_time", "exchange", "close"}.issubset(aligned.columns):
        return pd.DataFrame()
    data = aligned.copy()
    data["open_time"] = pd.to_datetime(data.open_time, utc=True, errors="coerce")
    if "price_type" in data:
        data = data[data.price_type == "trade"]
    return data.pivot_table(index="open_time", columns="exchange", values="close", aggfunc="last").sort_index()


def _spread(a: pd.Series, b: pd.Series) -> pd.Series:
    return 20_000 * (a - b) / (a + b)


def _save_charts(root: Path, report_dir: Path, data: dict[str, pd.DataFrame]) -> None:
    chart_dir = report_dir / "charts"
    chart_dir.mkdir(parents=True, exist_ok=True)
    sns.set_theme(style="whitegrid", font="WenQuanYi Zen Hei", rc={"axes.unicode_minus": False})
    coverage, price, joint, events = data["coverage"], data["price"], data["joint"], data["events"]
    wide = _wide_prices(data["aligned"])

    if not coverage.empty and {"exchange", "first_open_time", "last_open_time"}.issubset(coverage):
        c = coverage.copy()
        c["first"] = pd.to_datetime(c.first_open_time, utc=True, errors="coerce")
        c["last"] = pd.to_datetime(c.last_open_time, utc=True, errors="coerce")
        c = c.dropna(subset=["first", "last"])
        if not c.empty:
            origin = c["first"].min().floor("D")
            fig, ax = plt.subplots(figsize=(11, 4.8))
            y = np.arange(len(c))
            starts = (c["first"] - origin).dt.total_seconds() / 86400
            widths = (c["last"] - c["first"]).dt.total_seconds() / 86400
            ax.barh(y, widths, left=starts, color="#2563eb", alpha=.82)
            ax.set_yticks(y, [_label(x) for x in c.exchange]); ax.invert_yaxis()
            ticks = ax.get_xticks(); ax.set_xticks(ticks, [(origin + pd.Timedelta(days=float(x))).strftime("%m-%d") for x in ticks])
            for i, row in enumerate(c.itertuples()):
                pct = _finite(getattr(row, "coverage_percent", None))
                count = getattr(row, "strict_common_bar_count", "—")
                ax.text(starts.iloc[i] + widths.iloc[i] / 2, i, f"覆盖率 {pct:.2f}% · 共同桶 {count}" if pct is not None else f"共同桶 {count}", ha="center", va="center", color="white", fontsize=9)
            ax.set_title("五家原生15分钟成交收盘价覆盖区间"); ax.set_xlabel("UTC 日期")
            fig.tight_layout(); fig.savefig(chart_dir / "data_coverage_15m.png", dpi=160, bbox_inches="tight"); plt.close(fig)

    if not price.empty and {"exchange_A", "exchange_B"}.issubset(price):
        p = price.copy()
        if "median_signed_spread_bps" not in p and not wide.empty:
            p["median_signed_spread_bps"] = [float(_spread(wide[a], wide[b]).median()) if a in wide and b in wide else np.nan for a, b in zip(p.exchange_A, p.exchange_B)]
        mat = pd.DataFrame(np.nan, index=EXCHANGES, columns=EXCHANGES)
        for row in p.itertuples():
            v = _finite(getattr(row, "median_signed_spread_bps", None))
            if v is not None:
                mat.loc[row.exchange_A, row.exchange_B] = v
                mat.loc[row.exchange_B, row.exchange_A] = -v
        if mat.notna().any().any():
            fig, ax = plt.subplots(figsize=(8, 6.5)); sns.heatmap(mat, annot=True, fmt=".2f", center=0, cmap="RdBu_r", ax=ax)
            ax.set_title("五家统一窗口：15分钟成交收盘价有向价差中位数（bps）\n正数表示纵轴交易所通常更贵")
            ax.set_xlabel("比较交易所（横轴）"); ax.set_ylabel("基准交易所（纵轴）")
            fig.tight_layout(); fig.savefig(chart_dir / "pairwise_median_signed_spread_15m.png", dpi=160, bbox_inches="tight"); plt.close(fig)

    if not wide.empty and not price.empty and "pair" in price:
        top_pairs = price.sort_values("p95_abs_bps", ascending=False).head(3).pair.tolist()
        _timeseries_chart(wide, top_pairs, chart_dir / "top_spread_pairs_15m.png", "P95最高的3个15分钟有向价差", gate_band=True)
        non_gate = ["binance/bitget", "binance/okx", "bitget/okx", "hyperliquid/okx"]
        _timeseries_chart(wide, non_gate, chart_dir / "non_gate_pairs_15m.png", "不含Gate的主要15分钟有向价差", gate_band=False)

    if not events.empty and {"threshold_bps", "duration_minutes"}.issubset(events):
        e = events.copy()
        if "comparison_quality" in e:
            e = e[e.comparison_quality == "STRICT_NATIVE_15M_BARS"]
        if not e.empty:
            fig, ax = plt.subplots(figsize=(10, 5))
            sns.ecdfplot(data=e, x="duration_minutes", hue="threshold_bps", palette="viridis", ax=ax)
            ax.set_xlabel("持续时间（分钟，15分钟粒度）"); ax.set_ylabel("累计比例")
            ax.set_title("不同触发阈值的15分钟价差事件持续时间ECDF")
            fig.tight_layout(); fig.savefig(chart_dir / "event_duration_ecdf_15m.png", dpi=160, bbox_inches="tight"); plt.close(fig)
        e = e[pd.to_numeric(e.threshold_bps, errors="coerce") == 20]
        duration = pd.to_numeric(e.duration_minutes, errors="coerce").dropna()
        if len(duration):
            fig, (ax, tail) = plt.subplots(1, 2, figsize=(11, 4.8), gridspec_kw={"width_ratios": [4, 1]})
            sns.ecdfplot(duration[duration <= 1440], ax=ax, color="#2563eb", linewidth=2)
            ax.set_xlim(0, 1440); ax.set_xlabel("持续时间（分钟，15分钟粒度）"); ax.set_ylabel("累计比例")
            tail_count = int((duration > 1440).sum()); tail.bar(["≤24小时", ">24小时"], [int((duration <= 1440).sum()), tail_count], color=["#60a5fa", "#f59e0b"])
            tail.set_ylabel("事件数"); fig.suptitle("20 bps基础事件持续时间：0—24小时主体与长尾")
            fig.tight_layout(); fig.savefig(chart_dir / "event_duration_20bps_15m.png", dpi=160, bbox_inches="tight"); plt.close(fig)

    if not joint.empty and {"pair", "cost_bps", "total_net_bps", "win_rate"}.issubset(joint):
        j = joint[joint.cost_bps.isin([20, 40, 80])].copy()
        order = j[j.cost_bps == 20].sort_values("total_net_bps", ascending=False).pair.tolist()
        if order:
            fig, ax = plt.subplots(figsize=(12, 6)); sns.barplot(data=j, x="pair", y="total_net_bps", hue="cost_bps", order=order, palette="Blues_r", ax=ax)
            ax.axhline(0, color="black", linewidth=.8); ax.tick_params(axis="x", rotation=35); ax.set_xlabel("组合"); ax.set_ylabel("累计净bps")
            ax.set_title("联合策略成本敏感性：累计净bps（历史代理）"); fig.tight_layout(); fig.savefig(chart_dir / "joint_strategy_cost_comparison_15m.png", dpi=160, bbox_inches="tight"); plt.close(fig)
            fig, ax = plt.subplots(figsize=(12, 6)); sns.barplot(data=j, x="pair", y="win_rate", hue="cost_bps", order=order, palette="Oranges_r", ax=ax)
            ax.set_ylim(0, 1); ax.tick_params(axis="x", rotation=35); ax.set_xlabel("组合"); ax.set_ylabel("正收益事件比例")
            ax.set_title("联合策略成本敏感性：正收益事件比例（历史代理）"); fig.tight_layout(); fig.savefig(chart_dir / "joint_strategy_win_rate_15m.png", dpi=160, bbox_inches="tight"); plt.close(fig)


def _timeseries_chart(wide: pd.DataFrame, pairs: list[str], path: Path, title: str, gate_band: bool) -> None:
    fig, ax = plt.subplots(figsize=(12, 5.5)); plotted = False
    for pair in pairs:
        a, b = pair.split("/")
        if a not in wide or b not in wide:
            continue
        s = _spread(wide[a], wide[b]).dropna()
        # NaNs remain gaps; matplotlib never bridges them.
        ax.plot(s.index, s, label=f"{_label(a)} / {_label(b)}", linewidth=1.05)
        if len(s):
            t = s.abs().idxmax(); ax.scatter([t], [s.loc[t]], s=26, zorder=4); ax.annotate(f"最大 {s.loc[t]:.2f}", (t, s.loc[t]), xytext=(5, 7), textcoords="offset points", fontsize=8)
        plotted = True
    ax.axhline(0, color="black", linewidth=1)
    if gate_band:
        ax.axvspan(GATE_START, GATE_END, color="#f59e0b", alpha=.15, label="Gate regime 07-16—07-20")
    if plotted: ax.legend(ncol=2, fontsize=9)
    ax.set_title(title); ax.set_ylabel("有向价差（bps）"); ax.set_xlabel("UTC时间"); ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    fig.tight_layout(); fig.savefig(path, dpi=160, bbox_inches="tight"); plt.close(fig)


def _latest_complete_8h(funding: pd.DataFrame):
    """Return the latest UTC-aligned 8h bucket containing events for all exchanges."""
    if funding.empty or not {"exchange", "funding_time", "funding_rate"}.issubset(funding):
        return None, pd.DataFrame()
    f = funding.copy(); f["funding_time"] = pd.to_datetime(f.funding_time, utc=True, errors="coerce")
    f = f.dropna(subset=["funding_time"]); f["bucket"] = f.funding_time.dt.floor("8h")
    counts = f.groupby("bucket").exchange.nunique()
    candidates = counts[counts == len(EXCHANGES)]
    if candidates.empty:
        return None, pd.DataFrame()
    start = candidates.index.max(); end = start + pd.Timedelta(hours=8)
    selected = f[(f.funding_time >= start) & (f.funding_time < end)]
    if set(selected.exchange) != set(EXCHANGES):
        return None, pd.DataFrame()
    sums = selected.groupby("exchange").funding_rate.sum()
    rows = []
    for long_ex, short_ex in itertools.permutations(EXCHANGES, 2):
        cash = (-float(sums[long_ex]) + float(sums[short_ex])) * 10_000
        rows.append({"long": long_ex, "short": short_ex, "cash": cash})
    return (start, end), pd.DataFrame(rows).sort_values("cash", ascending=False)


def build_15m_report_context(root: Path, report_dir: Path | None = None) -> dict:
    report_dir = report_dir or root / "reports_15m"
    files = {
        "coverage": report_dir / "exchange_coverage_15m.csv",
        "window": report_dir / "global_common_window_15m.csv",
        "price": report_dir / "pairwise_price_summary_global_15m.csv",
        "duration": report_dir / "event_duration_summary_20bps_15m.csv",
        "duration_basic": report_dir / "event_duration_summary_15m.csv",
        "joint": report_dir / "joint_strategy_summary_15m.csv",
        "events": report_dir / "base_spread_events_global_five_15m.csv",
        "funding_matrix": root / "reports/funding_global_matrix.csv",
        "funding_coverage": root / "reports/funding_global_common_window.csv",
    }
    data = {key: _read_csv(path) for key, path in files.items()}
    data["aligned"] = _read_parquet(root / "data/normalized/aligned_prices_15m.parquet")
    data["funding_events"] = _read_parquet(root / "data/normalized/funding_events.parquet")
    # Backfill the newly displayed signed statistic from the underlying aligned prices.
    wide = _wide_prices(data["aligned"])
    if not data["price"].empty and "median_signed_spread_bps" not in data["price"]:
        data["price"] = data["price"].copy()
        data["price"]["median_signed_spread_bps"] = [float(_spread(wide[a], wide[b]).median()) if a in wide and b in wide else np.nan for a, b in zip(data["price"].exchange_A, data["price"].exchange_B)]
    _save_charts(root, report_dir, data)
    window = data["window"].iloc[0] if not data["window"].empty else pd.Series(dtype=object)
    funding_window = data["funding_coverage"].iloc[0] if not data["funding_coverage"].empty else pd.Series(dtype=object)
    top_funding = data["funding_matrix"].sort_values("cashflow_10000usd", ascending=False).head(5) if "cashflow_10000usd" in data["funding_matrix"] else pd.DataFrame()
    top_price = data["price"].sort_values("p95_abs_bps", ascending=False).head(5) if "p95_abs_bps" in data["price"] else pd.DataFrame()
    top_joint = data["joint"][data["joint"].cost_bps == 20].sort_values("total_net_bps", ascending=False).head(1) if {"cost_bps", "total_net_bps"}.issubset(data["joint"]) else pd.DataFrame()
    common_cycle, cycle_ranking = _latest_complete_8h(data["funding_events"])
    return {"root": root, "report_dir": report_dir, "files": files, **data, "window_row": window, "funding_window_row": funding_window,
            "top_funding": top_funding, "top_price": top_price, "top_joint": top_joint, "common_cycle": common_cycle, "cycle_ranking": cycle_ranking}


def render_summary_cards(c: dict) -> str:
    w, fw = c["window_row"], c["funding_window_row"]
    topf = c["top_funding"].iloc[0] if not c["top_funding"].empty else None
    topp = c["top_price"].iloc[0] if not c["top_price"].empty else None
    topj = c["top_joint"].iloc[0] if not c["top_joint"].empty else None
    cards = [
        ("五家价格共同窗口", f'{format_utc(w.get("price_15m_global_start"))}<br>至 {format_utc(w.get("price_15m_global_end"))}'),
        ("共同15分钟桶数", _format_count(w.get("strict_common_bar_count"))),
        ("资金费率策略第一名", f'多 {_label(topf.long_exchange)} / 空 {_label(topf.short_exchange)}<br>每 $10,000 累计 {format_money(topf.cashflow_10000usd)}' if topf is not None else "无可用数据"),
        ("价差P95第一名", f'{_label(topp.exchange_A)} / {_label(topp.exchange_B)}<br>P95 = {format_bps(topp.p95_abs_bps)}' if topp is not None else "无可用数据"),
        ("20bps成本最佳联合组合", f'{html.escape(str(topj.pair))}<br>{format_bps(topj.total_net_bps)} · 胜率 {format_percent(topj.win_rate, True)} · 中位 {format_bps(topj.median_net_bps)}' if topj is not None else "无可用数据"),
        ("数据口径警告", "成交收盘价 ≠ 可执行BBO", "warning"),
    ]
    return '<div class="summary-grid">' + "".join(f'<article class="summary-card {x[2] if len(x)>2 else ""}"><span>{x[0]}</span><strong>{x[1]}</strong></article>' for x in cards) + "</div>"


def _key_conclusions(c: dict) -> str:
    items = []
    if not c["top_funding"].empty:
        r = c["top_funding"].iloc[0]; items.append(("强结论", "strong", f'统一历史窗口内，资金费率最佳方向是多 {_label(r.long_exchange)}、空 {_label(r.short_exchange)}，每 $10,000 累计 {format_money(r.cashflow_10000usd)}。'))
    if not c["top_price"].empty:
        names = "、".join(_label(x) for x in c["top_price"].head(3).pair); gate_count = int(c["top_price"].head(5).pair.str.contains("gate", case=False).sum())
        items.append(("强结论", "strong", f"P95最高的三个组合为 {names}；它们描述价差尾部大小，不代表平均收益。"))
        items.append(("初步结论", "prelim", f"P95前五名中有 {gate_count} 个涉及Gate，Gate是高价差的重要来源；但陈旧成交价或结构性基差也可能造成这一现象。"))
    j = c["joint"]
    if not j.empty and {"cost_bps", "total_net_bps"}.issubset(j):
        p20 = int((j[j.cost_bps == 20].total_net_bps > 0).sum()); p40 = int((j[j.cost_bps == 40].total_net_bps > 0).sum())
        items.append(("初步结论", "prelim", f"累计净值为正的组合从20bps成本下 {p20} 个降至40bps下 {p40} 个，结果对交易成本明显敏感。"))
    items.append(("无法确认", "unknown", "现有数据没有历史BBO、盘口深度和实际成交路径，当前证据不足以支持实盘可获利结论。"))
    return '<ul class="conclusions">' + "".join(f'<li><span class="tag {css}">{tag}</span>{text}</li>' for tag, css, text in items) + "</ul>"


def render_funding_section(c: dict) -> str:
    rows = []
    for i, r in enumerate(c["top_funding"].itertuples(), 1):
        rows.append([i, _label(r.long_exchange), _label(r.short_exchange), format_money(r.cashflow_10000usd), format_money(r.cashflow_per_day_10000usd), format_percent(r.simple_apr_not_compounded, True), "正常" if str(r.data_quality) == "OK" else "需检查"])
    window = c["funding_window_row"]
    top3 = "；".join(f'{i}. 多{_label(r.long_exchange)}/空{_label(r.short_exchange)} {format_money(r.cashflow_10000usd)}（每日{format_money(r.cashflow_per_day_10000usd)}）' for i, r in enumerate(c["top_funding"].head(3).itertuples(), 1)) or "暂无数据"
    cycle = c["cycle_ranking"]; cycle_rows = []
    if not cycle.empty:
        shown = pd.concat([cycle.head(5), cycle.tail(3)]).drop_duplicates()
        for rank, (idx, r) in enumerate(shown.iterrows(), 1):
            actual_rank = cycle.index.get_loc(idx) + 1
            cycle_rows.append([actual_rank, _label(r["long"]), _label(r["short"]), format_money(r.cash)])
    cycle_title = f'{format_utc(c["common_cycle"][0])} 至 {format_utc(c["common_cycle"][1])}' if c["common_cycle"] else "当前数据不足以自动确认"
    return f'''<section id="funding"><h2>3. 纯资金费率策略</h2><p class="scope">历史统计 · 使用纯资金费率统一窗口，不依赖价格窗口</p>
    {_figure(c["root"] / "reports", "funding_global_common_window_matrix.png", "五家统一资金窗口：每 $10,000 累计资金费率现金流", "纵轴做多，横轴做空；正数表示该方向收到资金费率；所有组合使用完全相同时间窗口。", f'统一窗口为 {format_utc(window.get("global_start"))} 至 {format_utc(window.get("global_end"))}。前三名：{top3}。空OKX通常靠前，原因是本窗口内OKX累计支付费率较高；这是历史结算差，而非未来保证。不同结算频率会改变累计事件贡献，报告直接累计真实结算事件、不做摊平。结果不含手续费、滑点、借贷、保证金和换汇成本。')}
    <h3>历史统一窗口前5名</h3>{_table(["排名","做多","做空","累计收益","每日收益","简单年化","数据状态"], rows)}
    <h3>上一完整共同周期</h3><p>自动识别的最近UTC对齐8小时窗口：<strong>{cycle_title}</strong>。窗口内直接累计各交易所真实资金事件。</p>
    {_table(["排名","做多","做空","周期现金流 / $10,000"], cycle_rows)}</section>'''


def render_spread_section(c: dict) -> str:
    rows = []
    for i, r in enumerate(c["top_price"].itertuples(), 1):
        rows.append([i, f"{_label(r.exchange_A)} / {_label(r.exchange_B)}", format_bps(getattr(r, "median_signed_spread_bps", None)), format_bps(r.p95_abs_bps), format_bps(r.p99_abs_bps), format_bps(r.max_abs_bps), format_percent(r.percent_A_higher)])
    top3 = "、".join(f'{_label(r.exchange_A)}/{_label(r.exchange_B)}（{format_bps(r.p95_abs_bps)}）' for r in c["top_price"].head(3).itertuples()) or "暂无数据"
    return f'''<section id="spread"><h2>4. 15分钟价格价差</h2><p class="scope">历史统计 · 五家严格共同时间戳</p>
    {_figure(c["report_dir"], "pairwise_p95_heatmap_15m.png", "五家统一窗口：15分钟成交收盘价绝对价差P95", "非BBO；可能受低流动性、陈旧成交价和指数定义差异影响。", f'P95表示95%的观测绝对价差不超过该值，最高三组为 {top3}。它是分布尾部统计，不是平均收益。Gate相关组合普遍偏高，可能来自成交稀疏、陈旧收盘价或结构性定价差异；若价差长期保持同方向，它更像结构性基差，而不是可快速收敛的套利。')}
    {_figure(c["report_dir"], "pairwise_median_signed_spread_15m.png", "五家统一窗口：有向价差中位数", "正数表示纵轴交易所通常较贵；负数表示横轴交易所通常较贵；色阶以0为中心。", "这张图提供方向：正值时通常考虑在纵轴一侧做空、横轴一侧做多。绝对价差热力图只能说明距离，不能决定多空方向。")}
    <h3>价格组合前5名</h3>{_table(["排名","组合","中位有向价差","P95绝对价差","P99绝对价差","最大绝对价差","A高于B时间占比"], rows)}</section>'''


def render_timeseries_section(c: dict) -> str:
    price = c["price"]
    gate = price[price.pair.str.contains("gate", case=False)] if not price.empty and "pair" in price else pd.DataFrame()
    nongate = price[~price.pair.str.contains("gate", case=False)] if not price.empty and "pair" in price else pd.DataFrame()
    gate_text = f'Gate相关组合P95中位值为 {format_bps(gate.p95_abs_bps.median())}。橙色区间用于判断高价差是否集中在2026-07-16至07-20；区间外若仍长期同号，更支持结构性基差解释。' if not gate.empty else "暂无足够数据判断Gate阶段。"
    ng_text = f'排除Gate后，主要组合P95的中位值为 {format_bps(nongate.p95_abs_bps.median())}。持续同号表示长期价格层级；多次穿越零线则显示更明显的收敛与反转。' if not nongate.empty else "暂无足够数据。"
    return f'''<section id="timeseries"><h2>5. 价差时序</h2>
    {_figure(c["report_dir"], "top_spread_pairs_15m.png", "P95最高的3个组合", "零线清晰；数据缺口不连线；标注最大偏离及Gate regime。", gate_text)}
    {_figure(c["report_dir"], "non_gate_pairs_15m.png", "不含Gate的主要组合", "Binance/Bitget、Binance/OKX、Bitget/OKX、Hyperliquid/OKX。", ng_text)}</section>'''


def render_duration_section(c: dict) -> str:
    d = c["duration"]
    row = pd.Series(dtype=object)
    if not d.empty:
        q = d[(d.get("group_type") == "ALL") & (d.get("group_value") == "ALL_OBSERVED")]
        row = q.iloc[0] if not q.empty else d.iloc[0]
    else:
        b = c["duration_basic"]
        q = b[(b.get("threshold_bps") == 20) & (b.get("comparison_quality") == "STRICT_NATIVE_15M_BARS")] if not b.empty else pd.DataFrame()
        row = q.iloc[0] if not q.empty else pd.Series(dtype=object)
    events = c["events"]
    e20 = events[(events.get("threshold_bps") == 20) & (events.get("comparison_quality") == "STRICT_NATIVE_15M_BARS")] if not events.empty else pd.DataFrame()
    gate_med = e20[e20.pair.str.contains("gate", case=False)].duration_minutes.median() if not e20.empty else None
    non_med = e20[~e20.pair.str.contains("gate", case=False)].duration_minutes.median() if not e20.empty else None
    total = row.get("event_count_total", row.get("event_count", None))
    p90 = row.get("p90_minutes", None); p95 = row.get("p95_minutes", row.get("p95_duration_minutes", None)); longest = row.get("max_observed_minutes", row.get("max_duration_minutes", None))
    ratios = [("1小时", row.get("ratio_le_60m")), ("4小时", row.get("ratio_le_240m")), ("24小时", None)]
    if _finite(row.get("ratio_gt_1440m")) is not None: ratios[2] = ("24小时", 1-float(row.ratio_gt_1440m))
    ratio_text = "、".join(f'{label}内结束 {format_percent(v, True)}' for label, v in ratios)
    explanation = f'20bps唯一基础事件共 {int(total):,} 个；中位 {format_bps(row.get("median_minutes")).replace(" bps", "分钟")}，P90 {format_bps(p90).replace(" bps", "分钟")}，P95 {format_bps(p95).replace(" bps", "分钟")}；{ratio_text}；最长 {format_bps(longest).replace(" bps", "分钟")}。Gate相关事件中位 {format_bps(gate_med).replace(" bps", "分钟")}、非Gate {format_bps(non_med).replace(" bps", "分钟")}。OPEN或右删失事件按已观测时长统计并单独标记，不能假设其在数据边界收敛。所有时长都是15分钟的倍数。' if _finite(total) is not None else "暂无足够事件数据。"
    return f'''<section id="duration"><h2>6. 价差事件持续时间</h2><p class="scope">历史统计 · 唯一基础事件，不使用策略场景重复行</p>
    {_figure(c["report_dir"], "event_duration_ecdf_15m.png", "多阈值事件持续时间ECDF", "15分钟数据只能按15分钟桶解释。", "曲线越靠左，事件越快结束；不同阈值用于比较持续性。")}
    {_figure(c["report_dir"], "event_duration_20bps_15m.png", "20bps事件：0—24小时主体与长尾", "超过24小时单列；持续时间为15分钟倍数。", explanation)}</section>'''


def render_strategy_section(c: dict) -> str:
    j = c["joint"]
    tables = []
    positive_text = []
    for cost in [20, 40, 80]:
        q = j[j.cost_bps == cost].sort_values("total_net_bps", ascending=False).head(5) if not j.empty and "cost_bps" in j else pd.DataFrame()
        rows = [[i, r.pair, format_bps(r.total_net_bps), format_percent(r.win_rate, True), format_bps(r.median_net_bps), int(r.event_count)] for i, r in enumerate(q.itertuples(), 1)]
        tables.append(f'<h3>{cost}bps成本前5名</h3>' + _table(["排名","组合","累计净bps","胜率","单事件中位净bps","事件数"], rows))
        positives = j[(j.cost_bps == cost) & (j.total_net_bps > 0)].pair.tolist() if not j.empty else []
        positive_text.append(f'{cost}bps：{"、".join(positives) if positives else "无组合累计为正"}')
    top20 = j[j.cost_bps == 20].sort_values("total_net_bps", ascending=False).head(3) if not j.empty else pd.DataFrame()
    top_text = "、".join(f'{r.pair}（{format_bps(r.total_net_bps)}）' for r in top20.itertuples()) or "暂无数据"
    explanation = f'20bps前三名：{top_text}。{"；".join(positive_text)}。累计正而单事件中位数为负，通常表示少数大事件覆盖了多数亏损事件。事件可能重叠，因此total_net_bps不能视为单账户收益率。当前价格收益使用事件峰值减离场价差，包含事件发生后的峰值信息；当前收益可能使用事件峰值信息，不能视为无偏策略回测。'
    return f'''<section id="strategy"><h2>7. 价格＋资金费率联合策略</h2><div class="proxy"><strong>历史代理策略，不是实盘回测。</strong></div>
    {_figure(c["report_dir"], "joint_strategy_cost_comparison_15m.png", "联合策略成本比较：累计净bps", "每个组合比较20/40/80bps成本。", explanation)}
    {_figure(c["report_dir"], "joint_strategy_win_rate_15m.png", "联合策略成本比较：正收益事件比例", "胜率与累计净值分图展示，避免混淆量纲。", "成本越高，正收益事件比例通常越低。该指标仍不包含盘口成交约束。")}
    {''.join(tables)}</section>'''


def render_limitations_section(c: dict) -> str:
    file_rows = [[str(path.relative_to(c["root"])), "存在" if path.exists() else "缺失（对应区块已降级）"] for path in c["files"].values()]
    return f'''<section id="usage"><h2>8. 该如何使用这份报告</h2><div class="three-col">
    <article><h3>可以确认</h3><ul><li>五家15分钟价格的长期相对层级</li><li>统一窗口的历史资金费率差</li><li>哪些组合对成本最敏感</li></ul></article>
    <article class="muted"><h3>不能确认</h3><ul><li>当时真实买一卖一价与可成交容量</li><li>滑点、排队、单腿成交风险</li><li>保证金与强平风险</li></ul></article>
    <article><h3>下一步</h3><ul><li>实时BBO与五档盘口</li><li>1秒同步快照</li><li>按$1,000/$5,000/$10,000计算实际吃单成本</li></ul></article></div>
    <details><summary>技术明细</summary><p>价格为五家严格共同时间戳上的原生15分钟成交收盘价；资金为真实结算事件；联合策略使用基础事件代理。测试状态以仓库最近一次pytest结果为准。</p>
    {_table(["数据文件","状态"], file_rows)}</details></section>'''


def _coverage_section(c: dict) -> str:
    rows = []
    coverage = c["coverage"]
    for r in coverage.itertuples():
        start = format_utc(r.first_open_time); status = "完整" if _finite(r.coverage_percent) == 100 else "需检查"
        rows.append([_label(r.exchange), start, format_utc(r.last_open_time), format_percent(r.coverage_percent), status])
    all_june10 = bool(len(coverage) == 5 and pd.to_datetime(coverage.first_open_time, utc=True, errors="coerce").dt.date.eq(pd.Timestamp("2026-06-10").date()).all()) if not coverage.empty else False
    w = c["window_row"]
    explanation = f'五家{("都" if all_june10 else "并非都")}从2026-06-10开始有原生15分钟数据。严格共同窗口为 {format_utc(w.get("price_15m_global_start"))} 至 {format_utc(w.get("price_15m_global_end"))}，共 {int(w.get("strict_common_bar_count")):,} 桶。15分钟原生历史覆盖较长且五家重叠，因此比覆盖较短的1分钟数据更适合全历史横向比较；但它不能回答桶内BBO、成交顺序、盘口深度、滑点和排队。' if _finite(w.get("strict_common_bar_count")) is not None else "共同窗口暂无数据；无法确认五家可比性。"
    return f'''<section id="coverage"><h2>2. 数据是否可比</h2>
    {_figure(c["report_dir"], "data_coverage_15m.png", "五家原生15分钟数据覆盖", "时间区间、覆盖率与严格共同桶数量。", explanation)}
    {_table(["交易所","起始时间","结束时间","覆盖率","数据状态"], rows)}</section>'''


CSS = """
:root{--ink:#162033;--muted:#64748b;--blue:#1d4ed8;--line:#dbe3ef;--panel:#fff;--bg:#f4f7fb}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.68 system-ui,-apple-system,"Noto Sans SC",sans-serif}main,header{max-width:1280px;margin:auto;padding:24px}header{padding-top:46px}h1{font-size:clamp(30px,4vw,48px);line-height:1.18;margin:0 0 8px}h2{font-size:27px;margin:0 0 12px}h3{font-size:18px}.subtitle{font-size:19px;color:var(--muted)}.meta{display:flex;flex-wrap:wrap;gap:8px;margin:20px 0}.meta span,.scope{background:#e8eefb;color:#29416c;border-radius:999px;padding:5px 10px}.warning,.proxy{background:#fff3cd;border-left:5px solid #f59e0b;padding:14px 16px;border-radius:8px;margin:16px 0}.nav{position:sticky;top:0;z-index:5;background:rgba(255,255,255,.96);border-block:1px solid var(--line);overflow:auto;white-space:nowrap;padding:10px calc((100% - 1232px)/2)}.nav a{color:#334155;text-decoration:none;margin:0 10px}.nav a:hover{color:var(--blue)}section{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:clamp(18px,3vw,34px);margin:22px 0;box-shadow:0 8px 25px rgba(15,23,42,.04)}.summary-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.summary-card{background:#f8fafc;border:1px solid var(--line);border-radius:13px;padding:17px;min-height:125px}.summary-card span{display:block;color:var(--muted);font-size:13px;margin-bottom:12px}.summary-card strong{display:block;font-size:21px;line-height:1.4}.summary-card.warning{margin:0;background:#fff3cd}.conclusions{list-style:none;padding:0}.conclusions li{padding:10px 0;border-bottom:1px solid #edf2f7}.tag{display:inline-block;width:70px;text-align:center;border-radius:999px;margin-right:10px;font-size:12px}.tag.strong{background:#dcfce7;color:#166534}.tag.prelim{background:#ffedd5;color:#9a3412}.tag.unknown{background:#e5e7eb;color:#4b5563}figure{margin:26px 0;border-top:1px solid var(--line);padding-top:18px}figure img{display:block;width:100%;height:auto;margin:14px auto;border-radius:8px}.chart-subtitle,figcaption{color:var(--muted)}figcaption{background:#f8fafc;padding:13px;border-radius:8px}.table-wrap{overflow-x:auto}table{border-collapse:collapse;width:100%;min-width:680px}th,td{text-align:left;padding:10px;border-bottom:1px solid var(--line);white-space:nowrap}th{background:#f1f5f9}.three-col{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}.three-col article{border:1px solid var(--line);border-radius:12px;padding:16px}.muted,.empty{background:#f1f5f9;color:#64748b}.empty{padding:24px;text-align:center;border-radius:8px}details{margin-top:24px;border-top:1px solid var(--line);padding-top:18px}summary{cursor:pointer;font-weight:700}@media(max-width:780px){header,main{padding:16px}.summary-grid,.three-col{grid-template-columns:1fr}.summary-card{min-height:auto}.nav{padding:9px 6px}section{border-radius:10px}h2{font-size:23px}}
"""


def write_15m_report(root: Path, report_dir: Path | None = None) -> Path:
    root, report_dir = Path(root), Path(report_dir or Path(root) / "reports_15m")
    report_dir.mkdir(parents=True, exist_ok=True)
    c = build_15m_report_context(root, report_dir)
    w, fw = c["window_row"], c["funding_window_row"]
    dates = [x for x in [w.get("price_15m_global_start"), w.get("price_15m_global_end"), fw.get("global_start"), fw.get("global_end")] if x is not None]
    cutoff = max((pd.Timestamp(x) for x in dates), default=None)
    doc = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>SKHYNIX 五家交易所 15分钟历史研究</title><style>{CSS}</style></head><body>
    <header><h1>SKHYNIX 五家交易所 15分钟历史研究</h1><p class="subtitle">原生15分钟成交收盘价＋真实资金费率结算事件</p>
    <div class="meta"><span>价格区间：{format_utc(w.get("price_15m_global_start"))} — {format_utc(w.get("price_15m_global_end"))}</span><span>五家价格共同窗口：{format_utc(w.get("price_15m_global_start"))} — {format_utc(w.get("price_15m_global_end"))}</span><span>五家资金费率共同窗口：{format_utc(fw.get("global_start"))} — {format_utc(fw.get("global_end"))}</span><span>数据截止：{format_utc(cutoff)}</span><span>共同15分钟桶：{_format_count(w.get("strict_common_bar_count"))}</span></div>
    <div class="warning"><strong>重要警告：</strong>历史15分钟成交收盘价不是当时可执行BBO，不代表可成交利润。</div></header>
    <nav class="nav"><a href="#summary">一页结论</a><a href="#coverage">数据可比性</a><a href="#funding">资金费率</a><a href="#spread">价格价差</a><a href="#timeseries">价差时序</a><a href="#duration">事件持续</a><a href="#strategy">联合策略</a><a href="#usage">如何使用</a></nav>
    <main><section id="summary"><h2>1. 一页结论</h2>{render_summary_cards(c)}<h3>最重要的结论</h3>{_key_conclusions(c)}</section>
    {_coverage_section(c)}{render_funding_section(c)}{render_spread_section(c)}{render_timeseries_section(c)}{render_duration_section(c)}{render_strategy_section(c)}{render_limitations_section(c)}</main></body></html>'''
    # Never leak pandas' non-finite display tokens into decision-facing output.
    doc = doc.replace(">nan<", ">无可用数据<").replace(">NaN<", ">无可用数据<").replace(">inf<", ">无可用数据<")
    path = report_dir / "quick_report_15m.html"
    path.write_text(doc, encoding="utf-8")
    return path
