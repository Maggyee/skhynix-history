from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import skhynix_research.live_bbo as lb

NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "live_bbo"
SYMBOLS = {"binance":"SKHYNIXUSDT","bitget":"SKHYNIXUSDT","gate":"SKHYNIX_USDT",
           "hyperliquid":"xyz:SKHX","okx":"SKHYNIX-USDT-SWAP"}
PARSERS = {"binance":lb.parse_binance,"bitget":lb.parse_bitget,"gate":lb.parse_gate,
           "hyperliquid":lb.parse_hyperliquid,"okx":lb.parse_okx}


def fixture(exchange):
    return json.loads((FIXTURES/f"{exchange}.json").read_text())


def metadata(exchange):
    return lb.parse_product_metadata(exchange,SYMBOLS[exchange],fixture(exchange)["metadata"],
                                     f"fixture:{exchange}")


def quote(exchange="binance",seq=1):
    f=fixture(exchange);message=f["bbo"]
    if exchange=="binance": message["u"]=seq
    elif exchange=="gate": message["result"]["u"]=seq
    elif exchange=="okx": message["data"][0]["seqId"]=seq
    return PARSERS[exchange](message,SYMBOLS[exchange],NOW,seq,"c",metadata(exchange))


def test_sanitized_fixtures_cover_ack_bbo_heartbeat_and_metadata():
    for exchange in lb.EXCHANGES:
        f=fixture(exchange)
        assert {"subscription_ack","heartbeat","bbo","metadata"}<=set(f)


def test_all_five_parsers_normalize_size_timestamp_sequence_and_trace():
    for exchange in lb.EXCHANGES:
        q=quote(exchange,7)
        assert q.exchange==exchange and q.bid<=q.ask
        assert q.exchange_ts.tzinfo is not None and q.receive_ts==NOW
        assert q.sequence>0 and q.raw_message_id==f"c:{q.sequence}"
        assert q.size_unit_status==lb.SIZE_UNIT_OK
        assert q.normalized_underlying_bid_qty==pytest.approx(q.bid_size*q.contract_multiplier)
        assert q.normalized_underlying_ask_qty==pytest.approx(q.ask_size*q.contract_multiplier)
        assert q.bid_notional_usd==pytest.approx(q.bid*q.normalized_underlying_bid_qty)
        assert q.ask_notional_usd==pytest.approx(q.ask*q.normalized_underlying_ask_qty)
    assert quote("bitget").sequence_source=="connection_local"


def test_metadata_units_and_multipliers_are_venue_specific():
    expected={"binance":("BASE_ASSET",1.0),"bitget":("CONTRACT",.01),
              "gate":("CONTRACT",.01),"hyperliquid":("UNDERLYING_ASSET",1.0),
              "okx":("CONTRACT",.1)}
    for exchange,(unit,multiplier) in expected.items():
        meta=metadata(exchange)
        assert (meta.native_size_unit,meta.contract_multiplier)==(unit,multiplier)


def test_unknown_size_unit_fails_closed_for_capacity_fields():
    meta=lb.parse_product_metadata("okx",SYMBOLS["okx"],{"data":[]})
    q=lb.parse_okx(fixture("okx")["bbo"],SYMBOLS["okx"],NOW,1,"c",meta)
    assert q.size_unit_status==lb.SIZE_UNIT_UNKNOWN
    assert np.isnan(q.normalized_underlying_bid_qty) and np.isnan(q.bid_notional_usd)


def test_subscription_heartbeat_reconnect_and_health_metrics():
    async def scenario():
        metas={x:metadata(x) for x in lb.EXCHANGES};monitor=lb.CollectorMonitor({},metas)
        for exchange in lb.EXCHANGES:
            await monitor.on_status(exchange,True,"c")
            monitor.on_message(exchange,fixture(exchange)["subscription_ack"])
            monitor.on_message(exchange,fixture(exchange)["heartbeat"])
            await monitor.on_quote(quote(exchange))
            await monitor.on_status(exchange,False,"c")
            await monitor.on_status(exchange,True,"c2")
            await monitor.on_status(exchange,False,"c2")
        return monitor.snapshot()
    frame=asyncio.run(scenario())
    assert (frame.message_count==2).all() and (frame.parsed_bbo_count==1).all()
    assert (frame.reconnect_count==1).all() and frame.size_unit_status.eq(lb.SIZE_UNIT_OK).all()
    assert frame.median_receive_latency_ms.notna().all()


def test_raw_writer_is_decoupled_and_parquet_is_traceable(tmp_path):
    async def scenario():
        storage=lb.BBOStorage(.01,1,tmp_path/"raw",tmp_path/"bbo")
        await storage.raw("binance",NOW,"c",'{"x":1}')
        assert not list((tmp_path/"raw").rglob("*.ndjson"))
        raw_task=asyncio.create_task(storage.run_raw());bbo_task=asyncio.create_task(storage.run())
        await storage.put(quote("binance",1));await storage.put(quote("binance",2))
        await storage.raw_queue.put(None);await storage.queue.put(None)
        await raw_task;await bbo_task
    asyncio.run(scenario())
    raw=next((tmp_path/"raw").rglob("*.ndjson"))
    assert json.loads(raw.read_text())["raw_message"]=='{"x":1}'
    frame=pd.concat(pd.read_parquet(p) for p in (tmp_path/"bbo").rglob("*.parquet"))
    required={"native_bid_size","native_ask_size","native_size_unit","contract_multiplier",
              "normalized_underlying_bid_qty","normalized_underlying_ask_qty","bid_notional_usd",
              "ask_notional_usd","raw_message_id","sequence_source"}
    assert required<=set(frame.columns) and len(frame)==2


def test_metadata_startup_fetch_archives_raw_and_snapshot(tmp_path,monkeypatch):
    monkeypatch.setattr(lb,"_metadata_request",lambda client,exchange,symbol:fixture(exchange)["metadata"])
    class Client:
        def close(self): pass
    result=lb.fetch_product_metadata(SYMBOLS,tmp_path,Client())
    assert set(result)==set(lb.EXCHANGES)
    assert all(x.usable for x in result.values())
    assert len(list((tmp_path/"raw").glob("*.json")))==5
    assert (tmp_path/"latest.parquet").exists() and (tmp_path/"latest.json").exists()


def test_crossed_or_nonpositive_bbo_is_rejected():
    with pytest.raises(ValueError):
        lb.BBO("x","s",102,101,1,1,NOW,NOW,1,"native","c")
    with pytest.raises(ValueError):
        lb.BBO("x","s",100,101,0,1,NOW,NOW,1,"native","c")


def test_collector_source_has_no_strategy_account_auth_or_order_code():
    source=Path(lb.__file__).read_text().lower()
    forbidden=("paperengine","position(","trade(","api_key","private channel","place_order")
    assert not any(token in source for token in forbidden)
