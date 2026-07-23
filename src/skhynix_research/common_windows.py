from __future__ import annotations
import pandas as pd

GATE_START=pd.Timestamp("2026-07-16T00:00:00Z")
GATE_END=pd.Timestamp("2026-07-20T00:00:00Z")

def common_window(first_a,last_a,first_b,last_b):
    return max(pd.Timestamp(first_a),pd.Timestamp(first_b)), min(pd.Timestamp(last_a),pd.Timestamp(last_b))

def global_common_window(bounds: dict[str,tuple]):
    start=max(pd.Timestamp(v[0]) for v in bounds.values())
    end=min(pd.Timestamp(v[1]) for v in bounds.values())
    limiting_start=sorted(k for k,v in bounds.items() if pd.Timestamp(v[0])==start)
    limiting_end=sorted(k for k,v in bounds.items() if pd.Timestamp(v[1])==end)
    return start,end,limiting_start,limiting_end

def joint_common_window(price_start,price_end,funding_start,funding_end):
    return max(pd.Timestamp(price_start),pd.Timestamp(funding_start)), min(pd.Timestamp(price_end),pd.Timestamp(funding_end))

def left_closed_right_open(df: pd.DataFrame,column: str,start,end):
    return df[(df[column]>=pd.Timestamp(start))&(df[column]<pd.Timestamp(end))]

def gate_regime(ts) -> str:
    t=pd.Timestamp(ts)
    if t<GATE_START:return "PRE_GATE_REGIME"
    if t<GATE_END:return "GATE_REGIME_20260716_19"
    return "POST_GATE_REGIME"

def percent_gate_higher(df: pd.DataFrame, exchange_a: str, exchange_b: str) -> float:
    if df.empty:return float("nan")
    if exchange_a=="gate": return 100.0*(df["spread"]>0).mean()
    if exchange_b=="gate": return 100.0*(df["spread"]<0).mean()
    raise ValueError("pair does not contain gate")

