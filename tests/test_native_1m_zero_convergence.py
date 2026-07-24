import numpy as np
import pandas as pd
import pytest

from skhynix_research import native_1m_zero_convergence as z


def candles(values, exchanges=z.EXCHANGES, start="2026-07-16T18:34Z", gap=None):
    times = pd.date_range(start, periods=len(values), freq="min")
    rows=[]
    for ei,ex in enumerate(exchanges):
        for i,t in enumerate(times):
            if gap and (ex,i) in gap: continue
            value = values[i][ei] if isinstance(values[i], (tuple,list)) else values[i] + ei*.01
            rows.append({"exchange":ex,"price_type":"trade","open_time":t,
                "close_time":t+pd.Timedelta(minutes=1)-pd.Timedelta(milliseconds=1),
                "open":value,"high":value+1,"low":value-1,"close":value,
                "retrieved_at":"2026-07-17T00:00Z"})
            # rows explicitly excluded by the loader
    rows += [{**rows[0],"exchange":"hyperliquid"},{**rows[0],"price_type":"mark"}]
    return pd.DataFrame(rows)


def frame(spreads, opens=None, gap_at=None):
    times=pd.date_range("2026-01-01",periods=len(spreads),freq="min",tz="UTC")
    if gap_at is not None: times=times.delete(gap_at)
    # b=100 and solve symmetric spread for a.
    b=np.full(len(times),100.0); s=np.asarray(spreads,float)
    a=100*(20_000+s)/(20_000-s)
    o=np.asarray(opens if opens is not None else spreads,float)
    ao=100*(20_000+o)/(20_000-o)
    return pd.DataFrame({"open_a":ao,"open_b":b,"open_spread_bps":o,
        "close_a":a,"close_b":b,"close_spread_bps":s},index=times)


def sim(f, **kw):
    defaults=dict(a="binance",b="gate",scope=z.SCOPES[0],confirmation="ONE_BAR_CONFIRM",
        threshold=20,exit_policy="ZERO_CROSS",max_hold=None,funding=None)
    defaults.update(kw)
    return z.simulate_pair(f,**defaults)


def test_prepare_filters_exchanges_price_types_and_uses_gate_start():
    p=candles([100,101,102])
    # Give the other exchanges an older row; Gate remains the limiting first bar.
    old=p[(p.exchange=="binance") & (p.price_type=="trade")].iloc[0].copy()
    extras=[]
    for ex in ("binance","bitget","okx"):
        q=old.copy();q.exchange=ex;q.open_time=pd.Timestamp("2026-07-16T18:33Z");q.close_time=q.open_time+pd.Timedelta(minutes=1)-pd.Timedelta(milliseconds=1);extras.append(q)
    out=z.prepare_prices(pd.concat([p,pd.DataFrame(extras)],ignore_index=True))
    assert set(out.trade.exchange)==set(z.EXCHANGES)
    assert not out.trade.price_type.ne("trade").any()
    assert out.window_start==pd.Timestamp("2026-07-16T18:34Z")


def test_prepare_excludes_a_still_forming_last_bar():
    p=candles([100,101,102])
    forming=p.open_time.eq(pd.Timestamp("2026-07-16T18:36Z"))
    p.loc[forming,"retrieved_at"]="2026-07-16T18:36:30Z"
    out=z.prepare_prices(p)
    assert out.window_end==pd.Timestamp("2026-07-16T18:36Z")
    assert out.all_four.max()==pd.Timestamp("2026-07-16T18:35Z")


def test_strict_four_intersection_and_no_forward_fill():
    out=z.prepare_prices(candles([100,101,102,103],gap={("bitget",2)}))
    assert len(out.all_four)==3
    strict=z.pair_frame(out,"binance","gate",z.SCOPES[0]); pair=z.pair_frame(out,"binance","gate",z.SCOPES[1])
    assert len(strict)==3 and len(pair)==4
    assert pd.Timestamp("2026-07-16T18:36Z") not in strict.index


def test_completion_uses_minute_boundary_not_malformed_gate_close_time():
    p=candles([100,101])
    gate=p[(p.exchange=="gate") & (p.price_type=="trade")].index
    p.loc[gate,"close_time"]=p.loc[gate,"open_time"]+pd.Timedelta(hours=16,minutes=40)-pd.Timedelta(seconds=1)
    out=z.prepare_prices(p)
    assert out.window_end==pd.Timestamp("2026-07-16T18:36Z")
    p.loc[p.open_time.eq(pd.Timestamp("2026-07-16T18:35Z")),"retrieved_at"]="2026-07-16T18:35:30Z"
    out=z.prepare_prices(p)
    assert out.window_end==pd.Timestamp("2026-07-16T18:35Z")


def test_close_signal_next_open_direction_cross_and_single_cost():
    f=frame([50,40,-1,-2],opens=[50,30,20,-3])
    e=sim(f).iloc[0]
    assert e.signal_time==f.index[0] and e.entry_exec_time==f.index[1]
    assert e.entry_open_spread_bps==pytest.approx(30)
    assert e.entry_direction=="SHORT_A_LONG_B" and e.short_exchange=="binance" and e.long_exchange=="gate"
    assert e.exit_signal_time==f.index[2] and e.exit_exec_time==f.index[3]
    assert e.status=="REALIZED"
    assert e.net_price_pnl_bps==pytest.approx(e.gross_price_pnl_bps-20)


@pytest.mark.parametrize("policy,value",[("ZERO_BAND_5",4),("ZERO_BAND_10",9),("ZERO_BAND_20",19)])
def test_exit_bands(policy,value):
    e=sim(frame([50,40,value,value]),exit_policy=policy).iloc[0]
    assert e.exit_signal_time==pd.Timestamp("2026-01-01T00:02Z")
    assert e.status=="REALIZED"


def test_negative_signal_has_correct_long_short():
    e=sim(frame([-50,-40,1,2])).iloc[0]
    assert e.entry_direction=="LONG_A_SHORT_B"
    assert e.long_exchange=="binance" and e.short_exchange=="gate"


def test_right_censored_has_no_pnl_and_does_not_enter_mean():
    e=sim(frame([50,40,30])).iloc[0]
    assert e.status=="RIGHT_CENSORED" and pd.isna(e.net_price_pnl_bps)
    s=z.summarize_events(pd.DataFrame([e])).iloc[0]
    assert s.realized_event_count==0 and s.right_censored_count==1 and pd.isna(s.mean_net_price_pnl_bps)


def test_gap_during_hold_and_missing_entry_are_explicit():
    g=frame([50,40,30,10]);g.index=pd.DatetimeIndex([g.index[0],g.index[1]+pd.Timedelta(minutes=1),g.index[2]+pd.Timedelta(minutes=1),g.index[3]+pd.Timedelta(minutes=1)])
    assert sim(g).iloc[0].status=="NO_NEXT_BAR_FOR_ENTRY"
    h=frame([0,50,40,30,10]);h.index=pd.DatetimeIndex([h.index[0],h.index[1],h.index[2],h.index[3]+pd.Timedelta(minutes=1),h.index[4]+pd.Timedelta(minutes=1)])
    assert sim(h).iloc[0].status=="DATA_GAP_DURING_HOLD"


def test_two_bar_confirmation_and_no_repeated_sustained_entries():
    f=frame([50,55,45,35,25,10,0,-1])
    one=sim(f);two=sim(f,confirmation="TWO_BAR_CONFIRM")
    assert len(one)==1 and len(two)==1
    assert two.iloc[0].signal_time==f.index[1]


def test_funding_window_is_strict_and_cashflow_sign_is_consistent():
    t=pd.date_range("2026-01-01",periods=5,freq="min",tz="UTC")
    funding=pd.DataFrame([
        {"exchange":"gate","funding_time":t[1],"funding_rate":.9},
        {"exchange":"gate","funding_time":t[2],"funding_rate":.001},
        {"exchange":"binance","funding_time":t[2],"funding_rate":.002},
        {"exchange":"binance","funding_time":t[3],"funding_rate":.8},])
    # Positive signal: long gate, short binance; entry t1, exit t3.
    e=sim(frame([50,40,-1,-2,-3]),funding=funding).iloc[0]
    assert e.funding_pnl_bps==pytest.approx(10)
    assert e.net_combined_pnl_bps==pytest.approx(e.gross_price_pnl_bps+10-20)


def test_max_hold_is_realized_once_and_no_immediate_reentry():
    e=sim(frame([50,45,40,35,30,0,-1]),max_hold=2)
    assert len(e)==1 and e.iloc[0].status=="MAX_HOLD"
    assert e.iloc[0].holding_minutes==2


def test_zero_and_small_samples_are_supported():
    empty=sim(frame([0,1,2]))
    assert list(empty.columns)==z.EVENT_COLUMNS and z.summarize_events(empty).empty
    full=z.complete_summary_grid(z.summarize_events(empty))
    assert len(full)==4200 and full.total_signal_count.sum()==0


def test_compact_grids_keep_every_pair_and_threshold():
    empty=z._aggregate_events(pd.DataFrame(columns=z.EVENT_COLUMNS),["pair","threshold_bps"])
    full=z.complete_aggregate_grid(empty,{"pair":[f"{a}/{b}" for a,b in z.PAIRS],"threshold_bps":z.THRESHOLDS})
    assert len(full)==42 and full.total_signal_count.sum()==0


def test_event_type_normalization_makes_metrics_plot_ready():
    e=z.normalize_event_types(sim(frame([50,40,-1,-2])))
    assert pd.api.types.is_numeric_dtype(e.mae_price_pnl_bps)
    assert str(e.signal_time.dtype)=="datetime64[us, UTC]"


def test_global_equity_is_sequential_and_never_has_more_than_one_position():
    events=pd.DataFrame([
        {**{c:np.nan for c in z.EVENT_COLUMNS},"pair":"a/b","status":"REALIZED","entry_exec_time":pd.Timestamp("2026-01-01T00:00Z"),"exit_exec_time":pd.Timestamp("2026-01-01T00:02Z"),"net_price_pnl_bps":100},
        {**{c:np.nan for c in z.EVENT_COLUMNS},"pair":"c/d","status":"REALIZED","entry_exec_time":pd.Timestamp("2026-01-01T00:02Z"),"exit_exec_time":pd.Timestamp("2026-01-01T00:03Z"),"net_price_pnl_bps":-100},])
    curve=z.global_equity(events)
    assert curve.iloc[-1].compounded_equity_usd==pytest.approx(999.975)
    assert curve.iloc[-1].non_compounded_equity_usd==pytest.approx(1000)
