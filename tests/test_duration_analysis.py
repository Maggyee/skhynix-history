import pandas as pd
import numpy as np

from skhynix_research.duration_analysis import (
    build_base_events, build_strategy_scenarios, convergence_summary,
    duration_bucket_counts,
)
from skhynix_research.history_quality import detailed_session


DATES={pd.Timestamp(x).date() for x in ["2026-07-15","2026-07-16","2026-12-15","2026-12-16"]}


def pair_df(times, spreads, pair="a/b"):
    times=pd.to_datetime(times,utc=True)
    a,b=pair.split("/")
    return pd.DataFrame({
        "minute":times,"spread":spreads,"abs_spread":np.abs(spreads),
        "exchange_A":a,"exchange_B":b,"pair":pair,
        "price_A":100+np.asarray(spreads)/200,"price_B":100-np.asarray(spreads)/200,
        "comparison_quality":"mark_spread_bps",
    })


def empty_funding(exchanges=("a","b")):
    rows=[]
    for ex in exchanges:
        rows.append({"exchange":ex,"funding_time":pd.Timestamp("2026-07-14T00:00Z"),"funding_rate":0.0})
    return pd.DataFrame(rows)


def test_continuous_active_minutes_one_base_event():
    p=pair_df(pd.date_range("2026-07-15",periods=5,freq="min",tz="UTC"),[30,35,25,10,5])
    e=build_base_events(p,DATES,[20])
    assert len(e)==1 and e.iloc[0].status=="COMPLETED"
    assert e.iloc[0].duration_observed_minutes==3


def test_one_missing_minute_stays_one_event():
    p=pair_df(["2026-07-15T00:00Z","2026-07-15T00:02Z","2026-07-15T00:03Z"],[30,35,5])
    e=build_base_events(p,DATES,[20])
    assert len(e)==1 and e.iloc[0].duration_observed_minutes==3


def test_observed_below_threshold_minute_splits_events():
    p=pair_df(pd.date_range("2026-07-15",periods=5,freq="min",tz="UTC"),[30,10,35,10,5])
    e=build_base_events(p,DATES,[20])
    assert len(e)==2
    assert set(e.status)=={"COMPLETED"}
    assert e.duration_observed_minutes.tolist()==[1,1]


def test_gap_over_two_minutes_is_data_gap_censored():
    p=pair_df(["2026-07-15T00:00Z","2026-07-15T00:04Z"],[30,5])
    e=build_base_events(p,DATES,[20])
    assert len(e)==1 and e.iloc[0].status=="DATA_GAP_CENSORED"
    assert bool(e.iloc[0].is_data_gap_censored)


def test_active_at_pair_end_is_right_censored():
    p=pair_df(pd.date_range("2026-07-15",periods=3,freq="min",tz="UTC"),[30,25,22])
    e=build_base_events(p,DATES,[20])
    assert e.iloc[0].status=="RIGHT_CENSORED" and bool(e.iloc[0].is_right_censored)


def test_base_event_not_duplicated_by_direction_or_exit_rule():
    p=pair_df(pd.date_range("2026-07-15",periods=5,freq="min",tz="UTC"),[30,25,10,5,4])
    base=build_base_events(p,DATES,[20])
    scenarios=build_strategy_scenarios(p,empty_funding(),base,DATES,p.minute.max()+pd.Timedelta(minutes=1))
    assert len(base)==1
    assert scenarios.base_event_id.nunique()==1
    assert scenarios.strategy_direction.nunique()==2
    assert scenarios.exit_rule.nunique()==11
    assert scenarios.scenario_id.nunique()==len(scenarios)
    assert set(scenarios.base_event_id)==set(base.base_event_id)


def test_target_miss_and_stop_miss_are_retained_as_timeout():
    times=pd.date_range("2026-07-15",periods=4321,freq="min",tz="UTC")
    p=pair_df(times,np.repeat(30.0,len(times)))
    base=build_base_events(p,DATES,[20])
    scenarios=build_strategy_scenarios(p,empty_funding(),base,DATES,times[-1]+pd.Timedelta(minutes=1))
    target=scenarios[scenarios.exit_rule=="target_10bps"]
    stop=scenarios[scenarios.exit_rule=="stop_50bps"]
    assert len(target)==2 and (~target.target_hit).all() and set(target.exit_status)=={"TIMEOUT"}
    assert len(stop)==2 and (~stop.stop_hit).all() and set(stop.exit_status)=={"TIMEOUT"}


def test_end_of_data_is_retained():
    times=pd.date_range("2026-07-15",periods=5,freq="min",tz="UTC")
    p=pair_df(times,[30,28,27,26,25]);base=build_base_events(p,DATES,[20])
    scenarios=build_strategy_scenarios(p,empty_funding(),base,DATES,times[-1]+pd.Timedelta(minutes=1))
    q=scenarios[scenarios.exit_rule=="target_10bps"]
    assert len(q)==2 and set(q.exit_status)=={"END_OF_DATA"} and q.is_censored.all()


def test_target_hit_is_strictly_after_entry_and_below_threshold(tmp_path):
    p=pair_df(pd.date_range("2026-07-15",periods=3,freq="min",tz="UTC"),[20,19,18])
    base=build_base_events(p,DATES,[20])
    assert base.iloc[0].minutes_to_below_20bps==1
    assert bool(base.iloc[0].hit_20bps_within_5m)


def test_convergence_probability_uses_hits_not_holding_minutes(tmp_path):
    p=pair_df(pd.date_range("2026-07-15",periods=61,freq="min",tz="UTC"),np.repeat(30.,61))
    base=build_base_events(p,DATES,[20])
    out=convergence_summary(base,tmp_path)
    row=out[(out.group_type=="ALL")&(out.target_bps==20)&(out.horizon_minutes==60)].iloc[0]
    assert row.hit_count==0 and row.naive_hit_rate==0


def test_session_boundaries_and_dst():
    assert detailed_session("2026-07-15T00:00Z",DATES)=="KRX_REGULAR_EARLIER"
    assert detailed_session("2026-07-15T05:49Z",DATES)=="KRX_REGULAR_EARLIER"
    assert detailed_session("2026-07-15T05:50Z",DATES)=="PRE_CLOSE_BASELINE"
    assert detailed_session("2026-07-15T06:30Z",DATES)=="KRX_CLOSE_TRANSITION"
    assert detailed_session("2026-07-15T06:40Z",DATES)=="KRX_OFFICIAL_AFTER_HOURS"
    assert detailed_session("2026-07-15T09:00Z",DATES)=="US_PREMARKET"
    assert detailed_session("2026-07-15T13:30Z",DATES)=="US_REGULAR"
    assert detailed_session("2026-12-15T14:30Z",DATES)=="US_REGULAR"


def test_duration_buckets_have_over_240_and_only_base_rows():
    p1=pair_df(pd.date_range("2026-07-15",periods=3,freq="min",tz="UTC"),[30,25,5])
    p2=pair_df(pd.date_range("2026-07-15",periods=301,freq="min",tz="UTC"),np.r_[np.repeat(30.,300),5],"c/d")
    base=pd.concat([build_base_events(p1,DATES,[20]),build_base_events(p2,DATES,[20])],ignore_index=True)
    counts=duration_bucket_counts(base)
    assert counts.loc[">240m"].sum()==1
    assert counts.to_numpy().sum()==base.base_event_id.nunique()==2
    assert (base.duration_observed_minutes>0).all()


def test_censored_count_matches_base_csv_semantics():
    p=pair_df(["2026-07-15T00:00Z","2026-07-15T00:04Z"],[30,5])
    base=build_base_events(p,DATES,[20])
    assert (base.status!="COMPLETED").sum()==base.is_data_gap_censored.sum()+base.is_right_censored.sum()


def test_15m_frequency_is_not_one_minute_and_is_idempotent():
    p=pair_df(pd.date_range("2026-07-15",periods=4,freq="15min",tz="UTC"),[30,25,10,5])
    a=build_base_events(p,DATES,[20],frequency_minutes=15)
    b=build_base_events(p,DATES,[20],frequency_minutes=15)
    assert a.iloc[0].frequency_minutes==15 and a.iloc[0].duration_observed_minutes==30
    assert a.base_event_id.tolist()==b.base_event_id.tolist()
