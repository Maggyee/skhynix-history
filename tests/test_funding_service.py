from __future__ import annotations

from datetime import timedelta

import pandas as pd

import skhynix_research.funding_service as fs
import skhynix_research.paper_trading as pt
from skhynix_research.live_bbo import (BBO, CAPACITY_VALID, EXCHANGES,
    SCHEMA_VERSION, SIZE_UNIT_OK)

NOW=pd.Timestamp("2026-07-24T12:00:00Z").to_pydatetime()


def _quote(exchange,bid,ask,ts,seq):
    return BBO(exchange,"X",bid,ask,100,100,ts,ts,seq,"native","c","CONTRACT",1,
        100,100,bid*100,ask*100,SIZE_UNIT_OK,f"c:{seq}",SCHEMA_VERSION,"meta",
        CAPACITY_VALID,"")


def _engine(root):
    paper={"pairs":["binance/gate"],"allowed_regimes":["NORMAL"],
        "entry_thresholds_bps":[100,150,200],"confirmation_seconds":5,
        "gross_notional_usd":1000,"max_open_positions":1,"exit_spread_bps":20,
        "max_holding_seconds":86400,"taker_fee_bps":{x:10 for x in EXCHANGES},
        "slippage_bps_per_fill":2,"safety_buffer_bps":10}
    result=pt.PaperEngine(paper,{"stale_after_ms":3000,"max_cross_exchange_skew_ms":1500},root,lambda _:"NORMAL")
    for exchange in EXCHANGES:result.connected[exchange]=True;result.connection_ids[exchange]="c"
    return result


def _open(engine):
    engine.quotes["binance"]=_quote("binance",99,100,NOW,1)
    engine.quotes["gate"]=_quote("gate",102,103,NOW,1);engine.evaluate(NOW)
    opened=NOW+timedelta(seconds=5)
    engine.quotes["binance"]=_quote("binance",99,100,opened,2)
    engine.quotes["gate"]=_quote("gate",102,103,opened,2);engine.evaluate(opened)
    return engine.positions[0],opened


def test_event_id_is_deterministic_and_venue_specific():
    when="2026-07-24T12:00:00Z"
    assert fs.funding_event_id("binance","X",when)==fs.funding_event_id("binance","X",when)
    assert fs.funding_event_id("binance","X",when)!=fs.funding_event_id("gate","X",when)


def test_service_backfill_injects_once_and_restart_deduplicates(tmp_path):
    e=_engine(tmp_path/"engine");position,opened=_open(e)
    settled=opened+timedelta(minutes=1);now=pd.Timestamp(settled)+pd.Timedelta(minutes=1)
    symbols={x:"X" for x in EXCHANGES}
    def fetcher(exchange,symbol,start,end):
        return ([{"exchange":"binance","symbol":symbol,"settled_at":settled,
            "rate":.001,"source":"TEST"},{"exchange":"binance","symbol":symbol,
            "settled_at":now-pd.Timedelta(days=3),"rate":.001,"source":"TOO_OLD"}]
            if exchange=="binance" else [])
    service=fs.FundingSettlementService(e,symbols,tmp_path/"service",poll_seconds=1,
        fetcher=fetcher,mark_provider=lambda _:100.,now=lambda:now)
    first=service.poll_once();pnl=position.funding_pnl_usd
    assert first["injected_count"]==1 and pnl<0
    service.poll_once();assert position.funding_pnl_usd==pnl
    restored_engine=_engine(tmp_path/"engine")
    restored=fs.FundingSettlementService(restored_engine,symbols,tmp_path/"service",
        fetcher=fetcher,mark_provider=lambda _:100.,now=lambda:now)
    restored.poll_once()
    assert restored_engine.positions[0].funding_pnl_usd==pnl
    assert restored.duplicate_rejected_count>=1
    assert restored.state_path.exists() and restored.health_path.exists()
    assert len(list((tmp_path/"service/events").rglob("events.parquet")))==1


def test_delayed_relevant_event_uses_only_closed_public_mark_bar(tmp_path,monkeypatch):
    e=_engine(tmp_path/"engine");position,opened=_open(e);e.quotes={}
    settled=opened+timedelta(minutes=2);now=pd.Timestamp(settled)+pd.Timedelta(minutes=1)
    closed={"open_time":pd.Timestamp(settled)-pd.Timedelta(minutes=1),
        "close_time":pd.Timestamp(settled)-pd.Timedelta(milliseconds=1),"close":100.}
    future={"open_time":settled,"close_time":pd.Timestamp(settled)+pd.Timedelta(minutes=1),"close":999.}
    monkeypatch.setitem(fs.DOWNLOADERS,"binance",lambda start,end,symbol:{"mark":([closed,future],None)})
    def fetcher(exchange,symbol,start,end):
        return ([{"exchange":"binance","symbol":symbol,"settled_at":settled,"rate":.001}]
            if exchange=="binance" else [])
    service=fs.FundingSettlementService(e,{x:"X" for x in EXCHANGES},tmp_path/"service",
        fetcher=fetcher,now=lambda:now)
    service.poll_once()
    assert position.funding_pnl_usd<0
    event=pd.read_parquet(next((tmp_path/"service/events").rglob("events.parquet"))).iloc[0]
    assert event.mark_price==100
