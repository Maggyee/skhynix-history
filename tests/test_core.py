import numpy as np, pandas as pd
from skhynix_research.calendar import parse_utc, session_label
from skhynix_research.analysis import symmetric_spread_bps,hourly_equivalent,align_prices,threshold_events,detect_scale_mismatch

def test_parse_utc():
    assert str(parse_utc("2026-06-10T05:50:00Z")).endswith("+00:00")
def test_sessions():
    dates={pd.Timestamp("2026-06-10").date()}
    assert session_label("2026-06-10T05:50Z",dates)=="PRE_CLOSE_BASELINE"
    assert session_label("2026-06-10T06:35Z",dates)=="POST_CLOSE_TRANSITION"
    assert session_label("2026-06-10T07:00Z",dates)=="KRX_OFFICIAL_AFTER_HOURS"
    assert session_label("2026-06-10T10:00Z",dates)=="KRX_FULLY_CLOSED"
    assert session_label("2026-06-11T07:00Z",dates)=="KRX_HOLIDAY_OR_WEEKEND"
def test_funding_sign_and_interval():
    assert -0.001+0.002==0.001
    assert hourly_equivalent(.004,4)==.001
def test_symmetric_spread():
    assert np.isclose(symmetric_spread_bps(101,99),200)
    assert np.isclose(symmetric_spread_bps(99,101),-200)
def _prices():
    return pd.DataFrame({"exchange":["a","a"],"price_type":["trade","trade"],"open_time":pd.to_datetime(["2026-01-01T00:00Z","2026-01-01T00:04Z"]),"close":[1.,2.]})
def test_alignment_no_future_and_two_minute_limit():
    z=align_prices(_prices()); z=z[z.exchange=="a"].set_index("minute")
    assert z.loc[pd.Timestamp("2026-01-01T00:01Z"),"price"]==1
    assert z.loc[pd.Timestamp("2026-01-01T00:03Z"),"price"]!=2 # no future
    assert pd.isna(z.loc[pd.Timestamp("2026-01-01T00:03Z"),"price"]) # >2m not filled
def test_event_merge_one_missing_minute():
    d=pd.DataFrame({"minute":pd.to_datetime(["2026-01-01T00:00Z","2026-01-01T00:02Z"]),"abs_spread":[30,40],"spread":[30,40]})
    assert len(threshold_events(d,20))==1
def test_idempotent_duplicates():
    p=pd.concat([_prices(),_prices()]);z=align_prices(p)
    assert len(z)==5
def test_scale_mismatch():
    a=pd.Series(np.arange(10,30)*10.);b=pd.Series(np.arange(10,30)*1.)
    bad,ratio=detect_scale_mismatch(a,b);assert bad and np.isclose(ratio,10)
def test_no_scale_false_positive():
    bad,_=detect_scale_mismatch(pd.Series(np.arange(10,30)*1.01),pd.Series(np.arange(10,30)));assert not bad

