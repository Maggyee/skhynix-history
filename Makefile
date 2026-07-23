.PHONY: quick corrected-history test
quick:
	mkdir -p data/raw data/normalized reports/charts logs
	uv run --extra dev skhynix-research quick --start 2026-06-10T05:50:00Z --end now

corrected-history: quick

test:
	uv run --extra dev pytest -q
