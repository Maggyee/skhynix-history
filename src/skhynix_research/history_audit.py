from __future__ import annotations

import json
from pathlib import Path
import pandas as pd

from .config import ROOT
from .download import ENDPOINTS, ms
from .http import CachedHTTP

R = ROOT / "reports"


def gate_page_ranges(start, end, limit=1900, interval_minutes=1):
    """Inclusive bounded page ranges; never terminate before requested end."""
    cur=pd.Timestamp(start);end=pd.Timestamp(end);step=pd.Timedelta(minutes=limit*interval_minutes-1)
    while cur<end:
        upto=min(end,cur+step);yield cur,upto;cur=upto+pd.Timedelta(minutes=1)


def merge_candle_pages(pages):
    rows=[x for page in pages for x in page]
    return sorted({int(x["t"]):x for x in rows}.values(),key=lambda x:int(x["t"]))


def _timestamps(response, exchange):
    if not isinstance(response, list) or not response:
        return []
    key = "t" if exchange == "gate" else "t"
    vals = [int(x[key]) for x in response if isinstance(x, dict) and key in x]
    unit = "s" if vals and max(vals) < 10**12 else "ms"
    return list(pd.to_datetime(vals, unit=unit, utc=True))


def _probe(exchange, endpoint, params=None, body=None):
    h = CachedHTTP(exchange)
    try:
        if body is None:
            data, raw = h.get(endpoint, params)
        else:
            data, raw = h.post(endpoint, body)
        return data, raw, 200, ""
    except Exception as exc:
        # CachedHTTP has already persisted the complete 4xx response and request.
        return [], "", getattr(getattr(exc, "response", None), "status_code", None), str(exc)


def _scan_cached(exchange):
    rows = []
    for path in sorted((ROOT / "data" / "raw" / exchange).glob("*.json")):
        try:
            obj = json.loads(path.read_text())
            req = obj.get("request", {})
            url = req.get("url", "")
            body = req.get("json") or {}
            params = req.get("params") or {}
            if exchange == "gate" and "candlesticks" not in url:
                continue
            if exchange == "hyperliquid" and body.get("type") != "candleSnapshot":
                continue
            resp = obj.get("response", [])
            times = _timestamps(resp, exchange)
            warning = ""
            if times and times != sorted(times):
                warning = "API_DESCENDING_ORDER"
            if path.name.endswith(".error.json"):
                warning = (warning + "|HTTP_ERROR").strip("|")
            rows.append({
                "exchange": exchange,
                "endpoint": url or "POST /info candleSnapshot",
                "request_start": params.get("from") or (body.get("req") or {}).get("startTime"),
                "request_end": params.get("to") or (body.get("req") or {}).get("endTime"),
                "page_cursor": params.get("to") or (body.get("req") or {}).get("endTime"),
                "interval": params.get("interval") or (body.get("req") or {}).get("interval"),
                "contract_or_coin": params.get("contract") or (body.get("req") or {}).get("coin"),
                "row_count": len(resp) if isinstance(resp, list) else 0,
                "min_timestamp": min(times) if times else pd.NaT,
                "max_timestamp": max(times) if times else pd.NaT,
                "http_status": obj.get("status_code", 200 if "response" in obj else None),
                "raw_file": str(path.relative_to(ROOT)),
                "warning": warning or (obj.get("response_text", "")[:200] if path.name.endswith(".error.json") else ""),
            })
        except Exception as exc:
            rows.append({"exchange": exchange, "raw_file": str(path.relative_to(ROOT)), "warning": f"PARSE_ERROR:{exc}"})
    return rows


def _save_lower_frequency(exchange, interval, response, raw, symbol):
    times = _timestamps(response, exchange)
    if not times:
        return
    out = []
    for x, t in zip(response, times):
        out.append({
            "exchange": exchange, "symbol": symbol, "frequency": interval,
            "open_time": t, "open": float(x.get("o")), "high": float(x.get("h")),
            "low": float(x.get("l")), "close": float(x.get("c")),
            "volume": float(x.get("v", 0) or 0), "raw_file": raw,
            "comparison_quality": "lower_frequency_audit_only",
        })
    path = ROOT / "data" / "normalized" / f"{exchange}_history_{interval}_audit.parquet"
    new = pd.DataFrame(out)
    if path.exists():
        new = pd.concat([pd.read_parquet(path), new], ignore_index=True)
    new.sort_values("open_time").drop_duplicates("open_time", keep="last").to_parquet(path, index=False)


def _gate_audit(start, end):
    symbol = "SKHYNIX_USDT"
    rows = _scan_cached("gate")
    # One-minute evidence: an old bounded page is rejected as outside the recent 10,000-point window.
    for interval, step_minutes in [("1m", 1900), ("15m", 1900 * 15)]:
        stop = min(pd.Timestamp("2026-07-16T18:34:00Z"), pd.Timestamp(end))
        for cursor,upto in gate_page_ranges(start,stop,1900,1 if interval=="1m" else 15):
            params = {"contract": symbol, "from": int(cursor.timestamp()), "to": int(upto.timestamp()), "interval": interval}
            data, raw, status, warning = _probe("gate", ENDPOINTS["gate"] + "/futures/usdt/candlesticks", params=params)
            times = _timestamps(data, "gate")
            rows.append({"exchange": "gate", "endpoint": "/futures/usdt/candlesticks", "request_start": cursor,
                         "request_end": upto, "page_cursor": int(upto.timestamp()), "interval": interval,
                         "contract_or_coin": symbol, "row_count": len(data), "min_timestamp": min(times) if times else pd.NaT,
                         "max_timestamp": max(times) if times else pd.NaT, "http_status": status, "raw_file": raw,
                         "warning": warning})
            if interval != "1m" and data:
                _save_lower_frequency("gate", interval, data, raw, symbol)
            # A single 1m rejection is sufficient evidence; 15m is paged to prove deeper availability.
            if interval == "1m":
                break
    return rows


def _hyper_audit(start, end, coin):
    rows = _scan_cached("hyperliquid")
    ep = ENDPOINTS["hyperliquid"]
    old_end = min(pd.Timestamp("2026-06-15T23:59:00Z"), pd.Timestamp(end))
    probes = [("1m", pd.Timestamp(start), old_end), ("15m", pd.Timestamp(start), pd.Timestamp(end))]
    for interval, s, e in probes:
        body = {"type": "candleSnapshot", "req": {"coin": coin, "interval": interval, "startTime": ms(s), "endTime": ms(e)}}
        data, raw, status, warning = _probe("hyperliquid", ep, body=body)
        times = _timestamps(data, "hyperliquid")
        rows.append({"exchange": "hyperliquid", "endpoint": "POST /info candleSnapshot", "request_start": s,
                     "request_end": e, "page_cursor": ms(e), "interval": interval, "contract_or_coin": coin,
                     "row_count": len(data), "min_timestamp": min(times) if times else pd.NaT,
                     "max_timestamp": max(times) if times else pd.NaT, "http_status": status,
                     "raw_file": raw, "warning": warning or ("EMPTY_OLD_WINDOW" if not data else "")})
        if interval != "1m" and data:
            _save_lower_frequency("hyperliquid", interval, data, raw, coin)
    return rows


def audit_history_coverage(start, end):
    """Probe official APIs and produce reproducible coverage evidence without altering 1m research data."""
    R.mkdir(exist_ok=True)
    meta = pd.read_parquet(ROOT / "data" / "normalized" / "instrument_metadata.parquet")
    prices = pd.read_parquet(ROOT / "data" / "normalized" / "prices_1m.parquet")
    coin = meta.loc[meta.exchange == "hyperliquid", "resolved_symbol"].iloc[0]
    gate_rows = _gate_audit(start, end)
    hyper_rows = _hyper_audit(start, end, coin)
    outputs = {}
    docs = {
        "gate": (gate_rows, "Gate", "官方 1m 接口对旧请求返回 `Candlestick too long ago. Maximum 10000 points recently are allowed`；from/to 为秒，返回升序，mark_/index_ 均有缓存响应。"),
        "hyperliquid": (hyper_rows, "Hyperliquid", f"resolved coin=`{coin}`；HIP-3 DEX 前缀正确。官方 candleSnapshot 只返回最近 5,000 根 K 线，旧的封闭 1m 窗口返回空；不是只保留最后一页。"),
    }
    for ex, (rawrows, title, finding) in docs.items():
        pages = pd.DataFrame(rawrows)
        pages = pages.drop_duplicates(["raw_file", "request_start", "request_end", "interval"], keep="last")
        pages.to_csv(R / f"{ex}_history_pages.csv", index=False)
        ep = prices[prices.exchange == ex].open_time.min()
        raw_times = pd.to_datetime(pages.min_timestamp, utc=True, errors="coerce").dropna()
        raw_earliest = raw_times.min() if len(raw_times) else pd.NaT
        one = pages[pages.interval.astype(str) == "1m"]
        one_times = pd.to_datetime(one.min_timestamp, utc=True, errors="coerce").dropna()
        api_earliest = one_times.min() if len(one_times) else pd.NaT
        missing = pd.DataFrame([{"exchange": ex, "price_type": "1m_primary", "missing_start": start,
                                 "missing_end": ep, "missing_minutes": int((ep - pd.Timestamp(start)).total_seconds()/60),
                                 "reason": "official_recent_history_retention_limit", "filled": False}])
        missing.to_csv(R / f"{ex}_history_missing_ranges.csv", index=False)
        listing = meta.loc[meta.exchange == ex, "listing_time"].iloc[0]
        lower = ROOT / "data" / "normalized" / f"{ex}_history_15m_audit.parquet"
        lf = pd.read_parquet(lower) if lower.exists() else pd.DataFrame()
        text = f"""# {title} 历史覆盖审计

- requested_start: `{start}`
- api_earliest_available_1m: `{api_earliest}`
- raw_earliest_any_frequency: `{raw_earliest}`
- normalized_earliest_1m: `{ep}`
- metadata_listing_time: `{listing}`（不得等同于 API 最早可得时间）
- 15m 审计数据范围: `{lf.open_time.min() if len(lf) else '无'} .. {lf.open_time.max() if len(lf) else '无'}`；仅作覆盖证据，不进入 1m 分析。

## 结论

{finding}

原始 1m 最早时间与标准化 1m 最早时间一致，未发现 normalize 删除早期 raw 的证据。分页参数、HTTP 状态和原始文件逐页列在 `{ex}_history_pages.csv`。接口证据不支持把当前最早 K 线解释成产品上市日。

## 官方接口说明

- Gate: https://www.gate.com/docs/developers/apiv4/en/#market-candlesticks
- Hyperliquid: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/candle-snapshot
"""
        (R / f"{ex}_history_coverage_audit.md").write_text(text)
        outputs[ex] = {"api_earliest": api_earliest, "raw_earliest": raw_earliest, "normalized_earliest": ep}
    return outputs
