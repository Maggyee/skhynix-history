## Summary

Adds traceable BBO schema v2 capacity data, time-versioned public product metadata,
strict legacy-capacity audit, and continuous public settled-funding injection into the
existing paper engine. This changes data/runtime plumbing only.

## Validation coverage

- `collect-live-bbo --duration-seconds 600` completed for all five venues.
- Capacity validity in new BBOs: Binance 100%, Bitget 100%, Gate 100%, Hyperliquid
  100%, OKX 100%; all five had zero parse errors and zero stale observations.
- Metadata parsing: all five `VALID`; observed multipliers were Binance 1,
  Bitget 0.01, Gate 0.001, Hyperliquid 1, and OKX 1. These are timestamped public
  observations, not hard-coded assumptions.
- Bitget reconnected four times after public WebSocket close-frame failures; the other
  four venues did not reconnect during either 600-second run.
- Legacy audit: 13,718,080 rows inspected; 163,904 rows (1.1948%) have safe,
  time-bracketed stable-multiplier evidence. The remaining 13,554,176 rows stay
  `CAPACITY_UNKNOWN`; no historical part was mutated.
- Funding injection is now continuous in `paper-bbo`. During the 600-second test it
  injected one newly observed settled event and rejected 50 overlapping duplicates;
  the persisted cumulative processed count was 91.
- Runtime storage growth during the paper test was 7,254,393 bytes over 600 seconds,
  approximately 0.725 MB/minute. Disk status remained `WRITABLE`, with no watermark
  blocks. Actual long-run growth depends on venue message rates and retention/downsampling.
- Funding clock collection returned `VALID` for all five venues.
- Test suite: 163 passed; `git diff --check` passed.

## Safety and scope

- Entry thresholds, confirmation duration, exit threshold, holding limit, notional cap,
  fees, slippage, safety buffer, pair list, and allowed regimes are unchanged.
- No credentials, private interfaces, accounts, authenticated channels, or real-order
  path were added. Fills remain paper simulations at public BBOs.
