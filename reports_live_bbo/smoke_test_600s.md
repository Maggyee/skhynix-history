# Public live-BBO collector: 600-second smoke test

- Run window: 2026-07-24 03:40:59–03:51:00 UTC
- Command: `uv run skhynix-research collect-bbo --duration-seconds 600`
- Scope: public WebSockets only; no credentials, account access, or order path
- Result: all five venues produced BBO rows; 61,895 BBOs parsed with zero parse errors and zero stale quotes under the configured threshold
- Recovery: Bitget closed four live connections without a close frame; all four reconnects succeeded. The other venues did not reconnect during the run.
- Size metadata: all five products resolved to `SIZE_UNIT_OK`; normalized underlying quantity and USD notional fields were enabled.

`smoke_test_600s.csv` contains the venue-level message, parse, reconnect, latency, stale-rate, and size-unit results. Reconnect counts exclude normal collector shutdown.
