# Executive summary — P75 / historical-center native 1m study

## Verdict

**NO ROBUST CANDIDATE.** After the specified 20 bps cost, 0 tested main-scope configurations satisfy all robustness conditions.

This is a 1-minute trade-OHLC historical proxy study, not historical BBO, not an executable backtest, and it does not prove live profitability.

Run time: `2026-07-26T05:06:13.245385+00:00`. Dynamic Gate first valid minute: `2026-07-16T18:34:00+00:00`.

## Exchange coverage and latest-data lag

```text
exchange        first_valid_minute         last_valid_minute  valid_minutes  missing_minutes  latest_data_lag_minutes
 binance 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9532             4039                  21.2208
  bitget 2026-07-16 18:34:00+00:00 2026-07-26 04:43:00+00:00           9438             4132                  22.2208
    gate 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9532             4039                  21.2208
     okx 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9394             4177                  21.2208
```

## Pair-native and common-four windows

```text
          pair         data_scope                start_time        end_time_exclusive  valid_minutes  missing_minutes  longest_contiguous_minutes
binance/bitget PAIR_NATIVE_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9438             4132                         246
binance/bitget COMMON_FOUR_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9301             4269                         109
  binance/gate PAIR_NATIVE_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:45:00+00:00           9532             4039                        9527
  binance/gate COMMON_FOUR_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9301             4269                         109
   binance/okx PAIR_NATIVE_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:45:00+00:00           9394             4177                        9389
   binance/okx COMMON_FOUR_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9301             4269                         109
   bitget/gate PAIR_NATIVE_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9438             4132                         246
   bitget/gate COMMON_FOUR_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9301             4269                         109
    bitget/okx PAIR_NATIVE_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9301             4269                         109
    bitget/okx COMMON_FOUR_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9301             4269                         109
      gate/okx PAIR_NATIVE_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:45:00+00:00           9394             4177                        9389
      gate/okx COMMON_FOUR_WINDOW 2026-07-16 18:34:00+00:00 2026-07-26 04:44:00+00:00           9301             4269                         109
```

## Primary pair results

```text
          pair  signal_count  entered_count  realized_count  max_hold_count  unresolved_count  realized_rate  unresolved_rate  entry_decay_rate  mean_net_account_price_pnl_bps  median_net_account_price_pnl_bps  win_rate  median_holding_minutes  mean_net_funding_account_bps  mean_net_combined_account_pnl_bps
binance/bitget           130            114              99               0                15         0.8684           0.1316            0.1231                        -17.5409                          -17.6944       0.0                     4.0                       -0.0991                           -17.6400
  binance/gate             1              1               0               0                 1         0.0000           1.0000            0.0000                             NaN                               NaN       NaN                     NaN                           NaN                                NaN
   binance/okx            31             22              21               0                 1         0.9545           0.0455            0.2903                        -16.7753                          -16.9047       0.0                    30.0                       -0.8173                           -17.5926
   bitget/gate            37             30               0               0                30         0.0000           1.0000            0.1892                             NaN                               NaN       NaN                     NaN                           NaN                                NaN
    bitget/okx            69             66              50               0                16         0.7576           0.2424            0.0435                        -17.1418                          -17.1542       0.0                     5.0                        0.0000                           -17.1418
      gate/okx             0              0               0               0                 0            NaN              NaN               NaN                             NaN                               NaN       NaN                     NaN                           NaN                                NaN
```

## History windows

```text
 history_model         data_scope  signal_count  entered_count  realized_count  max_hold_count  unresolved_count  realized_rate  unresolved_rate  entry_decay_rate  mean_net_account_price_pnl_bps  median_net_account_price_pnl_bps  win_rate  median_holding_minutes  mean_net_funding_account_bps  mean_net_combined_account_pnl_bps
EXPANDING_PAST PAIR_NATIVE_WINDOW           268            233             170               0                63         0.7296           0.2704            0.1306                        -17.3289                          -17.5352       0.0                     5.0                       -0.1587                           -17.4876
EXPANDING_PAST COMMON_FOUR_WINDOW           328            276             167               0               109         0.6051           0.3949            0.1555                        -17.2872                          -17.5355       0.0                     5.0                       -0.1191                           -17.4062
   ROLLING_24H PAIR_NATIVE_WINDOW           115             76              74               0                 2         0.9737           0.0263            0.3391                        -17.3492                          -17.4888       0.0                     9.5                       -0.5035                           -17.8527
   ROLLING_24H COMMON_FOUR_WINDOW             0              0               0               0                 0            NaN              NaN               NaN                             NaN                               NaN       NaN                     NaN                           NaN                                NaN
   ROLLING_72H PAIR_NATIVE_WINDOW            48             32              30               0                 2         0.9375           0.0625            0.3333                        -16.5704                          -16.7175       0.0                    25.0                       -1.0869                           -17.6573
   ROLLING_72H COMMON_FOUR_WINDOW             0              0               0               0                 0            NaN              NaN               NaN                             NaN                               NaN       NaN                     NaN                           NaN                                NaN
    ROLLING_7D PAIR_NATIVE_WINDOW             0              0               0               0                 0            NaN              NaN               NaN                             NaN                               NaN       NaN                     NaN                           NaN                                NaN
    ROLLING_7D COMMON_FOUR_WINDOW             0              0               0               0                 0            NaN              NaN               NaN                             NaN                               NaN       NaN                     NaN                           NaN                                NaN
```

## Upper-only vs two-sided

```text
strategy_side_policy         data_scope  signal_count  entered_count  realized_count  max_hold_count  unresolved_count  realized_rate  unresolved_rate  entry_decay_rate  mean_net_account_price_pnl_bps  median_net_account_price_pnl_bps  win_rate  median_holding_minutes  mean_net_funding_account_bps  mean_net_combined_account_pnl_bps
      UPPER_P75_ONLY PAIR_NATIVE_WINDOW           268            233             170               0                63         0.7296           0.2704            0.1306                        -17.3289                          -17.5352       0.0                     5.0                       -0.1587                           -17.4876
      UPPER_P75_ONLY COMMON_FOUR_WINDOW           328            276             167               0               109         0.6051           0.3949            0.1555                        -17.2872                          -17.5355       0.0                     5.0                       -0.1191                           -17.4062
   TWO_SIDED_P75_P25 PAIR_NATIVE_WINDOW           586            515             381               0               134         0.7398           0.2602            0.1212                        -17.4610                          -17.6964       0.0                     6.0                       -0.1246                           -17.5856
   TWO_SIDED_P75_P25 COMMON_FOUR_WINDOW           738            628             378               0               250         0.6019           0.3981            0.1450                        -17.4371                          -17.6954       0.0                     6.0                       -0.0767                           -17.5138
```

## Frozen/dynamic mean and median exits

```text
   exit_center_policy         data_scope  signal_count  entered_count  realized_count  max_hold_count  unresolved_count  realized_rate  unresolved_rate  entry_decay_rate  mean_net_account_price_pnl_bps  median_net_account_price_pnl_bps  win_rate  median_holding_minutes  mean_net_funding_account_bps  mean_net_combined_account_pnl_bps
    FROZEN_ENTRY_MEAN PAIR_NATIVE_WINDOW           268            233             170               0                63         0.7296           0.2704            0.1306                        -17.3289                          -17.5352       0.0                     5.0                       -0.1587                           -17.4876
    FROZEN_ENTRY_MEAN COMMON_FOUR_WINDOW           328            276             167               0               109         0.6051           0.3949            0.1555                        -17.2872                          -17.5355       0.0                     5.0                       -0.1191                           -17.4062
  DYNAMIC_CAUSAL_MEAN PAIR_NATIVE_WINDOW           269            234             171               0                63         0.7308           0.2692            0.1301                        -17.3364                          -17.5407       0.0                     5.0                       -0.1577                           -17.4941
  DYNAMIC_CAUSAL_MEAN COMMON_FOUR_WINDOW           329            277             168               0               109         0.6065           0.3935            0.1550                        -17.2950                          -17.5431       0.0                     4.5                       -0.1184                           -17.4134
  FROZEN_ENTRY_MEDIAN PAIR_NATIVE_WINDOW           252            217             154               0                63         0.7097           0.2903            0.1389                        -17.0405                          -17.2583       0.0                     6.0                       -0.2446                           -17.2851
  FROZEN_ENTRY_MEDIAN COMMON_FOUR_WINDOW           320            265             156               0               109         0.5887           0.4113            0.1688                        -17.0552                          -17.2698       0.0                     6.0                       -0.1799                           -17.2351
DYNAMIC_CAUSAL_MEDIAN PAIR_NATIVE_WINDOW           270            228             165               0                63         0.7237           0.2763            0.1556                        -17.0533                          -17.2804       0.0                     7.0                       -0.3162                           -17.3695
DYNAMIC_CAUSAL_MEDIAN COMMON_FOUR_WINDOW           322            267             158               0               109         0.5918           0.4082            0.1677                        -17.0572                          -17.2698       0.0                     6.0                       -0.1833                           -17.2405
```

## Entry execution sensitivity

```text
entry_execution_policy         data_scope  signal_count  entered_count  realized_count  max_hold_count  unresolved_count  realized_rate  unresolved_rate  entry_decay_rate  mean_net_account_price_pnl_bps  median_net_account_price_pnl_bps  win_rate  median_holding_minutes  mean_net_funding_account_bps  mean_net_combined_account_pnl_bps
  RECHECK_AT_NEXT_OPEN PAIR_NATIVE_WINDOW           268            233             170               0                63         0.7296           0.2704            0.1306                        -17.3289                          -17.5352       0.0                     5.0                       -0.1587                           -17.4876
  RECHECK_AT_NEXT_OPEN COMMON_FOUR_WINDOW           328            276             167               0               109         0.6051           0.3949            0.1555                        -17.2872                          -17.5355       0.0                     5.0                       -0.1191                           -17.4062
   LOCKED_SIGNAL_ENTRY PAIR_NATIVE_WINDOW           249            249             186               0                63         0.7470           0.2530            0.0000                        -17.4841                          -17.6524       0.0                     5.0                       -0.1450                           -17.6291
   LOCKED_SIGNAL_ENTRY COMMON_FOUR_WINDOW           296            295             183               0               112         0.6203           0.3797            0.0000                        -17.4463                          -17.6920       0.0                     4.0                       -0.1087                           -17.5550
```

## Maximum holding sensitivity

```text
 max_holding_minutes         data_scope  signal_count  entered_count  realized_count  max_hold_count  unresolved_count  realized_rate  unresolved_rate  entry_decay_rate  mean_net_account_price_pnl_bps  median_net_account_price_pnl_bps  win_rate  median_holding_minutes  mean_net_funding_account_bps  mean_net_combined_account_pnl_bps
                 NaN PAIR_NATIVE_WINDOW           268            233             170               0                63         0.7296           0.2704            0.1306                        -17.3289                          -17.5352       0.0                     5.0                       -0.1587                           -17.4876
                 NaN COMMON_FOUR_WINDOW           328            276             167               0               109         0.6051           0.3949            0.1555                        -17.2872                          -17.5355       0.0                     5.0                       -0.1191                           -17.4062
              1440.0 PAIR_NATIVE_WINDOW           273            237             170               4                63         0.7173           0.2658            0.1319                        -17.5047                          -17.5431       0.0                     5.0                       -0.1999                           -17.7046
              1440.0 COMMON_FOUR_WINDOW           328            276             167               0               109         0.6051           0.3949            0.1555                        -17.2872                          -17.5355       0.0                     5.0                       -0.1191                           -17.4062
              4320.0 PAIR_NATIVE_WINDOW           269            234             170               1                63         0.7265           0.2692            0.1301                        -17.4115                          -17.5355       0.0                     5.0                       -0.2522                           -17.6637
              4320.0 COMMON_FOUR_WINDOW           328            276             167               0               109         0.6051           0.3949            0.1555                        -17.2872                          -17.5355       0.0                     5.0                       -0.1191                           -17.4062
             10080.0 PAIR_NATIVE_WINDOW           268            233             170               0                63         0.7296           0.2704            0.1306                        -17.3289                          -17.5352       0.0                     5.0                       -0.1587                           -17.4876
             10080.0 COMMON_FOUR_WINDOW           328            276             167               0               109         0.6051           0.3949            0.1555                        -17.2872                          -17.5355       0.0                     5.0                       -0.1191                           -17.4062
```

## Duration-tail stability

The longest 10% of closed primary events (at least 38.4 minutes) average -17.00 account bps versus -17.37 bps for the rest. This diagnoses whether a small long-duration tail drives the aggregate.

## Gate and funding

```text
  gate_group  signal_count  entered_count  realized_count  max_hold_count  unresolved_count  realized_rate  unresolved_rate  entry_decay_rate  mean_net_account_price_pnl_bps  median_net_account_price_pnl_bps  win_rate  median_holding_minutes  mean_net_funding_account_bps  mean_net_combined_account_pnl_bps
GATE_RELATED            38             31               0               0                31         0.0000           1.0000            0.1842                             NaN                               NaN       NaN                     NaN                           NaN                                NaN
    NON_GATE           230            202             170               0                32         0.8416           0.1584            0.1217                        -17.3289                          -17.5352       0.0                     5.0                       -0.1587                           -17.4876
```

Mean primary funding attribution is `-0.1587` account bps per closed event; it is separate from the price conclusion.

## ONE_POSITION_GLOBAL

```text
 selected_events  rejected_while_occupied  compounded_final_equity_usd  non_compounded_final_equity_usd  max_drawdown  capital_utilization
               4                        3                   994.953862                       994.945357     -0.005046              0.00339
```

Pair selection counts:

```text
          pair  selected_count
binance/bitget               1
  binance/gate               1
   binance/okx               2
```

The selector ranks same-minute candidates only by causal tail score, never future holding time or PnL.

## UTC hour and Korea cash-session proxy

```text
    stock_session  signal_count  entered_count  realized_count  max_hold_count  unresolved_count  realized_rate  unresolved_rate  entry_decay_rate  mean_net_account_price_pnl_bps  median_net_account_price_pnl_bps  win_rate  median_holding_minutes  mean_net_funding_account_bps  mean_net_combined_account_pnl_bps
KOREA_CASH_CLOSED           175            154             117               0                37         0.7597           0.2403            0.1200                        -17.4251                          -17.5455       0.0                     4.0                       -0.2275                           -17.6526
  KOREA_CASH_OPEN            93             79              53               0                26         0.6709           0.3291            0.1505                        -17.1166                          -17.3510       0.0                     6.0                       -0.0066                           -17.1232
```

## Cost and interpretation

Each leg receives half of gross notional. Two leg returns are summed, multiplied by 0.5, and the 20 bps full round-trip cost is deducted once. Unclosed natural events receive no invented terminal PnL.
