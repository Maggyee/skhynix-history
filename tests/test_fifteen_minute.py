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
        for i,p in enumerate(pd.DataFrame(price).pair): joint.append({"pair":p,"cost_bps":cost,"event_count":10,"win_rate":.5-i*.01,"total_net_bps":1000-cost*10-i,"median_net_bps":5-cost})
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
    assert "20bps成本前5名" in doc and "40bps成本前5名" in doc and "80bps成本前5名" in doc
    assert "历史15分钟成交收盘价不是当时可执行BBO" in doc
    assert re.findall(r'<img[^>]+src="(.*?)"',doc)
    assert all(src.startswith("data:image/png;base64,") for src in re.findall(r'<img[^>]+src="(.*?)"',doc))
    assert not re.search(r'<(?:link|script)[^>]+(?:href|src)="https?://',doc)
    assert not re.search(r'(?i)(?:>|\s)(nan|[+-]?inf)(?:<|\s)',doc)


def test_report_top_n_matches_csv_and_is_idempotent(tmp_path):
    r15=_report_artifacts(tmp_path);path=write_15m_report(tmp_path,r15);first=path.read_bytes()
    doc=first.decode(); assert "200.00" in doc and "100.00 bps" in doc
    blocks=re.findall(r'<h3>(?:20|40|80)bps成本前5名</h3>(.*?)(?=<h3>|</section>)',doc,re.S)
    assert len(blocks)==3
    assert all(re.search(r"<tbody>(.*?)</tbody>",block,re.S).group(1).count("<tr>") <= 5 for block in blocks)
    write_15m_report(tmp_path,r15);assert path.read_bytes()==first


def test_report_survives_missing_csv(tmp_path):
    r15=_report_artifacts(tmp_path);(r15/"joint_strategy_summary_15m.csv").unlink()
    path=write_15m_report(tmp_path,r15);doc=path.read_text()
    assert path.exists() and "无可用数据" in doc and "历史代理策略，不是实盘回测" in doc
