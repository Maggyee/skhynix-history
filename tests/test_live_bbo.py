from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pandas as pd

from skhynix_research.live_bbo import (BBO, BBOStorage, parse_binance, parse_bitget,
    parse_gate, parse_hyperliquid, parse_okx)
from skhynix_research.paper_trading import PaperEngine, generate_daily_report

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _quote(exchange, bid, ask, bid_size=10, ask_size=10, ts=NOW, seq=1, connection="c"):
    return BBO(exchange, "SKHYNIX", bid, ask, bid_size, ask_size, ts, ts, seq,
        "native", connection)


def _engine(tmp_path, **overrides):
    paper={"gross_notional_usd":1000,"max_open_positions":1,
        "entry_thresholds_bps":[100,150,200],"allowed_regimes":["POST_20260720"],
        "exit_spread_bps":20,"max_holding_seconds":86400,"fee_bps_per_leg":0}
    paper.update(overrides)
    live={"stale_after_ms":3000,"max_cross_exchange_skew_ms":1500}
    engine=PaperEngine(paper,live,tmp_path)
    for ex in engine.connected: engine.connected[ex]=True;engine.connection_ids[ex]="c"
    return engine


def _seed_neutral(engine, ts=NOW):
    for exchange in engine.connected:
        engine.quotes[exchange]=_quote(exchange,100,101,ts=ts)


def test_all_five_parsers_standardize_bbo_and_sequences():
    cases=[
        (parse_binance,{"e":"bookTicker","E":1770000000000,"u":9,"b":"100","B":"2","a":"101","A":"3"}),
        (parse_bitget,{"arg":{"channel":"ticker"},"data":[{"bidPr":"100","bidSz":"2","askPr":"101","askSz":"3","ts":"1770000000000"}]}),
        (parse_gate,{"channel":"futures.book_ticker","event":"update","time_ms":1770000000000,"result":{"b":"100","B":"2","a":"101","A":"3","u":9}}),
        (parse_hyperliquid,{"channel":"l2Book","data":{"time":1770000000000,"levels":[[{"px":"100","sz":"2"}],[{"px":"101","sz":"3"}]]}}),
        (parse_okx,{"arg":{"channel":"books5"},"data":[{"ts":"1770000000000","seqId":9,"bids":[["100","2","0","1"]],"asks":[["101","3","0","1"]]}]}),
    ]
    expected=["binance","bitget","gate","hyperliquid","okx"]
    for exchange,(parser,message) in zip(expected,cases):
        q=parser(message,"S",NOW,7,"c")
        assert q.exchange==exchange and (q.bid,q.ask,q.bid_size,q.ask_size)==(100,101,2,3)
        assert q.sequence>0 and q.exchange_ts.tzinfo is not None and q.receive_ts==NOW
    assert cases[1][0](cases[1][1],"S",NOW,7,"c").sequence_source=="connection_local"


def test_entry_uses_long_ask_short_bid_and_total_notional_cap(tmp_path):
    e=_engine(tmp_path)
    _seed_neutral(e)
    e.quotes["binance"]=_quote("binance",99,100)
    e.quotes["gate"]=_quote("gate",102,103)
    e.evaluate(NOW)
    assert len(e.positions)==1
    p=e.positions[0]
    assert (p.long_exchange,p.short_exchange)==("binance","gate")
    assert p.entry_spread_bps==200
    assert p.long_entry_ask==100 and p.short_entry_bid==102
    assert p.gross_notional_usd<=1000.0000001


def test_close_uses_long_bid_and_short_ask(tmp_path):
    e=_engine(tmp_path)
    _seed_neutral(e)
    e.quotes["binance"]=_quote("binance",99,100)
    e.quotes["gate"]=_quote("gate",102,103)
    e.evaluate(NOW); p=e.positions[0]
    later=NOW+timedelta(seconds=1)
    e.quotes["binance"]=_quote("binance",101,102,ts=later,seq=2)
    e.quotes["gate"]=_quote("gate",101.1,101.2,ts=later,seq=2)
    e.evaluate(later)
    assert not e.positions and len(e.trades)==1
    trade=e.trades[0]
    assert trade.long_exit_bid==101 and trade.short_exit_ask==101.2
    expected=p.quantity*((101-100)+(102-101.2))
    assert abs(trade.gross_pnl_usd-expected)<1e-9


def test_disconnect_stale_skew_and_single_leg_block_new_signal(tmp_path):
    e=_engine(tmp_path)
    e.connected["binance"]=False
    e.quotes["binance"]=_quote("binance",99,100)
    e.quotes["gate"]=_quote("gate",102,103)
    assert "disconnected:binance" in e.pair("binance","gate",NOW)[1]
    e.connected["binance"]=True
    e.quotes["binance"]=_quote("binance",99,100,ts=NOW-timedelta(seconds=4))
    assert any(x.startswith("stale_") for x in e.pair("binance","gate",NOW)[1])
    e.quotes["binance"]=_quote("binance",99,100,ts=NOW-timedelta(seconds=2))
    assert "cross_exchange_time_skew" in e.pair("binance","gate",NOW)[1]
    e.quotes.pop("binance")
    assert "missing_leg:binance" in e.pair("binance","gate",NOW)[1]


def test_bbo_size_and_frozen_regime_guards(tmp_path):
    e=_engine(tmp_path)
    _seed_neutral(e)
    e.quotes["binance"]=_quote("binance",99,100,ask_size=.01)
    e.quotes["gate"]=_quote("gate",102,103,bid_size=.01)
    e.evaluate(NOW)
    assert not e.positions and e.blocked_counts["insufficient_bbo_size"]>0
    pre=NOW.replace(day=15)
    _seed_neutral(e,pre)
    e.quotes["binance"]=_quote("binance",99,100,ts=pre)
    e.quotes["gate"]=_quote("gate",102,103,ts=pre)
    e.evaluate(pre)
    assert not e.positions and e.blocked_counts["regime_filtered"]>0


def test_storage_archives_raw_and_normalized_parquet(tmp_path):
    async def scenario():
        storage=BBOStorage(.01,1,tmp_path/"raw",tmp_path/"bbo")
        task=asyncio.create_task(storage.run())
        await storage.raw("binance",NOW,"c",'{"x":1}')
        await storage.put(_quote("binance",99,100))
        await asyncio.sleep(.02)
        await storage.put(_quote("binance",99.1,100.1,seq=2))
        await storage.queue.put(None);await task
    asyncio.run(scenario())
    raw=next((tmp_path/"raw").rglob("*.ndjson"))
    assert json.loads(raw.read_text())["raw_message"]=='{"x":1}'
    paths=list((tmp_path/"bbo").rglob("*.parquet"));assert len(paths)==1
    frame=pd.read_parquet(paths[0]);assert len(frame)==2
    assert {"bid","ask","bid_size","ask_size","exchange_ts","receive_ts","sequence"}<=set(frame)


def test_daily_report_survives_ledger_reload(tmp_path):
    e=_engine(tmp_path);e.save()
    restored=PaperEngine(e.paper,e.live,tmp_path)
    md,csv=generate_daily_report(restored,NOW.date())
    assert "PAPER ONLY" in md.read_text() and csv.exists()


def test_any_venue_disconnect_blocks_all_new_signals(tmp_path):
    e=_engine(tmp_path);_seed_neutral(e)
    e.quotes["binance"]=_quote("binance",99,100)
    e.quotes["gate"]=_quote("gate",102,103)
    e.connected["okx"]=False
    e.evaluate(NOW)
    assert not e.positions and e.blocked_counts["disconnected:okx"]==1
