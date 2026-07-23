import pandas as pd
import pytest

import skhynix_research.fifteen_minute as fm


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
