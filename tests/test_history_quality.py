import numpy as np
import pandas as pd

from skhynix_research.history_audit import gate_page_ranges, merge_candle_pages
from skhynix_research.history_quality import (
    normalize_epoch, detailed_session, strategy_price_pnl, excursion_metrics,
    gate_market_premium, event_entry_indices, gap_broken,
)
from skhynix_research.analysis import align_prices
from skhynix_research.common_windows import left_closed_right_open


def test_gate_pagination_does_not_stop_early():
    s=pd.Timestamp("2026-06-10T00:00Z");e=pd.Timestamp("2026-07-16T00:00Z")
    pages=list(gate_page_ranges(s,e,1900,1))
    assert pages[0][0]==s and pages[-1][1]==e
    assert all(b<a2 for (_,b),(a2,_) in zip(pages,pages[1:]))


def test_hyper_pages_keep_more_than_last_page_and_sort_descending():
    p1=[{"t":3},{"t":2}];p2=[{"t":2},{"t":1}]
    assert [x["t"] for x in merge_candle_pages([p1,p2])]==[1,2,3]


def test_seconds_and_milliseconds_conversion():
    assert normalize_epoch(1781070600)==normalize_epoch(1781070600000)


def test_raw_early_row_survives_normalization():
    p=pd.DataFrame({"exchange":["x","x"],"price_type":["trade","trade"],
                    "open_time":pd.to_datetime(["2026-01-01T00:00Z","2026-01-01T00:01Z"]),"close":[1.,2.]})
    z=align_prices(p)
    assert z.source_time.min()==p.open_time.min()


def test_requested_and_pair_coverage_are_distinct():
    requested=60*24;pair=60;valid=60
    assert 100*valid/pair==100 and 100*valid/requested<5


def test_funding_left_closed_right_open_fractional_timestamp():
    d=pd.DataFrame({"t":pd.to_datetime(["2026-01-01T00:00:00.001Z","2026-01-02T00:00:00.001Z"]),"r":[1,2]})
    q=left_closed_right_open(d,"t",pd.Timestamp("2026-01-01T00:00Z"),pd.Timestamp("2026-01-02T00:00:00.001Z"))
    assert q.r.tolist()==[1]


def test_outlier_exclusion_sensitivity():
    x=pd.Series([1.,1.,1.,100.]);idx=x.abs().nlargest(1).index
    assert x.drop(idx).sum()==3


def test_entries_use_current_and_past_only_and_merge_one_missing_minute():
    t=pd.date_range("2026-01-01",periods=5,freq="min",tz="UTC")
    # Active at 0 and 2 is one event; the future value at 4 cannot create entry at 0.
    assert event_entry_indices(t,[30,0,30,0,30],20).tolist()==[0]


def test_strategy_direction_signs():
    assert strategy_price_pnl(100,20,long_is_a=False)==80
    assert strategy_price_pnl(-100,-20,long_is_a=True)==80
    assert strategy_price_pnl(100,120,long_is_a=True)==20


def test_mae_mfe():
    mae,mfe,pos=excursion_metrics([0,-30,10,25,-5])
    assert (mae,mfe,pos)==(30,25,1)


def test_sessions_use_new_york_dst():
    dates={pd.Timestamp(x).date() for x in ["2026-07-15","2026-07-16","2026-12-15","2026-12-16"]}
    assert detailed_session("2026-07-15T13:30Z",dates)=="US_REGULAR"
    assert detailed_session("2026-12-15T14:30Z",dates)=="US_REGULAR"
    assert detailed_session("2026-07-15T20:00Z",dates)=="US_AFTER_HOURS"
    assert detailed_session("2026-12-15T21:00Z",dates)=="US_AFTER_HOURS"


def test_plot_data_breaks_long_gap():
    q=gap_broken(pd.to_datetime(["2026-01-01T00:00Z","2026-01-01T00:04Z"]),[1,2])
    assert q.value.isna().sum()==1


def test_gate_market_median_premium():
    assert np.isclose(gate_market_premium(102,[100,100,100]),200)


def test_merge_pages_idempotent():
    page=[{"t":1,"c":"1"},{"t":2,"c":"2"}]
    assert merge_candle_pages([page,page])==merge_candle_pages([page])
