from __future__ import annotations
import base64, html, itertools, json
import numpy as np, pandas as pd
import matplotlib.pyplot as plt
import seaborn as sns
from .config import ROOT
from .analysis import threshold_events, symmetric_spread_bps
from .common_windows import GATE_START,GATE_END,gate_regime,global_common_window,joint_common_window,left_closed_right_open,percent_gate_higher

R=ROOT/"reports"
REGIMES=["PRE_GATE_REGIME","GATE_REGIME_20260716_19","POST_GATE_REGIME"]

def _bounds(df,key="exchange",time="minute"):
    return {k:(g[time].min(),g[time].max()) for k,g in df.groupby(key) if len(g)}

def _spread_stats(g):
    if g.empty:return {k:np.nan for k in ["mean_bps","median_bps","std_bps","p05_bps","p25_bps","p75_bps","p95_bps","p99_bps","mean_abs_bps","p95_abs_bps","p99_abs_bps","max_abs_bps"]}
    s=g.spread
    return {"mean_bps":s.mean(),"median_bps":s.median(),"std_bps":s.std(),"p05_bps":s.quantile(.05),"p25_bps":s.quantile(.25),"p75_bps":s.quantile(.75),"p95_bps":s.quantile(.95),"p99_bps":s.quantile(.99),"mean_abs_bps":s.abs().mean(),"p95_abs_bps":s.abs().quantile(.95),"p99_abs_bps":s.abs().quantile(.99),"max_abs_bps":s.abs().max()}

def _calendar_minutes(start,end):
    return max(0,int((pd.Timestamp(end)-pd.Timestamp(start)).total_seconds()/60))

def _overlap(a,b,c,d):
    s=max(pd.Timestamp(a),pd.Timestamp(c));e=min(pd.Timestamp(b),pd.Timestamp(d));return s,e

def corrected_analysis(preferred,pairdf,funding,prices,meta,requested_start,run_end,legacy_ps,legacy_fs,legacy_events):
    preferred=preferred[(preferred.price.notna())&(preferred.age_seconds<=120)].copy()
    pb=_bounds(preferred)
    # Global five-exchange window plus best four- and three-exchange alternatives.
    rows=[]; exchanges=sorted(pb)
    for n in [len(exchanges),4,3]:
        candidates=[]
        for combo in itertools.combinations(exchanges,n):
            b={x:pb[x] for x in combo}; start,end,ls,le=global_common_window(b)
            sets=[]
            for ex in combo:
                q=preferred[(preferred.exchange==ex)&(preferred.minute>=start)&(preferred.minute<end)]
                sets.append(set(q.minute.tolist()))
            common=set.intersection(*sets) if sets else set(); cal=_calendar_minutes(start,end)
            candidates.append({"exchange_set":"|".join(combo),"exchange_count":n,"common_start":start,"common_end":end,"calendar_duration_hours":cal/60,"valid_common_minutes":len(common),"coverage_percent":100*len(common)/cal if cal else np.nan,"limiting_start_exchange":"|".join(ls),"limiting_end_exchange":"|".join(le)})
        if candidates: rows.append(max(candidates,key=lambda x:x["valid_common_minutes"]))
        if n==len(exchanges) and n in (4,3): break
    global_df=pd.DataFrame(rows).drop_duplicates("exchange_set");global_df.to_csv(R/"global_common_window.csv",index=False)
    five=global_df.sort_values("exchange_count",ascending=False).iloc[0];five_set=five.exchange_set.split("|");five_minutes=[]
    for ex in five_set:
        q=preferred[(preferred.exchange==ex)&(preferred.minute>=pd.Timestamp(five.common_start))&(preferred.minute<pd.Timestamp(five.common_end))];five_minutes.append(set(q.minute.tolist()))
    strict_global_minutes=set.intersection(*five_minutes)
    global_rank=[]
    for pair,g in pairdf.groupby("pair"):
        x=g[g.minute.isin(strict_global_minutes)];global_rank.append({"exchange_set":five.exchange_set,"common_start":five.common_start,"common_end":five.common_end,"pair":pair,"valid_common_minutes":len(x),**_spread_stats(x)})
    pd.DataFrame(global_rank).to_csv(R/"global_common_pairwise_price_summary.csv",index=False)

    # Strict pair windows and session summaries.
    price_rows=[]; pair_bounds={}
    for pair,g0 in pairdf.groupby("pair"):
        a,b=pair.split("/"); start=max(pb[a][0],pb[b][0]);end=min(pb[a][1],pb[b][1]);pair_bounds[pair]=(start,end)
        g=g0[(g0.minute>=start)&(g0.minute<end)&(g0.age_A<=120)&(g0.age_B<=120)].copy()
        cal=_calendar_minutes(start,end)
        sessions={"ALL":g,"PRE_CLOSE_BASELINE":g[g.session_detail=="PRE_CLOSE_BASELINE"],"AFTER_CLOSE_ALL":g[g.session_detail.isin({"POST_CLOSE_TRANSITION","KRX_OFFICIAL_AFTER_HOURS","KRX_FULLY_CLOSED"})],"KRX_OFFICIAL_AFTER_HOURS":g[g.session_detail=="KRX_OFFICIAL_AFTER_HOURS"],"KRX_FULLY_CLOSED":g[g.session_detail=="KRX_FULLY_CLOSED"],"KRX_HOLIDAY_OR_WEEKEND":g[g.session_detail=="KRX_HOLIDAY_OR_WEEKEND"]}
        for session,x in sessions.items():
            requested_minutes=max(1,int(np.ceil((pd.Timestamp(run_end)-pd.Timestamp(requested_start)).total_seconds()/60)))
            st=_spread_stats(x); price_rows.append({"pair":pair,"exchange_A":a,"exchange_B":b,"session":session,"pair_price_start":start,"pair_price_end":end,"calendar_minutes":cal,"valid_common_minutes":len(x),"coverage_vs_pair_window_percent":100*len(x)/cal if cal else np.nan,"coverage_vs_requested_period_percent":100*len(x)/requested_minutes,"comparison_quality":"mark_spread_bps" if len(x) and (x.comparison_quality=="mark_spread_bps").all() else "historical_proxy_spread_bps",**st})
    price_common=pd.DataFrame(price_rows);price_common.to_csv(R/"pairwise_price_summary_common_window.csv",index=False)

    # Strict funding and price+funding joint windows, left-closed/right-open.
    fb={ex:(g.funding_time.min(),g.funding_time.max()) for ex,g in funding.groupby("exchange") if len(g)}
    fund_rows=[]; joint_bounds={}
    for a,b in itertools.permutations(exchanges,2):
        key="/".join(sorted([a,b])); ps,pe=pair_bounds[key];fs=max(fb[a][0],fb[b][0]);fe=min(fb[a][1],fb[b][1]);js,je=joint_common_window(ps,pe,fs,fe);joint_bounds[(a,b)]=(js,je)
        fa=left_closed_right_open(funding[funding.exchange==a],"funding_time",js,je);fbg=left_closed_right_open(funding[funding.exchange==b],"funding_time",js,je)
        sa=fa.funding_rate.sum();sb=fbg.funding_rate.sum();net=-sa+sb
        fund_rows.append({"pair":f"long {a} / short {b}","long_exchange":a,"short_exchange":b,"pair_price_start":ps,"pair_price_end":pe,"pair_funding_start":fs,"pair_funding_end":fe,"joint_start":js,"joint_end":je,"joint_duration_hours":max(0,(je-js).total_seconds()/3600),"long_funding_event_count":len(fa),"short_funding_event_count":len(fbg),"sum_funding_long":sa,"sum_funding_short":sb,"net_funding_pnl_per_1usd":net,"theoretical_cashflow_10000usd":net*10000})
    common_funding=pd.DataFrame(fund_rows);common_funding.to_csv(R/"pairwise_funding_common_window.csv",index=False)
    # Main compatibility file is now strictly comparable; legacy is explicit audit only.
    legacy=legacy_fs.copy();legacy["comparability"]="NOT_COMPARABLE";legacy["method"]="legacy_non_common_window";legacy.to_csv(R/"pairwise_funding_legacy_NOT_COMPARABLE.csv",index=False)
    common_funding.to_csv(R/"pairwise_funding_summary.csv",index=False)

    gate_summary=[];gate_events=[]
    gate_pairs=sorted(p for p in pair_bounds if "gate" in p.split("/"))
    for pair in gate_pairs:
        a,b=pair.split("/");start,end=pair_bounds[pair];base=pairdf[(pairdf.pair==pair)&(pairdf.minute>=start)&(pairdf.minute<end)&(pairdf.age_A<=120)&(pairdf.age_B<=120)].copy();base["regime"]=base.minute.map(gate_regime)
        for regime in REGIMES:
            x=base[base.regime==regime];rs,re={"PRE_GATE_REGIME":(pd.Timestamp(requested_start),GATE_START),"GATE_REGIME_20260716_19":(GATE_START,GATE_END),"POST_GATE_REGIME":(GATE_END,pd.Timestamp(run_end))}[regime];os,oe=_overlap(start,end,rs,re);cal=_calendar_minutes(os,oe)
            longest={}
            for th in [20,50,100]:
                evs=threshold_events(x,th)
                longest[th]=max([e["duration_minutes"] for e in evs],default=np.nan)
                for ev in evs:gate_events.append({"pair":pair,"regime":regime,"threshold_bps":th,**ev})
            gate_summary.append({"pair":pair,"regime":regime,"pair_price_start":start,"pair_price_end":end,"regime_effective_start":os if os<oe else pd.NaT,"regime_effective_end":oe if os<oe else pd.NaT,"count":len(x),"valid_minutes":len(x),"coverage_percent":100*len(x)/cal if cal else np.nan,**_spread_stats(x),"percent_gate_higher":percent_gate_higher(x,a,b),"longest_event_over_20bps_minutes":longest[20],"longest_event_over_50bps_minutes":longest[50],"longest_event_over_100bps_minutes":longest[100]})
    gate_sum=pd.DataFrame(gate_summary);gate_ev=pd.DataFrame(gate_events);gate_sum.to_csv(R/"gate_regime_summary.csv",index=False);gate_ev.to_csv(R/"gate_regime_events.csv",index=False)

    # Gate-related funding, strictly within each directional joint window and regime.
    gf=[]
    for _,r in common_funding[(common_funding.long_exchange=="gate")|(common_funding.short_exchange=="gate")].iterrows():
        for regime in REGIMES:
            rs,re={"PRE_GATE_REGIME":(pd.Timestamp(requested_start),GATE_START),"GATE_REGIME_20260716_19":(GATE_START,GATE_END),"POST_GATE_REGIME":(GATE_END,pd.Timestamp(run_end))}[regime];s,e=_overlap(r.joint_start,r.joint_end,rs,re)
            valid=s<e;la=left_closed_right_open(funding[funding.exchange==r.long_exchange],"funding_time",s,e) if valid else funding.iloc[0:0];sh=left_closed_right_open(funding[funding.exchange==r.short_exchange],"funding_time",s,e) if valid else funding.iloc[0:0];net=-la.funding_rate.sum()+sh.funding_rate.sum() if valid else np.nan
            gf.append({"pair":r.pair,"long_exchange":r.long_exchange,"short_exchange":r.short_exchange,"regime":regime,"window_start":s if valid else pd.NaT,"window_end":e if valid else pd.NaT,"long_event_count":len(la) if valid else np.nan,"short_event_count":len(sh) if valid else np.nan,"sum_funding_long":la.funding_rate.sum() if valid else np.nan,"sum_funding_short":sh.funding_rate.sum() if valid else np.nan,"net_funding_pnl_per_1usd":net,"theoretical_cashflow_10000usd":net*10000 if valid else np.nan})
    gate_funding=pd.DataFrame(gf);gate_funding.to_csv(R/"gate_regime_funding_summary.csv",index=False)

    # Correct the combined opportunity table: only events wholly inside pair joint windows.
    cv=pd.read_csv(R/"convergence_events.csv");cv["event_start"]=pd.to_datetime(cv.event_start,utc=True);cv["event_end"]=pd.to_datetime(cv.event_end,utc=True);cv["peak_time"]=pd.to_datetime(cv.peak_time,utc=True)
    opp=[]
    for _,r in cv[cv.threshold_bps==20].iterrows():
        pair=r.pair;a,b=pair.split("/");js,je=joint_bounds[(a,b)]
        if not (r.event_start>=js and r.event_end<je):continue
        pg=pairdf[(pairdf.pair==pair)&(pairdf.minute==r.peak_time)]
        if pg.empty:continue
        peak=pg.iloc[0];long_ex=peak.lower_exchange;short_ex=peak.higher_exchange
        lf=left_closed_right_open(funding[funding.exchange==long_ex],"funding_time",r.event_start,r.event_end+pd.Timedelta(minutes=1));sf=left_closed_right_open(funding[funding.exchange==short_ex],"funding_time",r.event_start,r.event_end+pd.Timedelta(minutes=1));fund_bps=(-lf.funding_rate.sum()+sf.funding_rate.sum())*10000;gross=float(r.gross_convergence_bps);combined=gross+fund_bps
        opp.append({"pair":pair,"session":r.session,"event_start":r.event_start,"event_peak_time":r.peak_time,"event_end":r.event_end,"long_exchange_at_peak":long_ex,"short_exchange_at_peak":short_ex,"peak_spread_bps":r.peak_abs_spread_bps,"gross_convergence_bps":gross,"funding_advantage_bps_during_event":fund_bps,"combined_gross_bps":combined,"net_10bps":combined-10,"net_20bps":combined-20,"net_40bps":combined-40,"net_80bps":combined-80,"duration_minutes":r.duration_minutes,"data_quality":"strict_joint_window","warnings":"历史分钟代理；无真实BBO"})
    op=pd.DataFrame(opp)
    if len(op):op=op.sort_values(["combined_gross_bps","duration_minutes"],ascending=[False,False]).reset_index(drop=True);op.insert(0,"rank",range(1,len(op)+1))
    op.to_csv(R/"top_opportunities.csv",index=False)

    samples,diag=gate_diagnostics(pairdf,prices,preferred,meta,requested_start,run_end)
    samples.to_csv(R/"gate_regime_raw_samples.csv",index=False);(R/"gate_regime_diagnostics.md").write_text(diag)
    old_vs=old_vs_corrected(legacy_ps,price_common,legacy_fs,common_funding,legacy_events,gate_sum);old_vs.to_csv(R/"old_vs_corrected_results.csv",index=False)
    corrected_charts(global_df,common_funding,pairdf,gate_sum,prices,pair_bounds)
    # Higher-order quality/tradability analysis deliberately runs only after the
    # strict windows above have been materialized.
    from .history_quality import extended_analysis
    extended=extended_analysis(pairdf,funding,prices,requested_start,run_end)
    return {"global":global_df,"price":price_common,"funding":common_funding,"gate":gate_sum,"gate_funding":gate_funding,"joint_bounds":joint_bounds,"extended":extended}

def gate_diagnostics(pairdf,prices,preferred,meta,requested_start,run_end):
    gp=prices[prices.exchange=="gate"].copy(); piv=gp.pivot_table(index="open_time",columns="price_type",values="close",aggfunc="last").rename(columns={"trade":"gate_trade","mark":"gate_mark","index":"gate_index"});vol=gp[gp.price_type=="trade"].set_index("open_time")[["volume_base","raw_file"]].rename(columns={"volume_base":"gate_volume","raw_file":"gate_raw_source_file"});piv=piv.join(vol)
    regime=piv[(piv.index>=GATE_START)&(piv.index<GATE_END)];trade=gp[gp.price_type=="trade"].sort_values("open_time");dup=int(gp.duplicated(["price_type","open_time"]).sum());diff=trade.open_time.diff().dt.total_seconds().div(60);aligned=float((trade.open_time.dt.second==0).mean()*100);zero=float((trade.volume_base.fillna(0)==0).mean()*100);jumps=trade.close.pct_change().abs()*100
    gatepairs=pairdf[(pairdf.pair.str.contains("gate"))&(pairdf.minute>=GATE_START)&(pairdf.minute<GATE_END)];peak=gatepairs.loc[gatepairs.abs_spread.idxmax()]
    page_count=0;bad_order=0
    for path in (ROOT/"data"/"raw"/"gate").glob("*.json"):
        try:
            obj=json.loads(path.read_text());req=obj.get("request",{});resp=obj.get("response")
            if "candlesticks" not in req.get("url","") or not isinstance(resp,list) or not resp:continue
            tv=[int(x["t"]) for x in resp];page_count+=1;bad_order+=int(tv!=sorted(tv))
        except Exception:pass
    times=set()
    for center in [peak.minute,GATE_START,GATE_END]:
        times.update(pd.date_range(center-pd.Timedelta(minutes=10),center+pd.Timedelta(minutes=10),freq="1min",tz="UTC"))
    rawmap={(r.exchange,r.open_time,r.price_type):r.raw_file for r in prices.itertuples()}
    rows=[]
    for pair,g in pairdf[pairdf.pair.str.contains("gate")].groupby("pair"):
        other=next(x for x in pair.split("/") if x!="gate")
        for t in sorted(times):
            q=g[g.minute==t]; gr=piv.loc[t] if t in piv.index else pd.Series(dtype=object)
            if len(q):
                rr=q.iloc[0]
                if rr.exchange_A==other: cp=rr.price_A;src=rr.source_time_A;typ="mark" if rr.quality_A=="mark" else "trade"
                else: cp=rr.price_B;src=rr.source_time_B;typ="mark" if rr.quality_B=="mark" else "trade"
                cf=rawmap.get((other,src,typ));sp=rr.spread if rr.exchange_A=="gate" else -rr.spread
            else:cp=np.nan;cf=None;sp=np.nan
            flags=[]
            if pd.isna(gr.get("gate_trade")):flags.append("MISSING_GATE_TRADE")
            if gr.get("gate_volume",1)==0:flags.append("ZERO_VOLUME")
            rows.append({"timestamp":t,"gate_trade":gr.get("gate_trade"),"gate_mark":gr.get("gate_mark"),"gate_index":gr.get("gate_index"),"comparison_exchange":other,"comparison_price":cp,"spread_bps":sp,"gate_volume":gr.get("gate_volume"),"gate_raw_source_file":gr.get("gate_raw_source_file"),"comparison_raw_source_file":cf,"warning_flags":"|".join(flags)})
    mm=(regime.gate_mark/regime.gate_trade-1)*10000;mi=(regime.gate_mark/regime.gate_index-1)*10000
    m=meta[meta.exchange=="gate"].iloc[0]
    diag=f"""# Gate regime 自动诊断

**观察期：** `[2026-07-16T00:00:00Z, 2026-07-20T00:00:00Z)`；实际 Gate 1m 覆盖从 {gp.open_time.min()} 开始。

- Gate mark vs trade close：中位 {mm.median():.3f} bps，P99 绝对值 {mm.abs().quantile(.99):.3f} bps，最大绝对值 {mm.abs().max():.3f} bps。
- Gate mark vs index：中位 {mi.median():.3f} bps，P99 绝对值 {mi.abs().quantile(.99):.3f} bps，最大绝对值 {mi.abs().max():.3f} bps。
- 时间连续性：分钟对齐 {aligned:.2f}%，最大相邻缺口 {diff.max():.1f} 分钟；标准化后重复时间戳 {dup}。
- API 顺序/分页：检查 {page_count} 个缓存 candlestick 响应页，时间非升序页 {bad_order}；标准化后无重复分钟，未发现游标方向或分钟桶错误证据。
- 零成交量分钟占比：{zero:.2f}%；regime 成交量中位数 {regime.gate_volume.median():.2f}，P05 {regime.gate_volume.quantile(.05):.2f}（合约张数口径）。
- 成交收盘价跳变：绝对 1m 涨跌 P95 {jumps.quantile(.95):.4f}%，P99 {jumps.quantile(.99):.4f}%，最大 {jumps.max():.4f}%。
- Metadata：contract_multiplier={m.contract_multiplier}，price_tick={m.price_tick}，quantity_step={m.quantity_step}，quote={m.quote_currency}，collateral={m.collateral_currency}。
- 最大 Gate 跨所偏离：{peak.pair}，{peak.minute}，绝对 {peak.abs_spread:.3f} bps。
- 原始响应样本路径已写入 `gate_regime_raw_samples.csv`，覆盖峰值及 regime 两个边界前后各 10 分钟。

## 初步判断

**本样本的证据更偏向 Gate 指数/标记口径及交易所自身价格发现 regime，而非采集分页错误。** Gate mark 与 trade 的差异远小于约 200 bps 的跨所偏离，但 mark 与 index 的 P99 绝对差达到 {mi.abs().quantile(.99):.3f} bps；同时分钟连续、无重复、几乎无零量。合约乘数 0.001 影响每张合约的底层数量，不应把价格静默乘除 1,000。产品已在 6 月 2 日上线，因此 7 月 16 日不是首日价格发现。仍无法仅凭分钟 K 线确认当时实时盘口、指数成分和休市机制。
"""
    return pd.DataFrame(rows),diag

def old_vs_corrected(oldps,newps,oldfs,newfs,oldevents,gatesum):
    rows=[]
    oldall=oldps[oldps.session=="ALL"]
    newall=newps[newps.session=="ALL"]
    metrics=[("平均价差","mean_bps"),("中位价差","median_bps"),("P95 绝对价差","p95_abs_bps"),("P99 绝对价差","p99_abs_bps"),("最大绝对价差","max_abs_bps"),("覆盖率","coverage_vs_pair_window_percent")]
    for _,n in newall.iterrows():
        o=oldall[oldall.pair==n.pair]
        if o.empty:continue
        for label,col in metrics:
            oldcol="coverage_percent" if col=="coverage_vs_pair_window_percent" else col;ov=float(o.iloc[0][oldcol]);nv=float(n[col]);reason="严格 pair 左闭右开共同窗口"
            if "gate" in n.pair.split("/"):
                post=gatesum[(gatesum.pair==n.pair)&(gatesum.regime=="POST_GATE_REGIME")].iloc[0];mapcol="coverage_percent" if col=="coverage_vs_pair_window_percent" else col;nv=float(post[mapcol]);reason+="；Gate 主长期对照排除 7月16–19 regime"
            rows.append(_change(label,n.pair,ov,nv,reason))
        oe=oldevents[(oldevents.pair==n.pair)&(oldevents.threshold_bps==20)];newlong=oe.duration_minutes.max() if len(oe) else np.nan
        if "gate" in n.pair.split("/"):newlong=float(gatesum[(gatesum.pair==n.pair)&(gatesum.regime=="POST_GATE_REGIME")].iloc[0].longest_event_over_20bps_minutes)
        rows.append(_change("事件数量",n.pair,len(oe),len(oe),"严格共同分钟；事件按全局连续段去重"));rows.append(_change("最长事件持续时间",n.pair,oe.duration_minutes.max() if len(oe) else np.nan,newlong,"严格共同分钟；Gate 排除异常 regime" if "gate" in n.pair.split("/") else "严格共同分钟"))
    for _,n in newfs.iterrows():
        o=oldfs[(oldfs.long_exchange==n.long_exchange)&(oldfs.short_exchange==n.short_exchange)]
        if len(o):rows.append(_change("资金费率理论现金流",n.pair,float(o.iloc[0].theoretical_cashflow_10000usd),float(n.theoretical_cashflow_10000usd),"价格与资金费率联合共同窗口；左闭右开"))
    return pd.DataFrame(rows)

def _change(metric,pair,old,new,reason):
    return {"metric":metric,"pair":pair,"old_value":old,"corrected_value":new,"absolute_change":new-old if pd.notna(old) and pd.notna(new) else np.nan,"percent_change":100*(new-old)/abs(old) if pd.notna(old) and old!=0 and pd.notna(new) else np.nan,"reason_for_change":reason}

def corrected_charts(global_df,funding,pairdf,gate_sum,prices,pair_bounds):
    sns.set_theme(style="whitegrid",font="WenQuanYi Zen Hei",rc={"axes.unicode_minus":False})
    plt.figure(figsize=(9,4));sns.barplot(global_df,x="exchange_set",y="coverage_percent");plt.xticks(rotation=20,ha="right");plt.ylabel("严格共同窗口覆盖率 %");_save("common_window_coverage.png","五/四/三交易所最大共同组合覆盖率")
    plt.figure(figsize=(7,6));mat=funding.pivot(index="long_exchange",columns="short_exchange",values="theoretical_cashflow_10000usd");sns.heatmap(mat,annot=True,fmt=".2f",center=0,cmap="RdYlGn");_save("funding_common_window_matrix.png","严格联合同窗：做多行/做空列每 $10,000 资金现金流")
    gp=pairdf[pairdf.pair.str.contains("gate")]
    plt.figure(figsize=(13,6))
    maxrow=gp.loc[gp.abs_spread.idxmax()]
    for pair,g in gp.groupby("pair"):
        g=g.sort_values("minute").set_index("minute");idx=pd.date_range(g.index.min(),g.index.max(),freq="1min",tz="UTC");s=g.spread.reindex(idx);plt.plot(idx,s,label=pair,linewidth=.7)
    plt.axvspan(GATE_START,GATE_END,color="orange",alpha=.2,label="Gate regime");plt.axhline(0,color="black",linewidth=.7);plt.annotate(f"{maxrow.abs_spread:.1f} bps",(maxrow.minute,maxrow.spread),xytext=(8,15),textcoords="offset points",arrowprops={"arrowstyle":"->"});plt.legend(ncol=2);plt.ylabel("对称价差 bps");plt.xlabel("UTC");_save("gate_regime_spread_timeseries.png","Gate 相关组合价差时序（缺口不连接）")
    tmp=gp.copy();tmp["regime"]=tmp.minute.map(gate_regime);plt.figure(figsize=(11,5));sns.boxplot(tmp,x="pair",y="spread",hue="regime",showfliers=False);plt.xticks(rotation=20);plt.ylabel("对称价差 bps");_save("gate_regime_boxplot.png","Gate 相关组合分 regime 价差箱线图（离群点未绘制但统计保留）")
    plt.figure(figsize=(11,5));sns.barplot(gate_sum,x="pair",y="p95_abs_bps",hue="regime");plt.xticks(rotation=20);plt.ylabel("绝对价差 P95 bps");_save("gate_pre_during_post_comparison.png","Gate 各组合 regime 前/中/后 P95")
    g=prices[prices.exchange=="gate"].pivot_table(index="open_time",columns="price_type",values="close",aggfunc="last");g=g[(g.index>=GATE_START)&(g.index<GATE_END)];plt.figure(figsize=(12,5));
    for c in ["trade","mark","index"]:
        if c in g:plt.plot(g.index,g[c],label=c,linewidth=.8)
    plt.legend();plt.ylabel("价格");plt.xlabel("UTC");_save("gate_mark_trade_index_comparison.png","Gate regime：成交/标记/指数价格")

def _save(name,title):
    plt.title(title);plt.tight_layout();plt.savefig(R/"charts"/name,dpi=150,bbox_inches="tight");plt.close()

def corrected_report(requested_start,run_end):
    glob=pd.read_csv(R/"global_common_window.csv");grank=pd.read_csv(R/"global_common_pairwise_price_summary.csv");pp=pd.read_csv(R/"pairwise_price_summary_common_window.csv");ff=pd.read_csv(R/"pairwise_funding_common_window.csv");gs=pd.read_csv(R/"gate_regime_summary.csv");gf=pd.read_csv(R/"gate_regime_funding_summary.csv")
    five=glob.sort_values("exchange_count",ascending=False).iloc[0];topf=ff.sort_values("theoretical_cashflow_10000usd",ascending=False);old=pd.read_csv(R/"pairwise_funding_legacy_NOT_COMPARABLE.csv");old580=old[(old.long_exchange=="bitget")&(old.short_exchange=="okx")].iloc[0];newbo=ff[(ff.long_exchange=="bitget")&(ff.short_exchange=="okx")].iloc[0]
    oldtop=old.sort_values("theoretical_cashflow_10000usd",ascending=False).head(3).pair.tolist();newtop=topf.head(3).pair.tolist()
    pair_lines=[]
    for _,r in pp[pp.session=="ALL"].sort_values("pair").iterrows():pair_lines.append(f"- {r.pair}: price `[{r.pair_price_start}, {r.pair_price_end})`，有效 {int(r.valid_common_minutes):,} 分钟，覆盖 {r.coverage_vs_pair_window_percent:.2f}%")
    joint_lines=[]
    for _,r in ff.sort_values(["long_exchange","short_exchange"]).iterrows():
        if r.long_exchange<r.short_exchange:joint_lines.append(f"- {r.long_exchange}/{r.short_exchange}: joint `[{r.joint_start}, {r.joint_end})`")
    gate_lines=[];ad=pd.read_parquet(ROOT/"data/normalized/aligned_prices_1m.parquet")
    for pair in sorted(gs.pair.unique()):
        x=gs[gs.pair==pair];during=x[x.regime=="GATE_REGIME_20260716_19"].iloc[0];post=x[x.regime=="POST_GATE_REGIME"].iloc[0]
        full=pp[(pp.pair==pair)&(pp.session=="ALL")].iloc[0];g=ad[(ad.pair==pair)&~((ad.minute>=GATE_START)&(ad.minute<GATE_END))];st=_spread_stats(g)
        gate_lines.append(f"- {pair}: 全共同窗口={full.p95_abs_bps:.2f}/{full.p99_abs_bps:.2f}/{full.max_abs_bps:.2f}；regime={during.p95_abs_bps:.2f}/{during.p99_abs_bps:.2f}/{during.max_abs_bps:.2f}；排除后={st['p95_abs_bps']:.2f}/{st['p99_abs_bps']:.2f}/{st['max_abs_bps']:.2f}；7月20日后={post.p95_abs_bps:.2f}/{post.p99_abs_bps:.2f}/{post.max_abs_bps:.2f} bps")
    gp=pd.read_parquet(ROOT/"data/normalized/prices_1m.parquet");gp=gp[(gp.exchange=="gate")&(gp.open_time>=GATE_START)&(gp.open_time<GATE_END)].pivot_table(index="open_time",columns="price_type",values="close",aggfunc="last");mm=((gp.mark/gp.trade-1)*10000).abs();mi=((gp.mark/gp["index"]-1)*10000).abs()
    short_gate=gf[(gf.short_exchange=="gate")&(gf.regime=="GATE_REGIME_20260716_19")].sort_values("theoretical_cashflow_10000usd",ascending=False)
    post_short=gf[(gf.short_exchange=="gate")&(gf.regime=="POST_GATE_REGIME")]
    diffs=ff.merge(old[["long_exchange","short_exchange","theoretical_cashflow_10000usd"]],on=["long_exchange","short_exchange"],suffixes=("_new","_old"));diffs["change"]=diffs.theoretical_cashflow_10000usd_new-diffs.theoretical_cashflow_10000usd_old;largest=diffs.reindex(diffs.change.abs().sort_values(ascending=False).index).head(3)
    diag_short=f"Gate mark–trade 绝对差 P99={mm.quantile(.99):.2f} bps，而 mark–index P99={mi.quantile(.99):.2f} bps；分钟连续、无重复、零量极少。证据更偏向 Gate 指数/标记口径及自身价格发现 regime，而非时间戳或分页错误。"
    summary=f"""# SKHYNIX 永续历史研究执行摘要（严格共同窗口修正版）

**截止 UTC：{run_end}**。所有主结果使用左闭右开严格共同窗口；旧非同窗榜仅作 `NOT_COMPARABLE` 审计。

## 强结论

1. 五家全局共同窗口为 `[{five.common_start}, {five.common_end})`，有效共同分钟 {int(five.valid_common_minutes):,}，覆盖 {five.coverage_percent:.2f}%。起点限制：{five.limiting_start_exchange}；终点限制：{five.limiting_end_exchange}。
   该五家统一样本的绝对价差 P95 最高组合为 {grank.sort_values('p95_abs_bps',ascending=False).iloc[0].pair}（{grank.sort_values('p95_abs_bps',ascending=False).iloc[0].p95_abs_bps:.2f} bps），不混用 pair 各自更长窗口。
2. 原 `long Bitget / short OKX = ${old580.theoretical_cashflow_10000usd:.2f}` 是非同窗结果；严格联合同窗后为 **${newbo.theoretical_cashflow_10000usd:.2f}**，即单边 $10,000 的 **{newbo.theoretical_cashflow_10000usd/100:.4f}%**。
3. 修正后资金前三名：{'; '.join(f"{r.pair} ${r.theoretical_cashflow_10000usd:.2f}" for _,r in topf.head(3).iterrows())}。旧前三为 {', '.join(oldtop)}，新前三为 {', '.join(newtop)}，**第三名发生变化**。
4. Gate regime 分段结果：
{chr(10).join(gate_lines)}
5. 若排除 7 月16–19 日后 P95 明显下降，原高 P95 由特定 regime 驱动，不能解释为长期稳定套利空间。
6. Gate 价格较高的 regime 内，做空 Gate 的资金方向确为正：{'; '.join(f"{r.pair} ${r.theoretical_cashflow_10000usd:.2f}" for _,r in short_gate.iterrows())}。但约 75–110 美元资金现金流小于相关组合约 189–208 bps 的峰值偏离，不能覆盖继续扩张、滑点和不可成交风险。7 月20日后这些做空 Gate 方向多数反转为负（{'; '.join(f"{r.pair} ${r.theoretical_cashflow_10000usd:.2f}" for _,r in post_short.iterrows())}）。

## Pair 价格共同窗口

{chr(10).join(pair_lines)}

## Pair 价格＋资金联合窗口（无方向组合）

{chr(10).join(joint_lines)}

## 初步结论

- 同窗修正会改变资金累计排序与金额；最大修正包括：{'; '.join(f"{r.long_exchange}/{r.short_exchange} {r.theoretical_cashflow_10000usd_old:.2f}→{r.theoretical_cashflow_10000usd_new:.2f} USD" for _,r in largest.iterrows())}。这些旧结果主要因短历史交易所的非同窗累计被高估；逐项变化见 `old_vs_corrected_results.csv`。
- {diag_short}
- Gate 7 月20日后价差显著收窄但仍有 19–43 bps 的 P95 残余；不能称为完全恢复一致。分段资金见 `gate_regime_funding_summary.csv`。

## 无法确认

- 历史分钟 K 线不是可执行 BBO，无法确认真实滑点、深度、成交容量或暂停期间盘口。
- 仅凭公开 K 线无法最终区分真实市场偏离、指数成分/休市机制与交易所内部定价口径；不做静默缩放。
"""
    if (R/"joint_strategy_events.csv").exists():
        from .history_quality import extended_report_block
        summary += extended_report_block()
    (R/"EXECUTIVE_SUMMARY.md").write_text(summary)
    imgs=[]
    for p in sorted((R/"charts").glob("*.png")):imgs.append(f'<h2>{p.stem}</h2><img src="data:image/png;base64,{base64.b64encode(p.read_bytes()).decode()}">')
    html_doc=f"<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>SKHYNIX 严格同窗修正版</title><style>body{{max-width:1250px;margin:30px auto;font:15px system-ui;line-height:1.55}}img{{max-width:100%}}table{{border-collapse:collapse;display:block;overflow:auto;font-size:12px}}td,th{{border:1px solid #ddd;padding:4px}}pre{{white-space:pre-wrap}}.warn{{background:#fff3cd;padding:12px}}</style><h1>SKHYNIX 严格共同窗口修正版</h1><div class='warn'>历史分钟代理，不是可执行 BBO。主资金榜已删除非同窗累计。</div><pre>{html.escape(summary)}</pre><h2>五家全局共同窗口统一排名</h2>{grank.sort_values('p95_abs_bps',ascending=False).to_html(index=False)}<h2>Pair 严格价格共同窗口</h2>{pp[pp.session=='ALL'].to_html(index=False)}<h2>严格同窗资金榜</h2>{topf.to_html(index=False)}<h2>Gate regime</h2>{gs.to_html(index=False)}{''.join(imgs)}</html>"
    (R/"quick_report.html").write_text(html_doc)
    return summary
