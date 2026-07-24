.PHONY: quick corrected-history analysis-15m gate-regime-15m high-threshold-walk-forward higher-bps-study duration-fix collect-1m monitor-1m report-live-1m paper-bbo test
quick:
	mkdir -p data/raw data/normalized reports/charts logs
	uv run --extra dev skhynix-research quick --start 2026-06-10T05:50:00Z --end now

corrected-history: quick
	uv run --extra dev skhynix-research analysis-15m --start 2026-06-10T06:00:00Z --end now

analysis-15m:
	mkdir -p data/raw data/normalized reports/charts reports_15m/charts logs
	uv run --extra dev skhynix-research analysis-15m --start 2026-06-10T06:00:00Z --end now

gate-regime-15m:
	mkdir -p reports_15m/charts
	uv run python -m skhynix_research.gate_regime_15m

high-threshold-walk-forward:
	mkdir -p reports_high_threshold_walk_forward
	uv run skhynix-research high-threshold-walk-forward

higher-bps-study:
	mkdir -p reports_higher_bps_study/charts
	uv run skhynix-research higher-bps-study

duration-fix: corrected-history

collect-1m:
	uv run skhynix-research collect-1m

monitor-1m:
	uv run skhynix-research monitor-1m

report-live-1m:
	uv run skhynix-research report-live-1m

paper-bbo:
	uv run skhynix-research paper-bbo

test:
	uv run --extra dev pytest -q
