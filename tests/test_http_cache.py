import json
from datetime import timedelta

import httpx

import skhynix_research.http as cached_http


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fixed_parameters_use_cache_by_default(monkeypatch, tmp_path):
    monkeypatch.setattr(cached_http, "ROOT", tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"version": len(calls)})

    http = cached_http.CachedHTTP("okx", delay=0)
    http.client = _client(handler)
    first, first_path = http.get("https://example.test/candles", {"bar": "15m"})
    second, second_path = http.get("https://example.test/candles", {"bar": "15m"})

    assert first == second == {"version": 1}
    assert first_path == second_path
    assert len(calls) == 1
    assert http.last_from_cache


def test_force_refresh_bypasses_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cached_http, "ROOT", tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"version": len(calls)})

    http = cached_http.CachedHTTP("okx", delay=0)
    http.client = _client(handler)
    first, path = http.get("https://example.test/candles", {"bar": "15m"})
    refreshed, refreshed_path = http.get(
        "https://example.test/candles", {"bar": "15m"}, force_refresh=True
    )

    assert first == {"version": 1}
    assert refreshed == {"version": 2}
    assert path == refreshed_path
    assert len(calls) == 2
    assert not http.last_from_cache
    payload = json.loads((tmp_path / refreshed_path).read_text())
    assert payload["response"] == refreshed
    assert payload["retrieved_at"] == http.last_retrieved_at


def test_expired_ttl_refreshes_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(cached_http, "ROOT", tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"version": len(calls)})

    http = cached_http.CachedHTTP("okx", delay=0, ttl=timedelta(seconds=-1))
    http.client = _client(handler)
    http.get("https://example.test/candles")
    second, _ = http.get("https://example.test/candles")

    assert second == {"version": 2}
    assert len(calls) == 2
