# Methodology: native 1-minute zero convergence

This is a historical **trade OHLC proxy study, not an executable backtest and not historical BBO**. It does not establish a live-tradable strategy.

- Input: `data/normalized/prices_1m.parquet`; only valid `trade` OHLC for binance, bitget, gate, okx. Hyperliquid, mark and index rows are excluded.
- The input file is the collector's native one-minute product; no 15-minute reconstruction, forward fill, interpolation, or timestamp fabrication is used.
- Actual common window: `2026-07-16 18:34:00+00:00` inclusive to `2026-07-23 07:03:00+00:00` exclusive, calculated from each exchange's valid rows.
- `STRICT_ALL_FOUR_INTERSECTION` requires all four exchanges at the exact minute; `STRICT_PAIR_INTERSECTION` requires the exact two. Both remain inside the common four-exchange boundary.
- Signal: completed close; execution proxy: next contiguous minute open. One pair has at most one position, and an above-threshold episode must reset below threshold before another entry.
- Cost: 20 bps once per realized round trip. Censored or invalid events have no realized PnL. Event bps use the specified sum of equal-notional leg returns; the $1,000 global curve divides this by two because $1,000 is total two-leg gross notional.
- Funding cashflow is `-long funding + short funding`, using real settlements strictly inside `entry < funding_time < exit`. Combined net is price gross + funding cashflow - 20 bps (the sign follows the stated cashflow definition).
- MAE/MFE are close-marked price PnLs after the entry-open proxy, plus the exit-open observation; they are not intrabar executable excursions.
- Day-block confidence intervals resample daily mean event PnLs (500 deterministic bootstrap draws). Sparse samples should not be treated as asymptotic evidence.
- `sum_net_price_pnl_bps` is an event sum, not account return; overlapping pair results cannot be added as portfolio return.
