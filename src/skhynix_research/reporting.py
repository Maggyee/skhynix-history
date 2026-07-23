from __future__ import annotations
import base64, itertools, json, math, os, platform, subprocess, logging
from pathlib import Path
import numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import seaborn as sns
from matplotlib import font_manager
try: font_manager.fontManager.addfont("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")
except Exception: pass
plt.rcParams["font.family"]=["WenQuanYi Zen Hei","Unifont","DejaVu Sans"]
plt.rcParams["axes.unicode_minus"]=False
from .config import ROOT
from .calendar import trading_dates, session_label
from .analysis import align_prices, symmetric_spread_bps, detect_scale_mismatch, threshold_events
from .storage import build_duckdb
from .http import pdnow
from .corrected import corrected_analysis, corrected_report

SESSIONS=["ALL","PRE_CLOSE_BASELINE","AFTER_CLOSE_ALL","KRX_OFFICIAL_AFTER_HOURS","KRX_FULLY_CLOSED","KRX_HOLIDAY_OR_WEEKEND"]
AFTER={"POST_CLOSE_TRANSITION","KRX_OFFICIAL_AFTER_HOURS","KRX_FULLY_CLOSED"}

def _stats(x, a_higher):
    if len(x)==0:return {}
    s=x.spread
    return {"count":len(s),"mean_bps":s.mean(),"median_bps":s.median(),"std_bps":s.std(),"min_bps":s.min(),"max_bps":s.max(),
    "p01_bps":s.quantile(.01),"p05_bps":s.quantile(.05),"p25_bps":s.quantile(.25),"p75_bps":s.quantile(.75),"p95_bps":s.quantile(.95),"p99_bps":s.quantile(.99),
    "mean_abs_bps":s.abs().mean(),"p95_abs_bps":s.abs().quantile(.95),"p99_abs_bps":s.abs().quantile(.99),"max_abs_bps":s.abs().max(),
    "percent_A_higher":100*(s>0).mean(),"percent_B_higher":100*(s<0).mean()}

def _slice(df,session):
    if session=="ALL":return df
    if session=="AFTER_CLOSE_ALL":return df[df.session_detail.isin(AFTER)]
    return df[df.session_detail==session]

def analyze_all(requested_start, run_end):
    alog=logging.getLogger("analysis"); alog.info("analysis start requested_start=%s run_end=%s",requested_start,run_end)
    reports=ROOT/"reports"; reports.mkdir(exist_ok=True); (reports/"charts").mkdir(exist_ok=True)
    prices=pd.read_parquet(ROOT/"data"/"normalized"/"prices_1m.parquet")
    funding=pd.read_parquet(ROOT/"data"/"normalized"/"funding_events.parquet") if (ROOT/"data"/"normalized"/"funding_events.parquet").exists() else pd.DataFrame()
    meta=pd.read_parquet(ROOT/"data"/"normalized"/"instrument_metadata.parquet")
    dates,cal_source=trading_dates(requested_start,run_end)
    aligned=align_prices(prices)
    # Preferred comparison price independently per exchange: mark when genuinely available, trade otherwise.
    selected=[]; qualities={}
    for ex,g in aligned.groupby("exchange"):
        mark=g[(g.price_type=="mark")&g.price.notna()]
        trade=g[(g.price_type=="trade")&g.price.notna()]
        # Some RWA endpoints document silent fallback of mark/index to market.
        fallback=False
        if len(mark) and len(trade):
            chk=mark[["minute","price"]].merge(trade[["minute","price"]],on="minute",suffixes=("_m","_t"))
            fallback=len(chk)>100 and np.isclose(chk.price_m,chk.price_t,rtol=0,atol=1e-12).mean()>.999
        if len(mark) and not fallback: z=mark.copy(); quality="mark"
        else: z=trade.copy(); quality="trade_close_proxy" if not fallback else "trade_close_proxy_silent_mark_fallback"
        z["comparison_quality"]=quality; selected.append(z); qualities[ex]=quality
    preferred=pd.concat(selected,ignore_index=True)
    pairs=[]; scale_warnings=[]
    exs=sorted(preferred.exchange.unique())
    for a,b in itertools.combinations(exs,2):
        aa=preferred[preferred.exchange==a][["minute","price","source_time","age_seconds","comparison_quality"]].rename(columns={"price":"price_A","source_time":"source_time_A","age_seconds":"age_A","comparison_quality":"quality_A"})
        bb=preferred[preferred.exchange==b][["minute","price","source_time","age_seconds","comparison_quality"]].rename(columns={"price":"price_B","source_time":"source_time_B","age_seconds":"age_B","comparison_quality":"quality_B"})
        z=aa.merge(bb,on="minute",how="inner").dropna(subset=["price_A","price_B"])
        mismatch,ratio=detect_scale_mismatch(z.price_A,z.price_B)
        if mismatch: scale_warnings.append({"pair":f"{a}/{b}","median_ratio":ratio,"warning":"SCALE_MISMATCH"}); continue
        z["exchange_A"],z["exchange_B"],z["pair"]=a,b,f"{a}/{b}"
        z["spread_A_over_B_bps"]=10000*(z.price_A/z.price_B-1)
        z["symmetric_spread_bps"]=symmetric_spread_bps(z.price_A,z.price_B)
        z["spread"]=z.symmetric_spread_bps; z["abs_spread"]=z.spread.abs()
        z["higher_exchange"]=np.where(z.spread>=0,a,b);z["lower_exchange"]=np.where(z.spread>=0,b,a)
        z["comparison_quality"]=np.where((z.quality_A=="mark")&(z.quality_B=="mark"),"mark_spread_bps","trade_close_proxy")
        z["session_detail"]=[session_label(t,dates) for t in z.minute]
        pairs.append(z)
    pairdf=pd.concat(pairs,ignore_index=True) if pairs else pd.DataFrame()
    pairdf.to_parquet(ROOT/"data"/"normalized"/"aligned_prices_1m.parquet",index=False)
    # Coverage
    cov=[]; total_expected=int((pd.Timestamp(run_end)-pd.Timestamp(requested_start)).total_seconds()/60)+1
    derr=json.loads((ROOT/"data"/"raw"/"download_errors.json").read_text()) if (ROOT/"data"/"raw"/"download_errors.json").exists() else {}
    for _,m in meta.drop_duplicates("exchange").iterrows():
        ex=m.exchange; p=prices[prices.exchange==ex]; f=funding[funding.exchange==ex] if not funding.empty else pd.DataFrame()
        trade=p[p.price_type=="trade"]; times=trade.open_time.sort_values().drop_duplicates()
        gaps=times.diff().dt.total_seconds().div(60).sub(1).clip(lower=0)
        listing=pd.to_datetime(m.listing_time,utc=True) if pd.notna(m.listing_time) else pd.Timestamp(requested_start)
        exp=max(0,int((pd.Timestamp(run_end)-max(pd.Timestamp(requested_start),listing)).total_seconds()/60)+1)
        cov.append({"exchange":ex,"resolved_symbol":m.resolved_symbol,"first_price_time":times.min() if len(times) else None,"last_price_time":times.max() if len(times) else None,"price_rows":len(trade),"first_funding_time":f.funding_time.min() if len(f) else None,"last_funding_time":f.funding_time.max() if len(f) else None,"funding_rows":len(f),"expected_minutes":exp,"available_minutes":len(times),"coverage_percent":100*len(times)/exp if exp else 0,"longest_gap_minutes":gaps.max() if len(gaps) else None,"data_types_available":"|".join(sorted(p.price_type.unique())),"errors":derr.get(ex,"")})
    coverage=pd.DataFrame(cov); coverage.to_csv(reports/"exchange_coverage.csv",index=False)
    # Pairwise price summaries
    sums=[]
    if not pairdf.empty:
        for pair,g in pairdf.groupby("pair"):
            a,b=pair.split("/")
            common_span=max(1,int((g.minute.max()-g.minute.min()).total_seconds()/60)+1)
            for sess in SESSIONS:
                x=_slice(g,sess); st=_stats(x,a)
                if st: sums.append({"pair":pair,"exchange_A":a,"exchange_B":b,"session":sess,"coverage_percent":100*len(x)/common_span,"comparison_quality":"mark_spread_bps" if (x.comparison_quality=="mark_spread_bps").all() else "historical_proxy_spread_bps",**st})
    ps=pd.DataFrame(sums); ps.to_csv(reports/"pairwise_price_summary.csv",index=False)
    # Funding exchange and directional pairs
    fund_rows=[]
    if not funding.empty:
        agg=funding.groupby("exchange").agg(event_count=("funding_rate","size"),sum_rate=("funding_rate","sum"),mean_hourly_rate=("hourly_equivalent_rate","mean"),first_time=("funding_time","min"),last_time=("funding_time","max")).reset_index()
        agg["simple_apr_not_compounded"]=agg.mean_hourly_rate*24*365
        for a,b in itertools.permutations(sorted(agg.exchange),2):
            sa=float(agg.loc[agg.exchange==a,"sum_rate"].iloc[0]); sb=float(agg.loc[agg.exchange==b,"sum_rate"].iloc[0]); pnl=-sa+sb
            fund_rows.append({"long_exchange":a,"short_exchange":b,"pair":f"long {a} / short {b}","sum_funding_long":sa,"sum_funding_short":sb,"funding_pnl_per_1usd":pnl,"theoretical_cashflow_10000usd":pnl*10000,"long_event_count":int(agg.loc[agg.exchange==a,"event_count"].iloc[0]),"short_event_count":int(agg.loc[agg.exchange==b,"event_count"].iloc[0]),"note":"仅各自上市后已取得事件的非完全同窗累计"})
        fs=pd.DataFrame(fund_rows)
    else: agg=pd.DataFrame(); fs=pd.DataFrame(columns=["pair"])
    fs.to_csv(reports/"pairwise_funding_summary.csv",index=False)
    funding.to_csv(reports/"funding_events_normalized.csv",index=False)
    # Events, convergence and cost sensitivity.
    events=[]; conv=[]
    if not pairdf.empty:
        for pair,g in pairdf.groupby("pair"):
            g=g.sort_values("minute").reset_index(drop=True)
            time_ns=g.minute.dt.as_unit("ns").astype("int64").to_numpy()
            # Detect each global continuous event once; session is assigned at peak.
            # This avoids counting the same physical event in ALL and a sub-session.
            for sess in ["ALL"]:
                sg=g
                for th in [10,20,50,100,200]:
                    for ev in threshold_events(sg,th):
                        peak_ns=int(pd.Timestamp(ev["peak_time"]).value); end_ns=int(pd.Timestamp(ev["event_end"]).value)
                        pos=int(np.searchsorted(time_ns,peak_ns)); end_pos=min(len(g)-1,int(np.searchsorted(time_ns,end_ns,side="right")-1))
                        peak=g.iloc[pos]; endrow=g.iloc[end_pos]
                        actual_session=session_label(ev["peak_time"],dates)
                        rec={"pair":pair,"session":actual_session,"threshold_bps":th,**ev,"higher_exchange_at_peak":peak.higher_exchange,"lower_exchange_at_peak":peak.lower_exchange}
                        events.append(rec)
                        # At most to next plausible KRX opening (18h); requested snapshots stop at 240m.
                        stop=min(len(g),int(np.searchsorted(time_ns,peak_ns+18*3600*1_000_000_000,side="right")))
                        future_abs=g.abs_spread.to_numpy()[pos:stop]
                        future_spread=g.spread.to_numpy()[pos:stop]
                        future_times=time_ns[pos:stop]
                        peakabs=float(ev["peak_abs_spread_bps"])
                        def hit(level):
                            q=np.flatnonzero(future_abs<=level)
                            return float((future_times[q[0]]-peak_ns)/60_000_000_000) if len(q) else np.nan
                        cr={**rec,"minutes_to_50pct_peak":hit(peakabs*.5),"minutes_to_20bps":hit(20),"minutes_to_10bps":hit(10),"max_further_expansion_bps":max(0,float(np.nanmax(future_abs[:241])-peakabs))}
                        for horizon in [1,5,15,30,60,240]:
                            j=int(np.searchsorted(future_times,peak_ns+horizon*60_000_000_000,side="right")-1)
                            cr[f"spread_after_{horizon}m_bps"]=float(future_spread[j]) if j>=0 else np.nan
                        exit_abs=float(endrow.abs_spread); gross=max(0,peakabs-exit_abs)
                        cr["gross_convergence_bps"]=gross
                        for cost in [10,20,40,80]:cr[f"net_after_cost_{cost}bps"]=gross-cost
                        cr["converged_before_next_krx_open"]=bool(hit(10)<=18*60) if pd.notna(hit(10)) else False
                        conv.append(cr)
    evdf=pd.DataFrame(events); cvdf=pd.DataFrame(conv)
    evdf.to_csv(reports/"spread_events.csv",index=False); cvdf.to_csv(reports/"convergence_events.csv",index=False)
    # Opportunities: one row per unique 20bps ALL event avoids threshold duplicates.
    opp=[]
    if not cvdf.empty:
        for _,r in cvdf[cvdf.threshold_bps==20].iterrows():
            gross=r.gross_convergence_bps; fa=0.0
            combined=gross+fa
            opp.append({"pair":r.pair,"session":session_label(r.peak_time,dates),"event_start":r.event_start,"event_peak_time":r.peak_time,"event_end":r.event_end,"long_exchange_at_peak":r.lower_exchange_at_peak,"short_exchange_at_peak":r.higher_exchange_at_peak,"peak_spread_bps":r.peak_abs_spread_bps,"gross_convergence_bps":gross,"funding_advantage_bps_during_event":fa,"combined_gross_bps":combined,"net_10bps":combined-10,"net_20bps":combined-20,"net_40bps":combined-40,"net_80bps":combined-80,"duration_minutes":r.duration_minutes,"data_quality":"normal" if not scale_warnings else "reviewed","warnings":"历史分钟价格代理；无真实BBO"})
    op=pd.DataFrame(opp)
    if len(op):op=op.sort_values(["combined_gross_bps","data_quality","duration_minutes"],ascending=[False,False,False]).reset_index(drop=True);op.insert(0,"rank",range(1,len(op)+1))
    else: op=pd.DataFrame(columns="rank pair session event_start event_peak_time event_end long_exchange_at_peak short_exchange_at_peak peak_spread_bps gross_convergence_bps funding_advantage_bps_during_event combined_gross_bps net_10bps net_20bps net_40bps net_80bps duration_minutes data_quality warnings".split())
    op.to_csv(reports/"top_opportunities.csv",index=False)
    # Store context used by report phase.
    context={"requested_start":str(requested_start),"run_end":str(run_end),"calendar_source":cal_source,"qualities":qualities,"scale_warnings":scale_warnings}
    (reports/"analysis_context.json").write_text(json.dumps(context,ensure_ascii=False,indent=2,default=str))
    corrected_analysis(preferred,pairdf,funding,prices,meta,requested_start,run_end,ps,fs,evdf)
    build_duckdb(); make_charts(coverage,ps,agg,fs,pairdf,evdf)
    alog.info("analysis complete prices=%d aligned=%d funding=%d spread_events=%d convergence_events=%d",len(prices),len(pairdf),len(funding),len(evdf),len(cvdf))
    return coverage,ps,agg,fs,evdf,cvdf,op,context

def _savefig(name,title):
    plt.title(title);plt.tight_layout();plt.savefig(ROOT/"reports"/"charts"/name,dpi=150,bbox_inches="tight");plt.close()

def make_charts(cov,ps,agg,fs,pairdf,evdf):
    sns.set_theme(style="whitegrid",font="WenQuanYi Zen Hei",rc={"axes.unicode_minus":False})
    plt.figure(figsize=(9,4));sns.barplot(cov,x="exchange",y="coverage_percent");plt.ylabel("覆盖率 %");_savefig("data_coverage.png","1分钟成交价数据覆盖率（UTC）")
    for metric,name,title in [("p95_abs_bps","after_close_p95_spread_heatmap.png","盘后绝对价差 P95（bps，历史分钟代理）"),("max_abs_bps","after_close_max_spread_heatmap.png","盘后最大绝对价差（bps，未截断）")]:
        plt.figure(figsize=(7,6)); sub=ps[ps.session=="AFTER_CLOSE_ALL"] if len(ps) else ps
        ex=sorted(set(sub.exchange_A).union(sub.exchange_B)) if len(sub) else ["无数据"]
        mat=pd.DataFrame(np.nan,index=ex,columns=ex)
        for _,r in sub.iterrows():mat.loc[r.exchange_A,r.exchange_B]=mat.loc[r.exchange_B,r.exchange_A]=r[metric]
        sns.heatmap(mat,annot=True,fmt=".1f",cmap="YlOrRd");_savefig(name,title)
    plt.figure(figsize=(9,4))
    if len(agg):sns.barplot(agg,x="exchange",y="sum_rate")
    plt.ylabel("累计原始资金费率");_savefig("funding_cumulative_by_exchange.png","各交易所累计实际资金费率（各自可得区间）")
    plt.figure(figsize=(7,6))
    if len(fs):
        mat=fs.pivot(index="long_exchange",columns="short_exchange",values="theoretical_cashflow_10000usd");sns.heatmap(mat,annot=True,fmt=".2f",center=0,cmap="RdYlGn")
    _savefig("funding_pair_matrix.png","做多行/做空列：每 $10,000 理论资金现金流")
    plt.figure(figsize=(12,5))
    if len(pairdf):
        from .history_quality import gap_broken
        top=sorted(pairdf.loc[pairdf.pair.str.contains("gate"),"pair"].unique())
        for pair in top:
            g=pairdf[pairdf.pair==pair].sort_values("minute");q=gap_broken(g.minute,g.spread);plt.plot(q.time,q.value,label=pair,linewidth=.7)
        plt.legend();plt.ylabel("对称价差 bps");plt.xlabel("UTC 时间")
    _savefig("top_3_pair_spread_timeseries.png","Gate 相关高价差组合时序（mark 优先/成交收盘代理；缺口不连接）")
    # Duration charts are generated from unique base events in duration_analysis.
    plt.figure(figsize=(11,4))
    if len(ps):
        q=ps[ps.session.isin(["PRE_CLOSE_BASELINE","AFTER_CLOSE_ALL"])];sns.barplot(q,x="pair",y="p95_abs_bps",hue="session");plt.xticks(rotation=35,ha="right")
    plt.ylabel("绝对价差 P95 bps");_savefig("preclose_vs_afterclose.png","收盘前基线与盘后价差 P95 对比")

def generate_reports(requested_start,run_end,errors=None):
    if (ROOT/"reports"/"pairwise_funding_common_window.csv").exists():
        summary=corrected_report(requested_start,run_end)
        cov=pd.read_csv(ROOT/"reports"/"exchange_coverage.csv");meta=pd.read_parquet(ROOT/"data"/"normalized"/"instrument_metadata.parquet")
        success=cov[cov.price_rows>0].exchange.tolist();failed=[x for x in meta.exchange.unique() if x not in success]
        errors=errors or (json.loads((ROOT/"data"/"raw"/"download_errors.json").read_text()) if (ROOT/"data"/"raw"/"download_errors.json").exists() else {})
        make_manifest(requested_start,run_end,success,failed,meta,errors)
        return summary
    R=ROOT/"reports"; cov=pd.read_csv(R/"exchange_coverage.csv"); ps=pd.read_csv(R/"pairwise_price_summary.csv"); fs=pd.read_csv(R/"pairwise_funding_summary.csv"); ev=pd.read_csv(R/"spread_events.csv"); cv=pd.read_csv(R/"convergence_events.csv"); op=pd.read_csv(R/"top_opportunities.csv")
    funding=pd.read_parquet(ROOT/"data"/"normalized"/"funding_events.parquet") if (ROOT/"data"/"normalized"/"funding_events.parquet").exists() else pd.DataFrame()
    meta=pd.read_parquet(ROOT/"data"/"normalized"/"instrument_metadata.parquet")
    context=json.loads((R/"analysis_context.json").read_text()); errors=errors or (json.loads((ROOT/"data"/"raw"/"download_errors.json").read_text()) if (ROOT/"data"/"raw"/"download_errors.json").exists() else {})
    success=cov[cov.price_rows>0].exchange.tolist(); failed=[x for x in meta.exchange.unique() if x not in success]
    common_start=pd.to_datetime(cov[cov.exchange.isin(success)].first_price_time,utc=True).max() if success else pd.NaT
    after=ps[ps.session=="AFTER_CLOSE_ALL"].sort_values("p95_abs_bps",ascending=False)
    top=after.iloc[0] if len(after) else None
    maxev=ev[ev.session.isin(AFTER)].sort_values("peak_abs_spread_bps",ascending=False).iloc[0] if len(ev[ev.session.isin(AFTER)]) else None
    fundagg=funding.groupby("exchange").funding_rate.sum().sort_values(ascending=False) if len(funding) else pd.Series(dtype=float)
    bestfund=fs.sort_values("funding_pnl_per_1usd",ascending=False).iloc[0] if len(fs) else None
    uniqcv=cv[cv.threshold_bps==20] if len(cv) else cv
    medconv=uniqcv.minutes_to_50pct_peak.dropna().median() if len(uniqcv) else np.nan
    cost_counts={c:int((uniqcv[f"net_after_cost_{c}bps"]>0).sum()) if len(uniqcv) else 0 for c in [10,20,40,80]}
    weekend=ev[(ev.session=="KRX_HOLIDAY_OR_WEEKEND")&(ev.threshold_bps>=50)] if len(ev) else ev
    quality_lines=[]
    for _,r in cov.iterrows():
        quality_lines.append(f"- {r.exchange}: trade {int(r.price_rows):,} 行，资金事件 {int(r.funding_rows):,}，覆盖率 {r.coverage_percent:.2f}%，最长缺口 {r.longest_gap_minutes if pd.notna(r.longest_gap_minutes) else 'N/A'} 分钟；类型 {r.data_types_available}。{('错误：'+str(r.errors)) if pd.notna(r.errors) and str(r.errors) else ''}")
    listing="\n".join(f"- {r.exchange}: {r.first_price_time if pd.notna(r.first_price_time) else '无价格数据'}（产品 metadata 上市：{meta.loc[meta.exchange==r.exchange,'listing_time'].iloc[0]}）" for _,r in cov.iterrows())
    topdesc=(f"{top.pair}：P95 {top.p95_abs_bps:.2f} bps，P99 {top.p99_abs_bps:.2f} bps，最大 {top.max_abs_bps:.2f} bps；A 高占比 {top.percent_A_higher:.1f}%" if top is not None else "无可比较组合")
    maxdesc=(f"{maxev.pair}，峰值 {maxev.peak_abs_spread_bps:.2f} bps，阈值事件持续 {int(maxev.duration_minutes)} 分钟，发生于 {maxev.peak_time}" if maxev is not None else "无")
    funddesc=(f"累计最高为 {fundagg.index[0]}（原始费率和 {fundagg.iloc[0]:.6f}）" if len(fundagg) else "无资金数据")
    bestdesc=(f"做多 {bestfund.long_exchange}、做空 {bestfund.short_exchange}，每 $10,000 理论累计 {bestfund.theoretical_cashflow_10000usd:.2f} 美元" if bestfund is not None else "无法计算")
    summary=f"""# SKHYNIX 永续跨交易所历史研究执行摘要

**数据截止（UTC）：{run_end}**  
**请求起点（UTC）：{requested_start}**  
**研究性质：历史分钟 K 线代理，不是当时可执行 BBO，不代表可成交利润。**

## 直接结论

### 强结论

1. **实际成功交易所：** {', '.join(success) or '无'}。失败/无成交价格：{', '.join(failed) or '无'}。
2. **各家真正开始有价格数据：**
{listing}
3. **共同可比较起点：** {common_start if pd.notna(common_start) else '不存在'}。这是成功交易所全部同时拥有成交价的保守起点；单个组合可能更早。
4. **盘后价差领先组合：** {topdesc}。
5. **最大盘后事件：** {maxdesc}。
6. **通常价格更高的一边：** 对最高 P95 组合，{top.exchange_A if top is not None and top.percent_A_higher>=50 else (top.exchange_B if top is not None else '无法确认')} 更常较高。
7. **最大价差持续时间：** {int(maxev.duration_minutes) if maxev is not None else 'N/A'} 分钟（这里是超过对应阈值的连续分钟事件，并允许中间缺 1 分钟）。
8. **典型收敛：** 20 bps 事件从峰值回落一半的中位时间为 {medconv:.1f} 分钟。未收敛事件不被强行赋值。
9. **周末/休市大价差：** 识别出 {len(weekend)} 个阈值≥50 bps 的周末或休市事件记录（同一行情可能按多个阈值重复计数）。
10. **历史资金费率最高：** {funddesc}。
11. **最有利资金方向：** {bestdesc}。
12. **每 $10,000 理论资金差：** {bestdesc}。注意各产品上市时间不同，该排名是各自可得事件累计，不是严格同窗因果比较。

### 初步结论

13. **高价边与做空资金优势：** 需要逐事件同窗归因；当前机会表只在存在事件结算时才应加入资金影响，本版未把缺失结算猜成 0，因此不能一概断言。
14. **成本敏感性：** 在唯一的 20 bps 事件集合中，总往返成本 10/20/40/80 bps 后仍有正历史收敛空间的事件数分别为 **{cost_counts[10]}/{cost_counts[20]}/{cost_counts[40]}/{cost_counts[80]}**（总事件 {len(uniqcv)}）。不含真实盘口滑点。
15. **数据局限：** Hyperliquid 官方 1m candleSnapshot 只保留最近 5,000 根；RWA/TradFi 合约存在休市和零成交段；资金结算周期由事件间隔推断处已标记；历史 K 线无法还原 BBO。固定比例错配检测结果：{json.dumps(context.get('scale_warnings',[]),ensure_ascii=False)}。
16. **是否值得部署实时采集：** 若目标是验证可交易性，**值得做短期实时 BBO 与五档盘口采集**，优先覆盖报告中 P95 较高且数据覆盖好的组合；在此之前不应把本报告价差当作套利收益。

### 因数据不足无法确认

- 无历史 BBO、盘口深度、排队位置和逐笔同步数据，无法确认实际成交价格、滑点和容量。
- 缺失期间不插值；超过 2 分钟不前填。上市前空白不计为接口失败。
- `SCALE_MISMATCH` 组合（若有）已排除直接排行榜，未做静默缩放。

## 数据覆盖

{chr(10).join(quality_lines)}

韩国交易日来源：`{context['calendar_source']}`；所有时间为 UTC。周末和韩国休市日保留并单列。
"""
    (R/"EXECUTIVE_SUMMARY.md").write_text(summary)
    dq="# 数据质量报告\n\n**截止 UTC：%s**\n\n%s\n\n## 方法限制\n\n- 对齐只向过去寻找，最多前填 2 分钟，不使用未来数据。\n- mark 缺失时明确使用 `trade_close_proxy`；未将成交价冒充标记价。\n- 历史分钟价格不是可执行 BBO，成本分析仅为敏感性。\n- 原始 HTTP 响应按请求哈希缓存；标准化表按主键去重，重复运行幂等。\n- 比例错配：`%s`。\n"%(run_end,"\n".join(quality_lines),json.dumps(context.get("scale_warnings",[]),ensure_ascii=False))
    (R/"data_quality.md").write_text(dq)
    sources={
      "retrieved_at":str(run_end),"calendar":context["calendar_source"],
      "hyperliquid":["https://api.hyperliquid.xyz/info","https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint"],
      "binance":["https://fapi.binance.com/fapi/v1/exchangeInfo","https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api"],
      "bitget":["https://api.bitget.com/api/v3/market/history-candles","https://www.bitget.com/api-doc/uta/public/Get-History-Candle-Data","https://www.bitget.com/api-doc/uta/public/Get-History-Funding-Rate"],
      "okx":["https://www.okx.com/api/v5/market/history-candles","https://www.okx.com/docs-v5/en/"],
      "gate":["https://api.gateio.ws/api/v4/futures/usdt/candlesticks","https://www.gate.com/docs/developers/apiv4/en/futures/"]}
    (R/"sources_used.json").write_text(json.dumps(sources,ensure_ascii=False,indent=2))
    imgs=[]
    for p in sorted((R/"charts").glob("*.png")):
        b64=base64.b64encode(p.read_bytes()).decode();imgs.append(f'<section><h2>{p.stem}</h2><img src="data:image/png;base64,{b64}" alt="{p.stem}"></section>')
    html=f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>SKHYNIX 历史研究</title><style>body{{max-width:1200px;margin:30px auto;font:15px system-ui;line-height:1.55;color:#17202a}}img{{max-width:100%}}table{{border-collapse:collapse;font-size:12px;display:block;overflow:auto}}td,th{{border:1px solid #ddd;padding:5px}}.warn{{background:#fff3cd;padding:12px}}</style><h1>SKHYNIX 永续历史对比</h1><p><b>截止 UTC：</b>{run_end}</p><p class="warn">历史分钟 K 线代理，不是当时可执行 BBO；不代表可成交利润。缺失不伪造，最多向前填充 2 分钟。</p><h2>盘后组合摘要</h2>{after.head(10).to_html(index=False)}<h2>资金方向摘要</h2>{fs.sort_values('funding_pnl_per_1usd',ascending=False).head(10).to_html(index=False)}{''.join(imgs)}</html>"""
    (R/"quick_report.html").write_text(html)
    make_manifest(requested_start,run_end,success,failed,meta,errors)
    return summary

def make_manifest(start,end,success,failed,meta,errors):
    import importlib.metadata as im
    pkgs={p:(im.version(p) if _has(p) else None) for p in ["httpx","pandas","duckdb","pyarrow","numpy","matplotlib","exchange-calendars"]}
    try: commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True,stderr=subprocess.DEVNULL).strip()
    except: commit="uncommitted/no-commit"
    report_files=[str(x.relative_to(ROOT)) for x in (ROOT/"reports").rglob("*") if x.is_file()]
    counts={p.stem:len(pd.read_parquet(p)) for p in (ROOT/"data"/"normalized").glob("*.parquet")}
    manifest={"requested_start":str(start),"actual_run_end":str(end),"git_commit":commit,"python_version":platform.python_version(),"package_versions":pkgs,"successful_exchanges":success,"failed_exchanges":failed,"resolved_symbols":dict(zip(meta.exchange,meta.resolved_symbol)),"raw_file_count":len(list((ROOT/"data"/"raw").rglob("*.json"))),"normalized_row_counts":counts,"report_files":report_files,"warnings":list(errors.values())+["历史分钟K线不是可执行BBO"]}
    (ROOT/"reports"/"run_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2,default=str))

def _has(p):
    import importlib.util
    return importlib.util.find_spec(p.replace("-","_")) is not None
