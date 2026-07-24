import pandas as pd
import pytest
import re

import skhynix_research.fifteen_minute as fm
from skhynix_research.report_15m import write_15m_report


def _funding():
    rows=[]
    for i,ex in enumerate(fm.EXCHANGES):
        for t,r in [("2026-06-10T05:00Z",.001+i*.0001),("2026-06-10T06:00Z",.002+i*.0001),("2026-06-10T07:00Z",.003+i*.0001),("2026-06-10T08:00Z",.004+i*.0001)]:
            rows.append({"exchange":ex,"funding_time":pd.Timestamp(t),"funding_rate":r})
    return pd.DataFrame(rows)


def _prices(types=("trade",)):
    rows=[]
    for ex in fm.EXCHANGES:
        for typ in types:
            for j,t in enumerate(pd.date_range("2026-06-10T06:00Z",periods=4,freq="15min")):
                rows.append({"exchange":ex,"symbol":ex,"price_type":typ,"open_time":t,"close_time":t+pd.Timedelta(minutes=15)-pd.Timedelta(milliseconds=1),"open":100+j,"high":101+j,"low":99+j,"close":100+j+(fm.EXCHANGES.index(ex)*.01),"volume_base":1,"volume_quote":100,"source_endpoint":"official/native/candles","retrieved_at":"now","raw_file":f"data/raw/{ex}/x.json","native_interval":"15m","interval_minutes":15})
    return pd.DataFrame(rows)


def _okx_array(timestamp, confirmed="1"):
    t = int(pd.Timestamp(timestamp).timestamp() * 1000)
    return [str(t), "100", "101", "99", "100", "1", "100", "100", confirmed]


def test_okx_latest_page_forces_refresh_and_history_pages_use_cache(monkeypatch):
    instances = []

    class FakeHTTP:
        def __init__(self, exchange):
            self.calls = []
            self.last_retrieved_at = None
            instances.append(self)

        def get(self, url, params, *, force_refresh=False, ttl=None):
            self.calls.append((dict(params), force_refresh))
            self.last_retrieved_at = "2026-07-24T03:00:00+00:00"
            if "after" not in params:
                rows = [_okx_array("2026-07-24T02:00Z"), _okx_array("2026-07-24T01:45Z")]
                raw = "data/raw/okx/latest.json"
            else:
                rows = [_okx_array("2026-07-24T01:30Z"), _okx_array("2026-07-24T01:00Z")]
                raw = "data/raw/okx/history.json"
            return {"data": rows}, raw

    monkeypatch.setattr(fm, "CachedHTTP", FakeHTTP)
    rows = fm._download_okx_15m(
        pd.Timestamp("2026-07-24T01:00Z"),
        pd.Timestamp("2026-07-24T02:15Z"),
        "SKHYNIX-USDT-SWAP",
    )

    assert [force for _, force in instances[0].calls] == [True, False] * 3
    assert all("after" not in params for params, force in instances[0].calls if force)
    assert all("after" in params for params, force in instances[0].calls if not force)
    latest = [row for row in rows if row["open_time"] == pd.Timestamp("2026-07-24T02:00Z")]
    assert latest
    assert all(row["retrieved_at"] == "2026-07-24T03:00:00+00:00" for row in latest)
    assert all(row["raw_file"] == "data/raw/okx/latest.json" for row in latest)


def test_incremental_refresh_preserves_history_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(fm, "ROOT", tmp_path)
    normalized = tmp_path / "data" / "normalized"
    raw = tmp_path / "data" / "raw"
    normalized.mkdir(parents=True)
    raw.mkdir(parents=True)

    old = _prices().copy()
    old.to_parquet(normalized / "prices_15m.parquet", index=False)

    meta = pd.DataFrame(
        {"exchange": fm.EXCHANGES, "status": "active", "resolved_symbol": fm.EXCHANGES}
    )
    monkeypatch.setattr(fm, "discover_all", lambda: (meta, {}))

    def downloader(exchange):
        def run(start, end, symbol):
            row = _prices()[lambda frame: frame.exchange == exchange].iloc[0].to_dict()
            row["open_time"] = pd.Timestamp("2026-07-24T01:00Z")
            row["close_time"] = row["open_time"] + fm.BAR - pd.Timedelta(milliseconds=1)
            return [row]
        return run

    monkeypatch.setattr(
        fm, "NATIVE_DOWNLOADERS", {exchange: downloader(exchange) for exchange in fm.EXCHANGES}
    )
    args = (pd.Timestamp("2026-07-24T01:00Z"), pd.Timestamp("2026-07-24T01:30Z"))
    first = fm.download_native_prices_15m(*args)
    second = fm.download_native_prices_15m(*args)

    june_time = pd.Timestamp("2026-06-10T06:00Z")
    assert (first.open_time == june_time).any()
    assert len(first) == len(old) + len(fm.EXCHANGES)
    assert len(second) == len(first)
    assert not second.duplicated(["exchange", "symbol", "price_type", "open_time"]).any()


def test_incremental_refresh_failure_preserves_exchange_history(monkeypatch, tmp_path):
    monkeypatch.setattr(fm, "ROOT", tmp_path)
    normalized = tmp_path / "data" / "normalized"
    raw = tmp_path / "data" / "raw"
    normalized.mkdir(parents=True)
    raw.mkdir(parents=True)

    old = _prices().copy()
    old.to_parquet(normalized / "prices_15m.parquet", index=False)
    meta = pd.DataFrame(
        {"exchange": fm.EXCHANGES, "status": "active", "resolved_symbol": fm.EXCHANGES}
    )
    monkeypatch.setattr(fm, "discover_all", lambda: (meta, {}))

    def downloader(exchange):
        def run(start, end, symbol):
            if exchange == "okx":
                raise RuntimeError("transient OKX failure")
            row = _prices()[lambda frame: frame.exchange == exchange].iloc[0].to_dict()
            row["open_time"] = pd.Timestamp("2026-07-24T01:00Z")
            row["close_time"] = row["open_time"] + fm.BAR - pd.Timedelta(milliseconds=1)
            return [row]

        return run

    monkeypatch.setattr(
        fm, "NATIVE_DOWNLOADERS", {exchange: downloader(exchange) for exchange in fm.EXCHANGES}
    )
    out = fm.download_native_prices_15m(
        pd.Timestamp("2026-06-10T06:00Z"), pd.Timestamp("2026-07-24T01:30Z")
    )

    old_okx = old[old.exchange == "okx"].reset_index(drop=True)
    actual_okx = out[out.exchange == "okx"].reset_index(drop=True)
    pd.testing.assert_frame_equal(actual_okx[old_okx.columns], old_okx, check_dtype=False)


def test_funding_only_loader_does_not_read_price_data(monkeypatch,tmp_path):
    funding_path=tmp_path/"funding.parquet";_funding().to_parquet(funding_path,index=False)
    monkeypatch.setattr(fm,"ROOT",tmp_path)
    seen=[];real=pd.read_parquet
    def reader(path,*a,**kw):
        seen.append(str(path));return real(path,*a,**kw)
    monkeypatch.setattr(pd,"read_parquet",reader)
    fm.analyze_funding_global_from_file(funding_path)
    assert seen==[str(funding_path)]
    assert not any("price" in x for x in seen)


def test_all_funding_matrix_cells_share_identical_window(monkeypatch,tmp_path):
    monkeypatch.setattr(fm,"ROOT",tmp_path)
    _,matrix,_=fm.build_funding_global_outputs(_funding(),charts=False)
    assert matrix.global_start.nunique()==matrix.global_end.nunique()==1


def test_funding_events_are_real_not_prorated():
    q=fm.funding_events_in_position_window(_funding(),pd.Timestamp("2026-06-10T06:00Z"),pd.Timestamp("2026-06-10T08:00Z"))
    assert len(q)==5
    assert set(q.funding_time)=={pd.Timestamp("2026-06-10T07:00Z")}


def test_entry_boundary_settlement_is_excluded():
    q=fm.funding_events_in_position_window(_funding(),pd.Timestamp("2026-06-10T06:00Z"),pd.Timestamp("2026-06-10T07:30Z"))
    assert not (q.funding_time==pd.Timestamp("2026-06-10T06:00Z")).any()


def test_rejects_15m_synthesized_from_1m():
    p=_prices();p["native_interval"]="1m_resampled"
    with pytest.raises(ValueError,match="Non-native"):
        fm.validate_native_prices_15m(p)


def test_all_five_primary_analysis_uses_trade_close_only():
    aligned,_=fm._strict_trade_intersection(_prices(("trade","mark")))
    assert set(aligned.price_type)=={"trade"}
    assert set(aligned.comparison_price_type)=={"trade_close_15m"}


def test_mark_subset_does_not_enter_trade_primary():
    p=_prices(("trade","mark"));aligned,_=fm._strict_trade_intersection(p);marks=fm._mark_subset_summary(p)
    assert aligned.analysis_scope.eq("ALL_FIVE_TRADE_CLOSE_15M").all()
    assert marks.analysis_scope.eq("MARK_AVAILABLE_SUBSET_15M").all()


def test_15m_timestamps_align_to_quarter_hour():
    assert fm.validate_native_prices_15m(_prices())
    p=_prices();p.loc[p.index[0],"open_time"]=pd.Timestamp("2026-06-10T06:01Z")
    with pytest.raises(ValueError,match="align"):
        fm.validate_native_prices_15m(p)


def test_incomplete_last_bar_bound_is_excluded():
    assert fm._closed_end(pd.Timestamp("2026-06-10T06:14:59Z"))==pd.Timestamp("2026-06-10T06:00Z")
    assert fm._closed_end(pd.Timestamp("2026-06-10T06:15:00Z"))==pd.Timestamp("2026-06-10T06:15Z")


def test_15m_intersection_is_not_upsampled_to_1m():
    aligned,times=fm._strict_trade_intersection(_prices())
    assert len(times)==4
    assert len(aligned)==4*5
    assert aligned.open_time.sort_values().drop_duplicates().diff().dropna().min()==pd.Timedelta(minutes=15)


def test_event_duration_is_bar_count_times_15():
    z=pd.DataFrame({"open_time":pd.date_range("2026-06-10T06:00Z",periods=3,freq="15min"),"abs_spread_bps":[25,30,5],"spread_bps":[25,30,5]})
    event=fm._events_15m(z,20)[0]
    assert event["duration_bars"]==2
    assert event["duration_minutes"]==event["duration_bars"]*15


def test_one_missing_bar_sensitivity_never_crosses_real_below_threshold_bar():
    z=pd.DataFrame({"open_time":pd.date_range("2026-06-10T06:00Z",periods=3,freq="15min"),"abs_spread_bps":[25,5,30],"spread_bps":[25,5,30]})
    assert len(fm._events_15m(z,20,allow_one_missing=True))==2
    missing=z.drop(index=1)
    assert len(fm._events_15m(missing,20,allow_one_missing=True))==1


def test_joint_15m_funding_window_is_global_for_every_pair():
    matrix=fm._matrix_for_window(_funding(),pd.Timestamp("2026-06-10T06:00Z"),pd.Timestamp("2026-06-10T08:00Z"))
    assert matrix.global_start.nunique()==matrix.global_end.nunique()==1
    assert len(matrix)==20


def test_funding_global_generation_is_idempotent(monkeypatch,tmp_path):
    monkeypatch.setattr(fm,"ROOT",tmp_path)
    first=fm.build_funding_global_outputs(_funding(),charts=False)[1]
    second=fm.build_funding_global_outputs(_funding(),charts=False)[1]
    pd.testing.assert_frame_equal(first,second)


def _report_artifacts(tmp_path):
    r15=tmp_path/"reports_15m"; reports=tmp_path/"reports"; data=tmp_path/"data/normalized"
    (r15/"charts").mkdir(parents=True);(reports/"charts").mkdir(parents=True);data.mkdir(parents=True)
    pd.DataFrame([{"price_15m_global_start":"2026-06-10T06:00:00.123456Z","price_15m_global_end":"2026-06-10T07:00:00Z","strict_common_bar_count":4}]).to_csv(r15/"global_common_window_15m.csv",index=False)
    pd.DataFrame([{"exchange":ex,"first_open_time":"2026-06-10T06:00:00Z","last_open_time":"2026-06-10T06:45:00Z","coverage_percent":100,"strict_common_bar_count":4} for ex in fm.EXCHANGES]).to_csv(r15/"exchange_coverage_15m.csv",index=False)
    price=[]
    for i,(a,b) in enumerate(__import__('itertools').combinations(fm.EXCHANGES,2)):
        price.append({"pair":f"{a}/{b}","exchange_A":a,"exchange_B":b,"median_signed_spread_bps":i+.25,"p95_abs_bps":100-i,"p99_abs_bps":110-i,"max_abs_bps":120-i,"percent_A_higher":50+i})
    pd.DataFrame(price).to_csv(r15/"pairwise_price_summary_global_15m.csv",index=False)
    events=pd.DataFrame([{"base_event_id":f"e{i}","pair":"binance/gate" if i%2 else "binance/okx","threshold_bps":20,"comparison_quality":"STRICT_NATIVE_15M_BARS","duration_minutes":15*(i+1),"status":"COMPLETED"} for i in range(12)])
    events.to_csv(r15/"base_spread_events_global_five_15m.csv",index=False)
    pd.DataFrame([{"group_type":"ALL","group_value":"ALL_OBSERVED","event_count_total":12,"median_minutes":90,"p90_minutes":165,"p95_minutes":180,"max_observed_minutes":180,"ratio_le_60m":.3333,"ratio_le_240m":1,"ratio_gt_1440m":0}]).to_csv(r15/"event_duration_summary_20bps_15m.csv",index=False)
    joint=[]
    for cost in [20,40,80]:
        for i,p in enumerate(pd.DataFrame(price).pair): joint.append({"analysis_scope":"PRICE_FUNDING_15M_GLOBAL_WINDOW","execution_model":"NEXT_BAR_OPEN_PROXY","pair":p,"threshold_bps":20,"cost_bps":cost,"event_count_total":11,"realized_event_count":10,"censored_event_count":1,"positive_event_count":5,"win_rate":.5-i*.01,"sum_event_net_bps":1000-cost*10-i,"mean_event_net_bps":10-cost,"median_event_net_bps":5-cost,"p05_event_net_bps":-30,"p95_event_net_bps":40,"mean_holding_minutes":45,"median_holding_minutes":30,"max_holding_minutes":120,"joint_start":"2026-06-10T08:00:00Z","joint_end":"2026-06-11T08:00:00Z"})
    pd.DataFrame(joint).to_csv(r15/"joint_strategy_summary_15m.csv",index=False)
    funding=[]
    for i,(a,b) in enumerate(__import__('itertools').permutations(fm.EXCHANGES,2)):
        funding.append({"long_exchange":a,"short_exchange":b,"cashflow_10000usd":200-i,"cashflow_per_day_10000usd":2-i/100,"simple_apr_not_compounded":.2-i/1000,"data_quality":"OK"})
    pd.DataFrame(funding).to_csv(reports/"funding_global_matrix.csv",index=False)
    pd.DataFrame([{"exchange":ex,"global_start":"2026-06-10T08:00:00.123Z","global_end":"2026-06-11T08:00:00Z"} for ex in fm.EXCHANGES]).to_csv(reports/"funding_global_common_window.csv",index=False)
    rows=[]
    for ex_i,ex in enumerate(fm.EXCHANGES):
        for i,t in enumerate(pd.date_range("2026-06-10T06:00Z",periods=4,freq="15min")):
            rows.append({"exchange":ex,"price_type":"trade","open_time":t,"close":100+ex_i+i/10})
    pd.DataFrame(rows).to_parquet(data/"aligned_prices_15m.parquet",index=False)
    f=[]
    for ex_i,ex in enumerate(fm.EXCHANGES): f.append({"exchange":ex,"funding_time":pd.Timestamp("2026-06-11T00:00Z")+pd.Timedelta(seconds=ex_i),"funding_rate":.001+ex_i*.0001})
    pd.DataFrame(f).to_parquet(data/"funding_events.parquet",index=False)
    # The production funding heatmap is an already-generated input image.
    (reports/"charts/funding_global_common_window_matrix.png").write_bytes(b"not-a-real-png")
    return r15


def test_decision_report_structure_and_offline_assets(tmp_path):
    r15=_report_artifacts(tmp_path);path=write_15m_report(tmp_path,r15);doc=path.read_text()
    assert "SKHYNIX 五家交易所 15分钟历史研究" in doc
    assert "<pre" not in doc and "&lt;h1&gt;" not in doc
    assert "2026-06-10 06:00:00 UTC" in doc and ".123456" not in doc
    assert "20bps触发＋20bps双所总成本前5名" in doc and "20bps触发＋40bps双所总成本前5名" in doc and "20bps触发＋80bps双所总成本前5名" in doc
    assert "历史15分钟成交收盘价不是当时可执行BBO" in doc
    assert re.findall(r'<img[^>]+src="(.*?)"',doc)
    assert all(src.startswith("data:image/png;base64,") for src in re.findall(r'<img[^>]+src="(.*?)"',doc))
    assert not re.search(r'<(?:link|script)[^>]+(?:href|src)="https?://',doc)
    assert not re.search(r'(?i)(?:>|\s)(nan|[+-]?inf)(?:<|\s)',doc)
    assert "NEXT_BAR_OPEN_PROXY" in doc and "20/40/80bps均是整套双所开仓和平仓的总往返成本" in doc
    assert "当前价格收益使用事件峰值减离场价差" not in doc


def test_report_top_n_matches_csv_and_is_idempotent(tmp_path):
    r15=_report_artifacts(tmp_path);path=write_15m_report(tmp_path,r15);first=path.read_bytes()
    doc=first.decode(); assert "200.00" in doc and "100.00 bps" in doc
    blocks=re.findall(r'<h3>20bps触发＋(?:20|40|80)bps双所总成本前5名</h3>(.*?)(?=<h3>|</section>)',doc,re.S)
    assert len(blocks)==3
    assert all(re.search(r"<tbody>(.*?)</tbody>",block,re.S).group(1).count("<tr>") <= 5 for block in blocks)
    write_15m_report(tmp_path,r15);assert path.read_bytes()==first


def test_report_survives_missing_csv(tmp_path):
    r15=_report_artifacts(tmp_path);(r15/"joint_strategy_summary_15m.csv").unlink()
    path=write_15m_report(tmp_path,r15);doc=path.read_text()
    assert path.exists() and "无可用数据" in doc and "历史代理策略，不是实盘回测" in doc


def test_report_main_ranking_filters_model_and_20bps_threshold(tmp_path):
    r15=_report_artifacts(tmp_path);path=r15/"joint_strategy_summary_15m.csv";j=pd.read_csv(path)
    fake=j.iloc[0].copy();fake["pair"]="forbidden/threshold";fake["threshold_bps"]=50;fake["sum_event_net_bps"]=999999
    fake_model=j.iloc[0].copy();fake_model["pair"]="forbidden/model";fake_model["execution_model"]="LOOKAHEAD";fake_model["sum_event_net_bps"]=999999
    pd.concat([j,pd.DataFrame([fake,fake_model])],ignore_index=True).to_csv(path,index=False)
    doc=write_15m_report(tmp_path,r15).read_text()
    assert "forbidden/threshold" not in doc and "forbidden/model" not in doc


def _execution_inputs(signal_positive=True, times=None, exit_open=(101,101), funding=None, peak=999):
    times = list(times or pd.date_range("2026-06-10T00:00Z", periods=4, freq="15min"))
    rows=[]
    # Signal at t0, execution at t1, convergence close signal at t2,
    # and execution exit at t3.  Tuple order is exchange A/B.
    signal_close=(100.22,100) if signal_positive else (100,100.22)
    entry_open=(102,100) if signal_positive else (100,102)
    for ex_i,ex in enumerate(("binance","bitget")):
        for i,t in enumerate(times):
            if i==0: op=100;cl=signal_close[ex_i]
            elif i==1: op=entry_open[ex_i];cl=entry_open[ex_i]
            elif i==2: op=101;cl=101
            else: op=exit_open[ex_i];cl=exit_open[ex_i]
            rows.append({"exchange":ex,"price_type":"trade","open_time":t,"open":op,"high":max(op,cl)+1,"low":min(op,cl)-1,"close":cl})
    aligned=pd.DataFrame(rows)
    base=pd.DataFrame([{"base_event_id":"base-1","pair":"binance/bitget","threshold_bps":20,
        "event_start":times[0],"event_end":times[min(1,len(times)-1)],"peak_abs_spread_bps":abs(peak),
        "peak_spread_bps":peak,"comparison_quality":"STRICT_NATIVE_15M_BARS"}])
    funding = funding if funding is not None else pd.DataFrame(columns=["exchange","funding_time","funding_rate"])
    return aligned,base,funding,times


def _run_execution(signal_positive=True, **kwargs):
    aligned,base,funding,times=_execution_inputs(signal_positive=signal_positive,**kwargs)
    return fm._joint_events(aligned,base,funding,times[0],times[-1]+pd.Timedelta(minutes=15)).iloc[0]


def test_next_bar_open_entry_and_positive_signal_direction():
    r=_run_execution(True)
    assert r.status=="REALIZED" and r.entry_exec_time==pd.Timestamp("2026-06-10T00:15Z")
    assert r.signal_close_spread_bps>20 and r.signal_direction=="SHORT_A_LONG_B"
    assert r.long_exchange=="bitget" and r.short_exchange=="binance"
    assert r.entry_long_price==100 and r.entry_short_price==102


def test_negative_signal_goes_long_a_short_b():
    r=_run_execution(False)
    assert r.signal_close_spread_bps < -20 and r.signal_direction=="LONG_A_SHORT_B"
    assert r.long_exchange=="binance" and r.short_exchange=="bitget"
    assert r.entry_long_price==100 and r.entry_short_price==102
    expected=(101/100-1 + 1-101/102)*10_000
    assert r.gross_price_pnl_bps==pytest.approx(expected) and r.signed_spread_change_bps>0


def test_peak_field_never_changes_joint_returns():
    a=_run_execution(True,peak=25);b=_run_execution(True,peak=5000)
    for field in ["gross_price_pnl_bps","funding_cashflow_bps","combined_gross_bps","net_after_cost_20bps","mae_price_bps","mfe_price_bps"]:
        assert getattr(a,field)==pytest.approx(getattr(b,field))


def test_two_leg_formula_preserves_price_losses_and_cost_is_once():
    r=_run_execution(True,exit_open=(103,99))
    expected=(99/100-1 + 1-103/102)*10_000
    assert expected<0 and r.gross_price_pnl_bps==pytest.approx(expected)
    assert r.combined_gross_bps==pytest.approx(expected)
    assert r.net_after_cost_20bps==pytest.approx(expected-20)
    assert r.net_after_cost_40bps==pytest.approx(expected-40)
    assert r.net_after_cost_80bps==pytest.approx(expected-80)


def test_two_leg_profitable_formula_matches_manual_calculation():
    # Long Bitget enters 100/exits 101; short Binance enters 102/exits 101.
    r=_run_execution(True,exit_open=(101,101))
    expected=(101/100-1 + 1-101/102)*10_000
    assert r.gross_price_pnl_bps==pytest.approx(expected)
    assert r.long_return==pytest.approx(.01)
    assert r.short_return==pytest.approx(1-101/102)


def test_missing_next_entry_open_is_censored():
    t=[pd.Timestamp("2026-06-10T00:00Z")]
    aligned,base,funding,_=_execution_inputs(times=t)
    r=fm._joint_events(aligned,base,funding,t[0],t[0]+pd.Timedelta(hours=1)).iloc[0]
    assert r.status=="NO_NEXT_BAR_FOR_ENTRY" and not r.is_realized


def test_missing_next_exit_open_is_censored():
    t=list(pd.date_range("2026-06-10T00:00Z",periods=2,freq="15min"))
    aligned,base,funding,_=_execution_inputs(times=t)
    # The entry bar itself supplies the below-threshold exit signal.
    aligned.loc[aligned.open_time==t[1],"close"]=101
    r=fm._joint_events(aligned,base,funding,t[0],t[0]+pd.Timedelta(hours=1)).iloc[0]
    assert r.status=="NO_NEXT_BAR_FOR_EXIT" and not r.is_realized


def test_gap_during_hold_is_censored_without_bridging():
    t=[pd.Timestamp("2026-06-10T00:00Z"),pd.Timestamp("2026-06-10T00:15Z"),pd.Timestamp("2026-06-10T00:45Z")]
    aligned,base,funding,_=_execution_inputs(times=t)
    # Keep entry-bar spread active so the missing 00:30 bar is encountered.
    r=fm._joint_events(aligned,base,funding,t[0],t[-1]+pd.Timedelta(minutes=15)).iloc[0]
    assert r.status=="DATA_GAP_DURING_HOLD" and not r.is_realized


def test_funding_uses_strict_execution_boundaries_and_leg_signs():
    f=pd.DataFrame([
        {"exchange":"bitget","funding_time":pd.Timestamp("2026-06-10T00:10Z"),"funding_rate":.9},
        {"exchange":"bitget","funding_time":pd.Timestamp("2026-06-10T00:15Z"),"funding_rate":.8},
        {"exchange":"bitget","funding_time":pd.Timestamp("2026-06-10T00:30Z"),"funding_rate":.001},
        {"exchange":"binance","funding_time":pd.Timestamp("2026-06-10T00:30Z"),"funding_rate":.002},
        {"exchange":"binance","funding_time":pd.Timestamp("2026-06-10T00:45Z"),"funding_rate":.7},
    ])
    r=_run_execution(True,funding=f)
    assert r.entry_exec_time==pd.Timestamp("2026-06-10T00:15Z") and r.exit_exec_time==pd.Timestamp("2026-06-10T00:45Z")
    assert r.long_funding_event_count==1 and r.short_funding_event_count==1
    assert r.sum_long_funding==pytest.approx(.001) and r.sum_short_funding==pytest.approx(.002)
    assert r.funding_cashflow_bps==pytest.approx(10)  # -long + short


def test_thresholds_are_separate_and_only_realized_events_are_summarized():
    r=_run_execution(True)
    rows=[]
    for threshold in [20,50,100,150,200]:
        x=r.to_dict();x["threshold_bps"]=threshold;x["strategy_event_id"]=f"r-{threshold}";rows.append(x)
        y=x.copy();y["strategy_event_id"]=f"c-{threshold}";y["status"]="RIGHT_CENSORED";y["is_realized"]=False;y["net_after_cost_20bps"]=999999;rows.append(y)
    summary=fm._summarize_joint_events(pd.DataFrame(rows),pd.Timestamp("2026-06-10T00:00Z"),pd.Timestamp("2026-06-11T00:00Z"))
    assert set(summary.threshold_bps)=={20,50,100,150,200} and len(summary)==15
    assert summary.realized_event_count.eq(1).all() and summary.censored_event_count.eq(1).all()
    assert summary[summary.cost_bps==20].sum_event_net_bps.eq(r.net_after_cost_20bps).all()


def test_strategy_event_ids_and_results_are_idempotent():
    aligned,base,funding,times=_execution_inputs()
    first=fm._joint_events(aligned,base,funding,times[0],times[-1]+pd.Timedelta(minutes=15))
    second=fm._joint_events(aligned,base,funding,times[0],times[-1]+pd.Timedelta(minutes=15))
    pd.testing.assert_frame_equal(first,second)
    assert first.strategy_event_id.is_unique
