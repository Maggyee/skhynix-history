import pandas as pd
from skhynix_research.common_windows import common_window,global_common_window,joint_common_window,left_closed_right_open,gate_regime,percent_gate_higher
from skhynix_research.analysis import align_prices

def ts(x): return pd.Timestamp(x)

def test_pair_common_window():
    assert common_window(ts("2026-01-01T00:00Z"),ts("2026-01-05T00:00Z"),ts("2026-01-02T00:00Z"),ts("2026-01-04T00:00Z"))==(ts("2026-01-02T00:00Z"),ts("2026-01-04T00:00Z"))

def test_global_common_window_and_limiters():
    s,e,ls,le=global_common_window({"a":(ts("2026-01-01T00:00Z"),ts("2026-01-05T00:00Z")),"b":(ts("2026-01-02T00:00Z"),ts("2026-01-04T00:00Z")),"c":(ts("2026-01-01T00:00Z"),ts("2026-01-06T00:00Z"))})
    assert (s,e,ls,le)==(ts("2026-01-02T00:00Z"),ts("2026-01-04T00:00Z"),["b"],["b"])

def test_joint_price_funding_window():
    assert joint_common_window(ts("2026-01-01T00:00Z"),ts("2026-01-10T00:00Z"),ts("2026-01-03T00:00Z"),ts("2026-01-08T00:00Z"))==(ts("2026-01-03T00:00Z"),ts("2026-01-08T00:00Z"))

def test_funding_left_closed_right_open():
    d=pd.DataFrame({"t":pd.to_datetime(["2026-01-01T00:00Z","2026-01-02T00:00Z","2026-01-03T00:00Z"]),"r":[1,2,3]})
    q=left_closed_right_open(d,"t",ts("2026-01-02T00:00Z"),ts("2026-01-03T00:00Z"));assert q.r.tolist()==[2]

def test_non_common_event_excluded_from_window():
    d=pd.DataFrame({"t":pd.to_datetime(["2026-01-01T00:00Z","2026-01-02T00:00Z"]),"r":[99,1]})
    assert left_closed_right_open(d,"t",ts("2026-01-02T00:00Z"),ts("2026-01-03T00:00Z")).r.sum()==1

def test_gate_regime_boundaries():
    assert gate_regime("2026-07-15T23:59Z")=="PRE_GATE_REGIME"
    assert gate_regime("2026-07-16T00:00Z")=="GATE_REGIME_20260716_19"
    assert gate_regime("2026-07-19T23:59Z")=="GATE_REGIME_20260716_19"
    assert gate_regime("2026-07-20T00:00Z")=="POST_GATE_REGIME"

def test_gate_direction_independent_of_pair_order():
    a=pd.DataFrame({"spread":[10,-5,20]});b=pd.DataFrame({"spread":[-10,5,-20]})
    assert percent_gate_higher(a,"gate","okx")==percent_gate_higher(b,"okx","gate")

def test_regime_stats_input_can_be_window_limited():
    d=pd.DataFrame({"t":pd.to_datetime(["2026-07-15T23:59Z","2026-07-16T00:00Z","2026-07-20T00:00Z"]),"v":[100,1,100]})
    q=left_closed_right_open(d,"t",ts("2026-07-16T00:00Z"),ts("2026-07-20T00:00Z"));assert q.v.tolist()==[1]

def test_over_two_minutes_still_not_filled():
    p=pd.DataFrame({"exchange":["x","x"],"price_type":["trade","trade"],"open_time":pd.to_datetime(["2026-01-01T00:00Z","2026-01-01T00:04Z"]),"close":[1.,2.]})
    z=align_prices(p).set_index("minute");assert pd.isna(z.loc[ts("2026-01-01T00:03Z"),"price"])

def test_common_window_idempotent():
    b={"a":(ts("2026-01-01T00:00Z"),ts("2026-01-05T00:00Z")),"b":(ts("2026-01-02T00:00Z"),ts("2026-01-04T00:00Z"))}
    assert global_common_window(b)==global_common_window(b)
