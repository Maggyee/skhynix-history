import pandas as pd

from skhynix_research import live_1m


def _row(exchange="binance", minute="2026-07-23T10:00:00Z", close=100.0):
    t = pd.Timestamp(minute)
    return {"exchange":exchange, "symbol":"SKHYNIXUSDT", "price_type":"trade", "open_time":t,
        "close_time":t+pd.Timedelta(minutes=1)-pd.Timedelta(milliseconds=1), "open":close, "high":close,
        "low":close, "close":close, "volume_base":1.0, "volume_quote":close, "source_endpoint":"official",
        "retrieved_at":"2026-07-23T10:01:08+00:00", "raw_file":"raw.json", "native_interval":"1m", "interval_minutes":1}


def test_closed_window_excludes_open_bar():
    start, end = live_1m.closed_minute_window("2026-07-23T10:07:42Z", 5)
    assert start == pd.Timestamp("2026-07-23T10:02:00Z")
    assert end == pd.Timestamp("2026-07-23T10:07:00Z")


def test_partition_upsert_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(live_1m, "PRICES_ROOT", tmp_path/"prices")
    live_1m.upsert_prices([_row(close=100)])
    live_1m.upsert_prices([_row(close=101)])
    out = live_1m.read_prices()
    assert len(out) == 1
    assert out.iloc[0].close == 101


def test_funding_partition_upsert_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(live_1m,"FUNDING_ROOT",tmp_path/"funding")
    row={"exchange":"okx","symbol":"x","funding_time":pd.Timestamp("2026-07-23T16:00Z"),
        "funding_rate":-0.001,"settlement_status":"realized","source_endpoint":"official",
        "retrieved_at":"2026-07-23T16:01:00Z","raw_file":"raw.ndjson"}
    live_1m.upsert_funding([row]);row["funding_rate"]=-0.002;row["retrieved_at"]="2026-07-23T16:02:00Z"
    live_1m.upsert_funding([row]);out=live_1m.read_funding()
    assert len(out)==1
    assert out.iloc[0].funding_rate==-0.002


def test_monitor_detects_missing_minute(tmp_path, monkeypatch):
    monkeypatch.setattr(live_1m, "DATA_ROOT", tmp_path)
    monkeypatch.setattr(live_1m, "PRICES_ROOT", tmp_path/"prices")
    monkeypatch.setattr(live_1m, "FUNDING_ROOT", tmp_path/"funding")
    monkeypatch.setattr(live_1m, "RUNS_PATH", tmp_path/"runs.csv")
    monkeypatch.setattr(live_1m, "MONITOR_PATH", tmp_path/"monitor.csv")
    monkeypatch.setattr(live_1m, "FUNDING_MONITOR_PATH", tmp_path/"funding_monitor.csv")
    monkeypatch.setattr(live_1m, "STATUS_PATH", tmp_path/"status.json")
    live_1m.upsert_prices([_row(minute="2026-07-23T10:00Z"), _row(minute="2026-07-23T10:02Z")])
    live_1m._append_runs([{"cycle_id":"x", "exchange":"binance", "price_type":"trade", "success":True}])
    monitor, _ = live_1m.build_monitor("2026-07-23T10:03:10Z")
    row = monitor[(monitor.exchange=="binance")&(monitor.price_type=="trade")].iloc[0]
    assert row.missing_bar_count == 1
    assert row.status == "CHECK"
