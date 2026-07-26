# Methodology

This study uses **native 1-minute trade OHLC bars**. It does not use mark/index
prices, reconstructed 15-minute bars, forward filling, interpolation, or
synthetic timestamps. It is an OHLC execution proxy, not historical BBO and not
an executable backtest.

- Run time: 2026-07-26T05:06:13.245385+00:00
- Dynamic Gate start: 2026-07-16T18:34:00+00:00
- Spread: `20000 * (A - B) / (A + B)` bps.
- Signal: completed close crossing causal P75 (and P25 in the two-sided sensitivity).
- Statistics exclude the current value with `shift(1)`; rolling histories reset after gaps.
- Execution proxy: next contiguous minute open; the primary entry rechecks the frozen threshold.
- Primary exit: crossing the mean frozen at entry, then next contiguous minute open.
- Account PnL: `0.5 * (long_return + short_return) * 10000 - 20 bps`.
- Funding includes only actual events satisfying `entry < funding_time < exit`.
- Natural right-censored and gap events are not assigned terminal PnL.
- `PAIR_NATIVE_WINDOW` is primary; `COMMON_FOUR_WINDOW` is a sensitivity.
- Day-block intervals use deterministic seed 20260726.

The non-duplicated scenario set covers all requested history, side, entry,
center, and maximum-hold comparisons.
