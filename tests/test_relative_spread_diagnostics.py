from __future__ import annotations

import pandas as pd

import skhynix_research.fifteen_minute as fm
from skhynix_research.relative_spread_diagnostics import (
    SUMMARY_COLUMNS,
    compute_raw_directed_spread,
    compute_residual_edge,
    compute_residual_spread,
    compute_structural_baseline,
    make_relative_spread_summary_table,
)


def _series(values, *, start="2026-01-01T00:00Z", freq="15min"):
    return pd.Series(values, index=pd.date_range(start, periods=len(values), freq=freq))


def test_directed_spread_has_correct_sign_and_magnitude():
    a = _series([101.0, 99.0])
    b = _series([99.0, 101.0])
    result = compute_raw_directed_spread(a, b)
    assert result.iloc[0] == 200.0
    assert result.iloc[1] == -200.0


def test_baseline_is_causal_and_excludes_current_observation():
    original = _series([1.0, 3.0, 100.0, 8.0])
    baseline = compute_structural_baseline(original, "30min")
    assert baseline.iloc[2] == 2.0
    changed_future = original.copy()
    changed_future.iloc[3] = 1_000_000.0
    pd.testing.assert_series_equal(
        baseline.iloc[:3], compute_structural_baseline(changed_future, "30min").iloc[:3]
    )


def test_baseline_resets_after_timestamp_gap():
    index = pd.to_datetime(
        [
            "2026-01-01T00:00Z",
            "2026-01-01T00:15Z",
            "2026-01-01T00:30Z",
            "2026-01-01T01:00Z",
            "2026-01-01T01:15Z",
            "2026-01-01T01:30Z",
        ]
    )
    spread = pd.Series([1, 2, 3, 10, 20, 30], index=index, dtype=float)
    baseline = compute_structural_baseline(spread, "30min")
    assert pd.isna(baseline.loc["2026-01-01T01:00Z"])
    assert baseline.loc["2026-01-01T01:30Z"] == 15.0


def test_insufficient_history_is_nan():
    baseline = compute_structural_baseline(_series([1.0, 2.0]), "24h")
    assert baseline.isna().all()


def test_residual_and_diagnostic_edge_formulas():
    raw = _series([50.0])
    baseline = _series([10.0])
    residual = compute_residual_spread(raw, baseline)
    edge = compute_residual_edge(residual, 20, 5)
    assert residual.iloc[0] == 40.0
    assert edge.iloc[0] == 15.0


def _diagnostic_frame():
    index = pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC")
    return pd.DataFrame(
        {
            "spread_bps": [10.0, 30.0, 70.0],
            "baseline_24h_status": ["insufficient_history", "ok", "ok"],
            "residual_24h_bps": [float("nan"), 20.0, 60.0],
            "net_residual_edge_24h_bps": [float("nan"), -20.0, 20.0],
            "baseline_72h_status": ["insufficient_history", "insufficient_history", "ok"],
            "residual_72h_bps": [float("nan"), float("nan"), 50.0],
            "net_residual_edge_72h_bps": [float("nan"), float("nan"), 10.0],
        },
        index=index,
    )


def test_summary_table_has_complete_schema_and_is_deterministic():
    diagnostics = {"gate/binance": _diagnostic_frame()}
    first = make_relative_spread_summary_table(diagnostics)
    second = make_relative_spread_summary_table(diagnostics)
    assert list(first.columns) == SUMMARY_COLUMNS
    assert set(first.baseline_window) == {"24h", "72h"}
    pd.testing.assert_frame_equal(first, second)


def test_quick_report_embeds_relative_spread_section_and_chart(monkeypatch, tmp_path):
    monkeypatch.setattr(fm, "R15", tmp_path)
    relative_dir = tmp_path / "relative_spread"
    relative_dir.mkdir()
    # A valid image is unnecessary for the HTML embedder; it embeds file bytes.
    (relative_dir / "gate_binance_raw_vs_baseline.png").write_bytes(b"png")
    summary = make_relative_spread_summary_table({"gate/binance": _diagnostic_frame()})
    fm._write_15m_html("report", pd.DataFrame(), pd.DataFrame(), summary, ["gate/binance"])
    output = (tmp_path / "quick_report_15m.html").read_text()
    assert "Relative Spread Diagnostics" in output
    assert "Assumed cost = 20 bps, exit buffer = 20 bps" in output
    assert "gate_binance_raw_vs_baseline" in output
    assert "data:image/png;base64" in output


def test_relative_module_does_not_change_backtest_event_logic():
    bars = pd.DataFrame(
        {
            "open_time": pd.date_range("2026-01-01", periods=3, freq="15min", tz="UTC"),
            "abs_spread_bps": [25.0, 30.0, 5.0],
            "spread_bps": [25.0, 30.0, 5.0],
        }
    )
    before = fm._events_15m(bars, 20)
    compute_structural_baseline(bars.set_index("open_time").spread_bps, "30min")
    assert fm._events_15m(bars, 20) == before
