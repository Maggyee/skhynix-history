.PHONY: quick corrected-history analysis-15m duration-fix collect-1m monitor-1m report-live-1m test
quick:
	mkdir -p data/raw data/normalized reports/charts logs
	uv run --extra dev skhynix-research quick --start 2026-06-10T05:50:00Z --end now

corrected-history: quick
	uv run --extra dev skhynix-research analysis-15m --start 2026-06-10T06:00:00Z --end now

analysis-15m:
	mkdir -p data/raw data/normalized reports/charts reports_15m/charts logs
	uv run --extra dev skhynix-research analysis-15m --start 2026-06-10T06:00:00Z --end now

duration-fix: corrected-history

collect-1m:
	uv run skhynix-research collect-1m

monitor-1m:
	uv run skhynix-research monitor-1m

report-live-1m:
	uv run skhynix-research report-live-1m

test:
	uv run --extra dev pytest -q
