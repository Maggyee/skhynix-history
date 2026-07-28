from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

import skhynix_research.paper_trading as pt
from skhynix_research.live_bbo import (BBO, CAPACITY_UNKNOWN, CAPACITY_VALID,
    SCHEMA_VERSION, SIZE_UNIT_OK, SIZE_UNIT_UNKNOWN)

NOW = pd.Timestamp("2026-07-24T12:00:00Z").to_pydatetime()


def quote(exchange, bid, ask, *, native_size=100, multiplier=1.0, ts=NOW, seq=1,
          connection="c", status=SIZE_UNIT_OK):
    usable = status == SIZE_UNIT_OK
    bid_qty = native_size * multiplier if usable else float("nan")
    ask_qty = native_size * multiplier if usable else float("nan")
    return BBO(exchange, "SKHYNIX", bid, ask, native_size, native_size, ts, ts, seq,
        "native", connection, "CONTRACT", multiplier if usable else float("nan"),
        bid_qty, ask_qty, bid_qty * bid, ask_qty * ask, status, f"{connection}:{seq}",
        SCHEMA_VERSION,"metadata-test",CAPACITY_VALID if usable else CAPACITY_UNKNOWN,
        "" if usable else SIZE_UNIT_UNKNOWN)


def settings(**overrides):
    result = {"pairs":["binance/gate", "bitget/gate", "gate/hyperliquid", "gate/okx"],
        "allowed_regimes":["NORMAL", "TRANSIENT_DISLOCATION"],
        "entry_thresholds_bps":[100,150,200], "confirmation_seconds":5,
        "gross_notional_usd":1000, "max_open_positions":1, "exit_spread_bps":20,
        "max_holding_seconds":86400, "taker_fee_bps":{x:10 for x in pt.EXCHANGES},
        "slippage_bps_per_fill":2, "safety_buffer_bps":10}
    result.update(overrides)
    return result


def engine(tmp_path, regime="NORMAL", **overrides):
    e = pt.PaperEngine(settings(**overrides),
        {"stale_after_ms":3000,"max_cross_exchange_skew_ms":1500}, tmp_path,
        lambda _: regime)
    for exchange in pt.EXCHANGES:
        e.connected[exchange] = True; e.connection_ids[exchange] = "c"
    return e


def set_edge(e, ts=NOW, seq=1, long_ask=100, short_bid=102, native_size=100,
             multiplier=1.0, status=SIZE_UNIT_OK):
    e.quotes["binance"] = quote("binance", long_ask-1, long_ask,
        native_size=native_size,multiplier=multiplier,ts=ts,seq=seq,status=status)
    e.quotes["gate"] = quote("gate", short_bid, short_bid+1,
        native_size=native_size,multiplier=multiplier,ts=ts,seq=seq,status=status)


def open_position(e):
    set_edge(e, NOW, 1); e.evaluate(NOW)
    later = NOW + timedelta(seconds=5)
    set_edge(e, later, 2); e.evaluate(later)
    assert len(e.positions) == 1
    return e.positions[0], later


def test_calendar_date_does_not_create_a_regime_label():
    labels = pd.DataFrame({"open_time":["2026-07-15T12:00Z","2026-07-24T12:00Z"],
                           "causal_regime":["NORMAL","NORMAL"]})
    provider = pt.CausalRegimeProvider(labels)
    assert provider("2026-07-15T12:01Z") == provider("2026-07-24T12:01Z") == "NORMAL"
    assert provider("2026-07-16T12:01Z") == "STALE_OR_INVALID"


def test_future_rows_do_not_change_past_causal_lookup():
    past = pd.DataFrame({"open_time":["2026-07-24T12:00Z"],"causal_regime":["NORMAL"]})
    future = pd.concat([past,pd.DataFrame({"open_time":["2026-07-25T12:00Z"],
                                           "causal_regime":["STRUCTURAL_PREMIUM"]})])
    assert pt.CausalRegimeProvider(past)(NOW) == pt.CausalRegimeProvider(future)(NOW)


def test_large_native_contract_count_is_not_used_as_underlying_capacity(tmp_path):
    e = engine(tmp_path); set_edge(e,native_size=1000,multiplier=.001)
    e.evaluate(NOW)
    assert not e.positions and e.blocked_counts["insufficient_normalized_long_capacity"]


def test_contract_multiplier_controls_normalized_capacity(tmp_path):
    too_small = engine(tmp_path/"small"); set_edge(too_small,native_size=10,multiplier=.1)
    too_small.evaluate(NOW)
    enough = engine(tmp_path/"enough"); set_edge(enough,native_size=100,multiplier=.1)
    enough.evaluate(NOW); set_edge(enough,NOW+timedelta(seconds=5),2,native_size=100,multiplier=.1)
    enough.evaluate(NOW+timedelta(seconds=5))
    assert not too_small.positions and len(enough.positions)==1


def test_unknown_size_unit_fails_closed(tmp_path):
    e=engine(tmp_path);set_edge(e,status=SIZE_UNIT_UNKNOWN);e.evaluate(NOW)
    assert not e.positions and e.blocked_counts["size_unit_unknown:binance"]


def test_all_four_taker_fills_are_deducted(tmp_path):
    e=engine(tmp_path);position,opened=open_position(e)
    close=opened+timedelta(seconds=1)
    e.quotes["binance"]=quote("binance",101,102,ts=close,seq=3)
    e.quotes["gate"]=quote("gate",101.1,101.2,ts=close,seq=3)
    e.evaluate(close);trade=e.trades[0]
    expected=-(trade.entry_long_fee_usd+trade.entry_short_fee_usd
               +trade.exit_long_fee_usd+trade.exit_short_fee_usd)
    assert all(x>0 for x in (trade.entry_long_fee_usd,trade.entry_short_fee_usd,
                             trade.exit_long_fee_usd,trade.exit_short_fee_usd))
    assert trade.fee_pnl_usd==pytest.approx(expected)
    assert trade.net_pnl_usd==pytest.approx(trade.gross_price_pnl_usd+trade.funding_pnl_usd
        +trade.fee_pnl_usd-trade.slippage_assumption_usd)


def test_funding_settlement_boundary_and_direction(tmp_path):
    e=engine(tmp_path);position,opened=open_position(e)
    equal=pt.FundingEvent("equal","binance",position.opened_at,.001,100)
    e.on_funding_event(equal);assert position.funding_pnl_usd==0
    after=(opened+timedelta(hours=1)).isoformat()
    e.on_funding_event(pt.FundingEvent("long","binance",after,.001,100))
    e.on_funding_event(pt.FundingEvent("short","gate",after,.002,102))
    assert position.funding_pnl_usd==pytest.approx(
        -position.quantity*100*.001+position.quantity*102*.002)
    previous=position.funding_pnl_usd
    e.on_funding_event(pt.FundingEvent("short","gate",after,.002,102))
    assert position.funding_pnl_usd==previous


def test_negative_funding_reverses_long_and_short_cashflows(tmp_path):
    e=engine(tmp_path);position,opened=open_position(e);after=(opened+timedelta(minutes=1)).isoformat()
    e.on_funding_event(pt.FundingEvent("negative-long","binance",after,-.001,100))
    long_delta=position.funding_pnl_usd
    e.on_funding_event(pt.FundingEvent("negative-short","gate",after,-.001,100))
    assert long_delta>0 and position.funding_pnl_usd==pytest.approx(0)


def test_non_position_exchange_does_not_change_funding(tmp_path):
    e=engine(tmp_path);position,opened=open_position(e)
    e.on_funding_event(pt.FundingEvent("other","okx",(opened+timedelta(minutes=1)).isoformat(),.001,float("nan")))
    assert position.funding_pnl_usd==0 and not position.funding_event_ids


def test_delayed_event_updates_closed_trade_once_and_excludes_close_boundary(tmp_path):
    e=engine(tmp_path,max_holding_seconds=1);_,opened=open_position(e)
    close=opened+timedelta(seconds=2)
    e.quotes["binance"]=quote("binance",98,99,ts=close,seq=3)
    e.quotes["gate"]=quote("gate",104,105,ts=close,seq=3);e.evaluate(close)
    trade=e.trades[0];before=trade.net_pnl_usd
    between=opened+timedelta(seconds=1)
    event=pt.FundingEvent("delayed","binance",between.isoformat(),.001,100)
    assert e.on_funding_event(event) is True
    assert trade.funding_pnl_usd<0 and trade.net_pnl_usd<before
    after_once=trade.net_pnl_usd;assert e.on_funding_event(event) is False
    assert trade.net_pnl_usd==after_once
    equal_close=pt.FundingEvent("at-close","binance",trade.closed_at,.001,float("nan"))
    e.on_funding_event(equal_close);assert trade.net_pnl_usd==after_once


def test_single_tick_never_enters(tmp_path):
    e=engine(tmp_path);set_edge(e);e.evaluate(NOW)
    assert not e.positions and e.confirmation is not None


def test_continuous_five_second_confirmation_enters_on_net_edge(tmp_path):
    e=engine(tmp_path);set_edge(e);e.evaluate(NOW)
    set_edge(e,NOW+timedelta(seconds=4),2);e.evaluate(NOW+timedelta(seconds=4))
    assert not e.positions
    set_edge(e,NOW+timedelta(seconds=5),3);e.evaluate(NOW+timedelta(seconds=5))
    p=e.positions[0]
    assert p.estimated_total_cost_bps==58
    assert p.raw_entry_edge_bps==pytest.approx(200)
    assert p.net_entry_edge_bps==pytest.approx(142)
    assert p.threshold_bps==100


def test_disconnect_resets_confirmation_timer(tmp_path):
    e=engine(tmp_path);set_edge(e);e.evaluate(NOW)
    asyncio.run(e.on_status("binance",False,"c"))
    asyncio.run(e.on_status("binance",True,"c2"))
    t5=NOW+timedelta(seconds=5)
    e.quotes["binance"]=quote("binance",99,100,ts=t5,seq=1,connection="c2")
    e.quotes["gate"]=quote("gate",102,103,ts=t5,seq=2)
    e.evaluate(t5);assert not e.positions
    t10=NOW+timedelta(seconds=10)
    e.quotes["binance"]=quote("binance",99,100,ts=t10,seq=2,connection="c2")
    e.quotes["gate"]=quote("gate",102,103,ts=t10,seq=3)
    e.evaluate(t10);assert len(e.positions)==1


def test_structural_or_stale_regime_forbids_entry(tmp_path):
    for regime in ("STRUCTURAL_PREMIUM","STALE_OR_INVALID"):
        e=engine(tmp_path/regime,regime=regime);set_edge(e);e.evaluate(NOW)
        assert not e.positions and e.confirmation is None


def test_max_hold_exit_uses_observed_bid_and_ask_and_normalized_capacity(tmp_path):
    e=engine(tmp_path,max_holding_seconds=1);_,opened=open_position(e)
    close=opened+timedelta(seconds=2)
    e.quotes["binance"]=quote("binance",98,99,ts=close,seq=3)
    e.quotes["gate"]=quote("gate",104,105,ts=close,seq=3)
    e.evaluate(close);trade=e.trades[0]
    assert trade.close_reason=="MAX_HOLD"
    assert trade.long_exit_bid==98 and trade.short_exit_ask==105


def test_ledger_restart_preserves_position_and_funding_state(tmp_path):
    e=engine(tmp_path);position,opened=open_position(e)
    event=pt.FundingEvent("funding-1","binance",(opened+timedelta(hours=1)).isoformat(),.001,100)
    e.on_funding_event(event)
    restored=pt.PaperEngine(e.paper,e.live,tmp_path,lambda _:"NORMAL")
    assert restored.positions[0].position_id==position.position_id
    assert restored.positions[0].funding_pnl_usd==position.funding_pnl_usd
    assert restored.processed_funding_event_ids=={"funding-1"}


def test_daily_report_calls_unfunded_result_price_only(tmp_path):
    e=engine(tmp_path);_,opened=open_position(e)
    close=opened+timedelta(seconds=1)
    e.quotes["binance"]=quote("binance",101,102,ts=close,seq=3)
    e.quotes["gate"]=quote("gate",101,101.2,ts=close,seq=3)
    e.evaluate(close)
    md,csv=pt.generate_daily_report(e,NOW.date())
    assert pt.PRICE_ONLY_BEFORE_FUNDING in md.read_text()
    assert pd.read_csv(csv).pnl_scope.iloc[0]==pt.PRICE_ONLY_BEFORE_FUNDING


def test_source_has_no_authentication_account_or_real_order_path():
    source=Path(pt.__file__).read_text().lower()
    forbidden=("api_key","api_secret","place_order","submit_order","account_id")
    assert not any(token in source for token in forbidden)
