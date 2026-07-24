# Executive summary

## Bottom line

Raising a raw observed-spread threshold does not by itself establish a robust improvement. Mean and median must be read together: positive means with non-positive medians indicate tail dependence, while the highest thresholds often have too few independent realized events. Gate and non-Gate results are separated because Gate's persistent basis and causal filter materially change the event population.

Residual triggers are the cleaner answer to “structural basis or temporary deviation”: the trailing median estimates structural center and the residual measures short-lived displacement. The two declared windows are shown independently; neither is selected after seeing outcomes. Funding is a joint-trade attribution component, not an independent strategy. Across duplicated 40-bps realized scenario rows its absolute contribution ratio is 36.11%.

Costs are assumed total round-trip research scenarios. A positive gross result that fails at 40/58/80 bps is not robust. No historical result here is called tradable: 15-minute opens omit executable bid/ask, depth, latency and skew. Candidate labels mean only “continue future public-BBO paper validation.”

**No scenario passes all candidate criteria (`0` candidates).** Raw 20/50/100 bps legacy scenarios have negative mean and median even at the 20-bps assumption. Raw 150 bps turns positive at 20 bps but has only 19 realized events, 93.0% censoring, a CI crossing zero, and becomes negative at 40 bps. Raw 200 bps has 11 events and a negative median; 250 bps has two events; 300/400/500 have none. Thus the apparent high-threshold improvement is a small-sample/tail effect, not robust evidence.

Residual 50-bps triggers are positive at the 20-bps assumption (55 events for 24h; 58 for 72h) but become negative at 40 bps. Residual 100–200 bps positive cells contain only one to three realized events with censor rates above 98%. Residual triggering is conceptually more appropriate for separating structural basis, but current history and causal Gate data quality do not support a candidate.

## Raw trigger / legacy exit cost matrix

| threshold_bps | assumed_total_cost_bps | realized_event_count | censor_rate | mean_net_bps | median_net_bps | day_block_ci_low | day_block_ci_high |
|---|---|---|---|---|---|---|---|
| 20 | 20 | 683 | 0.37 | -11.10 | -13.71 | -12.25 | -9.75 |
| 20 | 40 | 683 | 0.37 | -31.10 | -33.71 | -32.32 | -29.65 |
| 20 | 58 | 683 | 0.37 | -49.10 | -51.71 | -50.28 | -47.74 |
| 20 | 80 | 683 | 0.37 | -71.10 | -73.71 | -72.36 | -69.77 |
| 50 | 20 | 390 | 0.44 | -9.86 | -12.14 | -11.46 | -8.10 |
| 50 | 40 | 390 | 0.44 | -29.86 | -32.14 | -31.47 | -28.08 |
| 50 | 58 | 390 | 0.44 | -47.86 | -50.14 | -49.45 | -46.16 |
| 50 | 80 | 390 | 0.44 | -69.86 | -72.14 | -71.37 | -67.85 |
| 100 | 20 | 89 | 0.68 | -8.38 | -9.54 | -12.01 | -3.87 |
| 100 | 40 | 89 | 0.68 | -28.38 | -29.54 | -32.29 | -23.68 |
| 100 | 58 | 89 | 0.68 | -46.38 | -47.54 | -49.89 | -41.87 |
| 100 | 80 | 89 | 0.68 | -68.38 | -69.54 | -72.10 | -63.81 |
| 150 | 20 | 19 | 0.93 | 10.84 | 19.68 | -7.21 | 23.34 |
| 150 | 40 | 19 | 0.93 | -9.16 | -0.32 | -27.86 | 3.85 |
| 150 | 58 | 19 | 0.93 | -27.16 | -18.32 | -44.45 | -14.15 |
| 150 | 80 | 19 | 0.93 | -49.16 | -40.32 | -66.45 | -37.41 |
| 200 | 20 | 11 | 0.94 | 3.43 | -7.16 | 2.00 | 5.15 |
| 200 | 40 | 11 | 0.94 | -16.57 | -27.16 | -18.00 | -14.85 |
| 200 | 58 | 11 | 0.94 | -34.57 | -45.16 | -36.00 | -32.85 |
| 200 | 80 | 11 | 0.94 | -56.57 | -67.16 | -58.00 | -54.85 |
| 250 | 20 | 2 | 0.95 | 5.85 | 5.85 | 5.85 | 5.85 |
| 250 | 40 | 2 | 0.95 | -14.15 | -14.15 | -14.15 | -14.15 |
| 250 | 58 | 2 | 0.95 | -32.15 | -32.15 | -32.15 | -32.15 |
| 250 | 80 | 2 | 0.95 | -54.15 | -54.15 | -54.15 | -54.15 |
| 300 | 20 | 0 | 1.00 |  |  |  |  |
| 300 | 40 | 0 | 1.00 |  |  |  |  |
| 300 | 58 | 0 | 1.00 |  |  |  |  |
| 300 | 80 | 0 | 1.00 |  |  |  |  |
| 400 | 20 | 0 | 1.00 |  |  |  |  |
| 400 | 40 | 0 | 1.00 |  |  |  |  |
| 400 | 58 | 0 | 1.00 |  |  |  |  |
| 400 | 80 | 0 | 1.00 |  |  |  |  |
| 500 | 20 | 0 |  |  |  |  |  |
| 500 | 40 | 0 |  |  |  |  |  |
| 500 | 58 | 0 |  |  |  |  |  |
| 500 | 80 | 0 |  |  |  |  |  |

## Residual trigger / baseline-residual exit comparison

| baseline_window_hours | threshold_bps | assumed_total_cost_bps | realized_event_count | censor_rate | mean_net_bps | median_net_bps | day_block_ci_low | day_block_ci_high |
|---|---|---|---|---|---|---|---|---|
| 24 | 20 | 20 | 413 | 0.68 | -10.62 | -12.72 | -11.90 | -9.27 |
| 72 | 20 | 20 | 461 | 0.58 | -10.75 | -12.92 | -12.07 | -9.48 |
| 24 | 20 | 40 | 413 | 0.68 | -30.62 | -32.72 | -32.02 | -29.27 |
| 72 | 20 | 40 | 461 | 0.58 | -30.75 | -32.92 | -32.04 | -29.51 |
| 24 | 50 | 20 | 55 | 0.89 | 7.58 | 5.08 | 1.09 | 13.75 |
| 72 | 50 | 20 | 58 | 0.86 | 8.94 | 10.99 | 0.53 | 15.81 |
| 24 | 50 | 40 | 55 | 0.89 | -12.42 | -14.92 | -18.71 | -5.49 |
| 72 | 50 | 40 | 58 | 0.86 | -11.06 | -9.01 | -18.89 | -4.24 |
| 24 | 100 | 20 | 3 | 0.99 | 62.21 | 54.67 | 62.21 | 62.21 |
| 72 | 100 | 20 | 3 | 0.98 | 42.95 | 64.45 | 32.20 | 64.45 |
| 24 | 100 | 40 | 3 | 0.99 | 42.21 | 34.67 | 42.21 | 42.21 |
| 72 | 100 | 40 | 3 | 0.98 | 22.95 | 44.45 | 12.20 | 44.45 |
| 24 | 150 | 20 | 0 | 1.00 |  |  |  |  |
| 72 | 150 | 20 | 3 | 0.97 | 126.21 | 120.46 | 120.46 | 129.08 |
| 24 | 150 | 40 | 0 | 1.00 |  |  |  |  |
| 72 | 150 | 40 | 3 | 0.97 | 106.21 | 100.46 | 100.46 | 109.08 |
| 24 | 200 | 20 | 0 | 1.00 |  |  |  |  |
| 72 | 200 | 20 | 1 | 0.99 | 144.20 | 144.20 | 144.20 | 144.20 |
| 24 | 200 | 40 | 0 | 1.00 |  |  |  |  |
| 72 | 200 | 40 | 1 | 0.99 | 124.20 | 124.20 | 124.20 | 124.20 |
| 24 | 250 | 20 | 0 | 1.00 |  |  |  |  |
| 72 | 250 | 20 | 0 | 1.00 |  |  |  |  |
| 24 | 250 | 40 | 0 | 1.00 |  |  |  |  |
| 72 | 250 | 40 | 0 | 1.00 |  |  |  |  |
| 24 | 300 | 20 | 0 | 1.00 |  |  |  |  |
| 72 | 300 | 20 | 0 | 1.00 |  |  |  |  |
| 24 | 300 | 40 | 0 | 1.00 |  |  |  |  |
| 72 | 300 | 40 | 0 | 1.00 |  |  |  |  |
| 24 | 400 | 20 | 0 | 1.00 |  |  |  |  |
| 72 | 400 | 20 | 0 | 1.00 |  |  |  |  |
| 24 | 400 | 40 | 0 | 1.00 |  |  |  |  |
| 72 | 400 | 40 | 0 | 1.00 |  |  |  |  |
| 24 | 500 | 20 | 0 | 1.00 |  |  |  |  |
| 72 | 500 | 20 | 0 | 1.00 |  |  |  |  |
| 24 | 500 | 40 | 0 | 1.00 |  |  |  |  |
| 72 | 500 | 40 | 0 | 1.00 |  |  |  |  |

## 40-bps all-pair screening table

| trigger | window_h | exit | threshold | realized | mean40 | median40 | ci | decision |
|---|---|---|---|---|---|---|---|---|
| RESIDUAL_SPREAD | 72 | BASELINE_RESIDUAL_EXIT | 200 | 1 | 124.20 | 124.20 | [124.2, 124.2] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 72 | BASELINE_RESIDUAL_EXIT | 150 | 3 | 106.21 | 100.46 | [100.5, 109.1] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 72 | FRACTIONAL_RESIDUAL_EXIT | 200 | 1 | 91.26 | 91.26 | [91.3, 91.3] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 72 | FRACTIONAL_RESIDUAL_EXIT | 150 | 3 | 78.45 | 85.42 | [75.0, 85.4] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | BASELINE_RESIDUAL_EXIT | 200 | 3 | 47.29 | 34.82 | [31.5, 55.2] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 24 | BASELINE_RESIDUAL_EXIT | 100 | 3 | 42.21 | 34.67 | [42.2, 42.2] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | FRACTIONAL_RESIDUAL_EXIT | 200 | 3 | 33.35 | 33.67 | [31.5, 34.2] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 72 | BASELINE_RESIDUAL_EXIT | 100 | 3 | 22.95 | 44.45 | [12.2, 44.5] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 72 | FRACTIONAL_RESIDUAL_EXIT | 100 | 3 | 18.66 | 31.59 | [5.8, 44.5] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | FRACTIONAL_RESIDUAL_EXIT | 150 | 4 | 18.02 | 12.64 | [12.6, 23.4] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 24 | FRACTIONAL_RESIDUAL_EXIT | 100 | 3 | 17.17 | -0.94 | [17.2, 17.2] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 72 | LEGACY_RAW_EXIT | 150 | 4 | 12.64 | 17.08 | [8.2, 17.1] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | BASELINE_RESIDUAL_EXIT | 150 | 5 | 5.95 | -4.02 | [-4.0, 12.6] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 72 | LEGACY_RAW_EXIT | 100 | 6 | 4.29 | 0.53 | [0.5, 6.2] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 24 | LEGACY_RAW_EXIT | 150 | 1 | 2.62 | 2.62 | [2.6, 2.6] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 24 | LEGACY_RAW_EXIT | 100 | 7 | -0.25 | -37.25 | [-42.0, 15.8] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | FRACTIONAL_RESIDUAL_EXIT | 100 | 14 | -8.06 | -5.22 | [-15.0, -0.1] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | LEGACY_RAW_EXIT | 150 | 19 | -9.16 | -0.32 | [-27.9, 3.9] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RESIDUAL_SPREAD | 72 | LEGACY_RAW_EXIT | 200 | 1 | -9.60 | -9.60 | [-9.6, -9.6] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | LEGACY_RAW_EXIT | 250 | 2 | -14.15 | -14.15 | [-14.1, -14.1] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | LEGACY_RAW_EXIT | 200 | 11 | -16.57 | -27.16 | [-18.0, -14.9] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | BASELINE_RESIDUAL_EXIT | 100 | 26 | -18.93 | -20.58 | [-33.5, -6.0] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | BASELINE_RESIDUAL_EXIT | 250 | 0 |  |  | [nan, nan] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | FRACTIONAL_RESIDUAL_EXIT | 250 | 0 |  |  | [nan, nan] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | LEGACY_RAW_EXIT | 300 | 0 |  |  | [nan, nan] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | BASELINE_RESIDUAL_EXIT | 300 | 0 |  |  | [nan, nan] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | FRACTIONAL_RESIDUAL_EXIT | 300 | 0 |  |  | [nan, nan] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | LEGACY_RAW_EXIT | 400 | 0 |  |  | [nan, nan] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | BASELINE_RESIDUAL_EXIT | 400 | 0 |  |  | [nan, nan] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |
| RAW_SPREAD | 24 | FRACTIONAL_RESIDUAL_EXIT | 400 | 0 |  |  | [nan, nan] | PAPER_OBSERVATION_ONLY_INSUFFICIENT_SAMPLE |

## Required interpretation

1. Inspect both mean and median; disagreement is evidence of a few large events masking the typical outcome.
2. Intervals crossing zero and fewer than 30 realized events prevent a positive claim.
3. Compare `ALL_GATE_PAIRS`, `ALL_NON_GATE_PAIRS`, individual pairs and UTC-day blocks before attributing improvement broadly.
4. High censoring, large adverse excursion or very long holding time disqualifies otherwise attractive averages.
5. Thresholds failing the stated candidate criteria are either observation-only or rejected; none is approved for live trading.
