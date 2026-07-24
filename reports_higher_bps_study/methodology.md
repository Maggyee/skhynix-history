# Methodology

## Scope and labels

This is **HISTORICAL_ROLLING_PSEUDO_OOS** research, not an executable backtest and not a live strategy. Inputs are native 15-minute **trade OHLC**. Pair bars require exact common timestamps; no filling, resampling, event-peak fills, or future values are used. A completed close confirms a signal and only the next contiguous 15-minute open is an execution proxy.

The first 672 bars (7 days) are training/warm-up and each following 192-bar (2-day) test block freezes all predeclared parameters. Fixed thresholds, 24h/72h windows, costs and exits are independently reported; none is selected from test outcomes.

## Triggers and exits

- Raw directed spread: `20000 * (price_A-price_B)/(price_A+price_B)`; trigger on absolute spread at least the threshold.
- Residual: directed spread minus a trailing median of **only earlier bars**. Declared windows are 24h and 72h. MAD and same-sign ratio use the identical prior window. Any missing pair bar invalidates the rolling window until all 96/288 observations reaccumulate.
- Exits are independently evaluated: raw spread below entry threshold; residual within 20 bps; residual within 25% of entry residual. Exit is also confirmed at close and executed at the following contiguous open.

## Attribution and uncertainty

Price PnL marks both legs from entry opens to exit opens. Funding includes only public settlement events strictly after entry and strictly before exit. `gross_combined = price + funding`; one assumed total cost of 0/20/40/58/80 bps is then deducted. These are research assumptions, not any user's fee tier. Funding-minus-cost is not called a standalone funding strategy because both-leg basis risk remains.

All incomplete and rejected outcomes retain explicit status. Only `REALIZED` rows enter PnL statistics. The primary mean confidence interval resamples whole UTC day blocks (1000 draws). Candidate language additionally requires at least 30 realized events, positive mean and median, a day-block lower bound not materially below zero, consistent 20/40 bps direction, multiple contributing dates, acceptable censoring and explainable holding/MAE risk.

## Gate filter audit

Gate pairs use causal labels only. Non-Gate pairs have no Gate regime filter and are reported separately. The rolling audit fix preserves the declared label thresholds but computes 24h median/MAD/sign persistence from one identical window and requires a full 24h re-warm after any gap.

## BBO boundary

Historical 15-minute OHLC cannot reconstruct bid/ask, quote age, timestamp skew or first-level capacity. Future BBO paper observations start only after the public collector runs and use long ask/short bid to enter, long bid/short ask to exit.
