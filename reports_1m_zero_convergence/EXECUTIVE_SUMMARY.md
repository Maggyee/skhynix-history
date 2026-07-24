# Native 1-minute zero-convergence study

> Historical one-minute trade-OHLC proxy only. This is not historical BBO, not an executable backtest, and not evidence of a live-tradable strategy.

## Window and primary result

The dynamically calculated strict boundary is **2026-07-16 18:34:00+00:00 inclusive to 2026-07-23 07:03:00+00:00 exclusive**. The main comparison uses strict all-four timestamps, one-bar confirmation, natural holding, and `ZERO_CROSS_OR_5BPS`.

- Lowest tested trigger with positive event median: **N/A bps**.
- Thresholds whose event-weighted mean and median are both positive: **none**.
- Best pair across the seven independently simulated thresholds: **binance/bitget**, mean 0.18 bps, median 0.18 bps.
- Worst pair: **gate/okx**, mean -10.55 bps, median -8.04 bps.
- Observed natural convergence among all primary signals: **8.18%** (22/269). There are **3 right-censored** and **244 data-gap-during-hold** events. Conditional on the small subset observable through convergence or window end, convergence is 88.00%; that conditional figure must not be generalized across gap-interrupted signals.
- Mean funding attribution on naturally realized primary events: **-0.29 bps** (worsens the mean combined result).
- ONE_POSITION_GLOBAL ends at compounded **$1000.00** and non-compounded **$1000.00** from $1,000 gross two-leg notional.

## Answers and interpretation

1. The minimum positive-median threshold and all thresholds positive on both metrics are stated above.
2. The threshold table below reports all seven independently, including mean, median, win rate, event count and censoring.
3. Mean-versus-median and win-rate differences show whether tails dominate; P05/P95 remain available per pair/configuration in `summary_1m.csv`.
4. The pair table ranks all six pairs. The ranking averages independent threshold scenarios; it is not a portfolio return.
5. The Gate/non-Gate comparison is shown below. It is descriptive and does not establish a causal venue effect.
6. The observed convergence, right-censor and data-gap shares are stated separately above; data failures are not mislabeled as censoring.
7. Each threshold's censor rate is in the threshold table.
8. The confirmation table quantifies whether two-bar confirmation changes count, mean and median.
9. The exit table compares strict cross, 5/10/20 bps bands, and the primary combined rule.
10. The holding-limit table directly compares natural, 240-minute and 1440-minute rules; 60/720 remain in `summary_1m.csv`.
11. Funding is shown independently above and in every detailed summary; price-only net remains primary.
12. The one-position capital result is stated above. Selection uses only the candidate's actual next-open spread, never its future exit or PnL.
13. The sample can motivate a real-time BBO paper experiment only. Trade OHLC is not BBO and these results are not a live-tradable strategy.

## Primary threshold table

```text
 threshold_bps  total_signal_count  realized_event_count  right_censored_count  censor_rate  positive_event_count  win_rate  mean_net_price_pnl_bps  median_net_price_pnl_bps  mean_gross_price_pnl_bps  mean_funding_pnl_bps  mean_net_combined_pnl_bps  median_holding_minutes
            20                 216                    22                     3     0.013889                     6  0.272727               -6.372098                 -5.894543                 13.627902             -0.294959                  -6.667057                    35.0
            50                  19                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
           100                  23                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
           150                   8                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
           200                   3                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
           250                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
           300                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
```

## Primary pair table

```text
          pair  total_signal_count  realized_event_count  right_censored_count  censor_rate  positive_event_count  win_rate  mean_net_price_pnl_bps  median_net_price_pnl_bps  mean_gross_price_pnl_bps  mean_funding_pnl_bps  mean_net_combined_pnl_bps  median_holding_minutes
binance/bitget                  35                     1                     0     0.000000                     1  1.000000                0.177635                  0.177635                 20.177635              0.000000                   0.177635                    31.0
  binance/gate                  40                     6                     0     0.000000                     0  0.000000               -8.635415                 -6.630003                 11.364585              0.440150                  -8.195265                    42.0
   binance/okx                  30                     2                     1     0.033333                     1  0.500000               -1.319681                 -1.319681                 18.680319              0.000000                  -1.319681                    63.5
   bitget/gate                  44                     6                     0     0.000000                     2  0.333333               -7.235875                 -3.837133                 12.764125             -1.755000                  -8.990875                    32.0
    bitget/okx                  54                     4                     1     0.018519                     2  0.500000               -2.712262                 -2.419070                 17.287738              0.000000                  -2.712262                    46.0
      gate/okx                  66                     3                     1     0.015152                     0  0.000000              -10.549211                 -8.038693                  9.450789              0.466667                 -10.082545                    12.0
```

## Gate-related versus non-Gate

```text
       group  pairs  mean_net_bps  median_net_bps  mean_censor_rate
Gate-related      3     -8.806834       -6.630003          0.005051
    non-Gate      3     -1.284770       -1.319681          0.017284
```

## Confirmation comparison

```text
confirmation_policy  total_signal_count  realized_event_count  right_censored_count  censor_rate  positive_event_count  win_rate  mean_net_price_pnl_bps  median_net_price_pnl_bps  mean_gross_price_pnl_bps  mean_funding_pnl_bps  mean_net_combined_pnl_bps  median_holding_minutes
    ONE_BAR_CONFIRM                 269                    22                     3     0.011152                     6  0.272727               -6.372098                 -5.894543                 13.627902             -0.294959                  -6.667057                    35.0
    TWO_BAR_CONFIRM                 213                    10                     1     0.004695                     3  0.300000               -2.028170                 -2.339372                 17.971830             -0.760740                  -2.788910                    43.5
```

## Exit-policy comparison

```text
       exit_policy  total_signal_count  realized_event_count  right_censored_count  censor_rate  positive_event_count  win_rate  mean_net_price_pnl_bps  median_net_price_pnl_bps  mean_gross_price_pnl_bps  mean_funding_pnl_bps  mean_net_combined_pnl_bps  median_holding_minutes
      ZERO_BAND_10                 287                    67                     2     0.006969                     3  0.044776               -9.660668                 -9.104219                 10.339332             -0.952648                 -10.613316                    24.0
      ZERO_BAND_20                 859                   744                     0     0.000000                     0  0.000000              -17.793717                -18.077588                  2.206283             -0.052943                 -17.846660                     1.0
       ZERO_BAND_5                 269                    22                     3     0.011152                     6  0.272727               -6.372098                 -5.894543                 13.627902             -0.294959                  -6.667057                    35.0
        ZERO_CROSS                 266                     8                     3     0.011278                     3  0.375000               -6.952331                 -4.056028                 13.047669              1.165325                  -5.787006                    30.5
ZERO_CROSS_OR_5BPS                 269                    22                     3     0.011152                     6  0.272727               -6.372098                 -5.894543                 13.627902             -0.294959                  -6.667057                    35.0
```

## Natural versus maximum holding limits

```text
 max_holding_minutes  total_signal_count  realized_event_count  right_censored_count  censor_rate  positive_event_count  win_rate  mean_net_price_pnl_bps  median_net_price_pnl_bps  mean_gross_price_pnl_bps  mean_funding_pnl_bps  mean_net_combined_pnl_bps  median_holding_minutes
               240.0                 269                    22                     3     0.011152                     6  0.272727               -6.372098                 -5.894543                 13.627902             -0.294959                  -6.667057                    35.0
              1440.0                 269                    22                     3     0.011152                     6  0.272727               -6.372098                 -5.894543                 13.627902             -0.294959                  -6.667057                    35.0
                 NaN                 269                    22                     3     0.011152                     6  0.272727               -6.372098                 -5.894543                 13.627902             -0.294959                  -6.667057                    35.0
```

## Strict all-four versus pair intersection

```text
                  data_scope  total_signal_count  realized_event_count  right_censored_count  censor_rate  positive_event_count  win_rate  mean_net_price_pnl_bps  median_net_price_pnl_bps  mean_gross_price_pnl_bps  mean_funding_pnl_bps  mean_net_combined_pnl_bps  median_holding_minutes
STRICT_ALL_FOUR_INTERSECTION                 269                    22                     3     0.011152                     6  0.272727               -6.372098                 -5.894543                 13.627902             -0.294959                  -6.667057                    35.0
    STRICT_PAIR_INTERSECTION                 165                    41                     3     0.018182                    17  0.414634               30.129846                 -3.637658                 50.129846             15.235285                  45.365131                   175.0
```

## Six pairs × seven thresholds

```text
          pair  threshold_bps  total_signal_count  realized_event_count  right_censored_count  censor_rate  positive_event_count  win_rate  mean_net_price_pnl_bps  median_net_price_pnl_bps  mean_gross_price_pnl_bps  mean_funding_pnl_bps  mean_net_combined_pnl_bps  median_holding_minutes
binance/bitget             20                  35                     1                     0     0.000000                     1  1.000000                0.177635                  0.177635                 20.177635              0.000000                   0.177635                    31.0
binance/bitget             50                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
binance/bitget            100                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
binance/bitget            150                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
binance/bitget            200                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
binance/bitget            250                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
binance/bitget            300                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
  binance/gate             20                  28                     6                     0     0.000000                     0  0.000000               -8.635415                 -6.630003                 11.364585              0.440150                  -8.195265                    42.0
  binance/gate             50                   1                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
  binance/gate            100                   9                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
  binance/gate            150                   2                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
  binance/gate            200                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
  binance/gate            250                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
  binance/gate            300                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   binance/okx             20                  30                     2                     1     0.033333                     1  0.500000               -1.319681                 -1.319681                 18.680319              0.000000                  -1.319681                    63.5
   binance/okx             50                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   binance/okx            100                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   binance/okx            150                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   binance/okx            200                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   binance/okx            250                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   binance/okx            300                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   bitget/gate             20                  29                     6                     0     0.000000                     2  0.333333               -7.235875                 -3.837133                 12.764125             -1.755000                  -8.990875                    32.0
   bitget/gate             50                   4                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   bitget/gate            100                   9                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   bitget/gate            150                   2                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   bitget/gate            200                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   bitget/gate            250                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
   bitget/gate            300                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
    bitget/okx             20                  49                     4                     1     0.020408                     2  0.500000               -2.712262                 -2.419070                 17.287738              0.000000                  -2.712262                    46.0
    bitget/okx             50                   5                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
    bitget/okx            100                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
    bitget/okx            150                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
    bitget/okx            200                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
    bitget/okx            250                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
    bitget/okx            300                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
      gate/okx             20                  45                     3                     1     0.022222                     0  0.000000              -10.549211                 -8.038693                  9.450789              0.466667                 -10.082545                    12.0
      gate/okx             50                   9                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
      gate/okx            100                   5                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
      gate/okx            150                   4                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
      gate/okx            200                   3                     0                     0     0.000000                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
      gate/okx            250                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
      gate/okx            300                   0                     0                     0          NaN                     0       NaN                     NaN                       NaN                       NaN                   NaN                        NaN                     NaN
```
