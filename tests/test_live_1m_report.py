import pandas as pd

from skhynix_research.live_1m_report import build_live_1m_statistics, strict_common_segments


EXCHANGES = ("binance", "bitget", "gate", "hyperliquid", "okx")


def _prices(times, missing=None):
    rows=[]; missing=missing or set()
    for i,t in enumerate(pd.to_datetime(times,utc=True)):
        for j,exchange in enumerate(EXCHANGES):
            if (exchange,t) in missing: continue
            rows.append({"exchange":exchange,"symbol":"x","price_type":"trade","open_time":t,"close":100+i+j/10})
    return pd.DataFrame(rows)


def test_longest_contiguous_common_segment_does_not_bridge_gap():
    p=_prices(["2026-01-01T00:00Z","2026-01-01T00:01Z","2026-01-01T00:03Z","2026-01-01T00:04Z","2026-01-01T00:05Z"])
    _,segments,window,chosen=strict_common_segments(p)
    assert segments.duration_minutes.tolist()==[2,3]
    assert chosen.duration_minutes==3
    assert window.index.min()==pd.Timestamp("2026-01-01T00:03Z")


def test_statistics_use_only_all_five_100pct_window():
    times=pd.to_datetime(["2026-01-01T00:00Z","2026-01-01T00:01Z","2026-01-01T00:02Z"],utc=True)
    p=_prices(times,missing={("gate",times[2])})
    result=build_live_1m_statistics(p)
    assert result["window"].iloc[0].duration_minutes==2
    assert result["coverage"].coverage_pct.eq(100).all()
    assert result["coverage"].missing_minutes.eq(0).all()
    assert result["pairs"].observations.eq(2).all()
    assert len(result["pairs"])==10
