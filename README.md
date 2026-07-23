# SKHYNIX perpetual historical research

Public-API-only research pipeline for Hyperliquid, Binance, Bitget, OKX and Gate.
Data is cached under `data/raw`, normalized to Parquet/DuckDB, and reported in
Chinese under `reports`. Historical minute bars are proxies, not executable BBO.

```bash
uv run skhynix-research discover
uv run skhynix-research download --start 2026-06-10T05:50:00Z --end now
uv run skhynix-research analyze
uv run skhynix-research report
make quick
```

