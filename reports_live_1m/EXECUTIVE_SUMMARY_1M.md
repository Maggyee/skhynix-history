# 实时五家严格共同覆盖 1 分钟报告

**分析口径：LIVE_ALL_FIVE_TRADE_CLOSE_1M_STRICT_COMMON_100PCT**

**统一窗口：[ 2026-07-23 13:17:00+00:00, 2026-07-24 01:22:00+00:00 )**

**最后一根 K 线 open time：2026-07-24 01:21:00+00:00**

本报告只使用 Binance、Bitget、Gate、Hyperliquid、OKX 五家同时存在的原生 `trade close 1m`，并选择最长连续、内部无缺分钟的共同区间。窗口共 **725 分钟**，五家共同覆盖率 **100%**，内部缺失 **0 分钟**。不使用 `mark/index`，不前向填充，也不跨缺口拼接。

## 关键结果

- 五家窗口收益介于 **-77.23 至 -59.39 bps**。
- 绝对价差 P95 最大的是 **bitget/hyperliquid：42.54 bps**；最小的是 **binance/bitget：8.29 bps**。
- 全窗口最大绝对价差为 **49.17 bps**，出现在 **bitget/hyperliquid / 2026-07-23 17:38:00+00:00**。
- 50/100/150/200 bps 阈值的超限分钟合计为 **0**。
- 部分 pair 长时间高于 20 bps，可能反映平台报价层级或合约微观结构差异；仅凭 K 线收盘价不能认定为可成交套利。

## 五家覆盖证明

| exchange | observed_minutes | expected_minutes | coverage_pct | missing_minutes | data_quality |
| --- | --- | --- | --- | --- | --- |
| binance | 725 | 725 | 100.0 | 0 | OK |
| bitget | 725 | 725 | 100.0 | 0 | OK |
| gate | 725 | 725 | 100.0 | 0 | OK |
| hyperliquid | 725 | 725 | 100.0 | 0 | OK |
| okx | 725 | 725 | 100.0 | 0 | OK |

## 各平台价格统计

| exchange | first_close | last_close | min_close | median_close | max_close | window_return_bps |
| --- | --- | --- | --- | --- | --- | --- |
| binance | 1267.55 | 1258.12 | 1238.18 | 1284.8 | 1306.83 | -74.395 |
| bitget | 1267.38 | 1259.17 | 1238.97 | 1285.45 | 1307.28 | -64.779 |
| gate | 1268.0 | 1258.3 | 1238.3 | 1283.4 | 1305.3 | -76.498 |
| hyperliquid | 1262.9 | 1255.4 | 1234.2 | 1281.3 | 1302.4 | -59.387 |
| okx | 1266.3 | 1256.52 | 1236.2 | 1282.77 | 1304.39 | -77.233 |

## Pair 价差统计

正价差表示 `exchange_a` 高于 `exchange_b`；绝对价差用于跨 pair 比较。

| pair | observations | median_spread_bps | mean_abs_spread_bps | p95_abs_spread_bps | p99_abs_spread_bps | max_abs_spread_bps | max_abs_spread_time | higher_exchange_at_max |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bitget/hyperliquid | 725 | 33.812 | 34.0 | 42.545 | 45.771 | 49.171 | 2026-07-23 17:38:00+00:00 | bitget |
| binance/hyperliquid | 725 | 30.124 | 29.885 | 38.989 | 41.054 | 44.373 | 2026-07-23 18:21:00+00:00 | binance |
| gate/hyperliquid | 725 | 19.477 | 20.126 | 32.003 | 40.693 | 48.954 | 2026-07-23 13:20:00+00:00 | gate |
| hyperliquid/okx | 725 | -14.096 | 14.779 | 26.642 | 30.648 | 34.783 | 2026-07-23 13:31:00+00:00 | okx |
| bitget/okx | 725 | 19.516 | 19.221 | 26.528 | 28.93 | 31.537 | 2026-07-23 18:52:00+00:00 | bitget |
| binance/okx | 725 | 15.379 | 15.105 | 21.775 | 24.388 | 26.019 | 2026-07-23 17:37:00+00:00 | binance |
| bitget/gate | 725 | 14.671 | 14.058 | 20.95 | 23.414 | 27.336 | 2026-07-23 20:00:00+00:00 | bitget |
| binance/gate | 725 | 10.854 | 10.091 | 18.173 | 20.207 | 24.389 | 2026-07-23 20:00:00+00:00 | binance |
| gate/okx | 725 | 5.146 | 5.861 | 12.385 | 14.749 | 16.973 | 2026-07-24 00:37:00+00:00 | gate |
| binance/bitget | 725 | -4.123 | 4.303 | 8.294 | 9.97 | 11.842 | 2026-07-23 14:37:00+00:00 | bitget |

## 20 bps 超限统计

| pair | bars_at_or_above_threshold | share_pct | event_count | max_consecutive_minutes |
| --- | --- | --- | --- | --- |
| bitget/hyperliquid | 725 | 100.0 | 1 | 725 |
| binance/hyperliquid | 700 | 96.552 | 19 | 475 |
| gate/hyperliquid | 351 | 48.414 | 48 | 57 |
| bitget/okx | 316 | 43.586 | 82 | 50 |
| hyperliquid/okx | 141 | 19.448 | 35 | 33 |
| binance/okx | 92 | 12.69 | 26 | 19 |
| bitget/gate | 55 | 7.586 | 27 | 14 |
| binance/gate | 8 | 1.103 | 5 | 4 |
| binance/bitget | 0 | 0.0 | 0 | 0 |
| gate/okx | 0 | 0.0 | 0 | 0 |

完整的 20/50/100/150/200 bps 统计见 `threshold_exceedance_1m.csv`。本报告是实时观察窗口，不替代 15 分钟全历史主分析，也不包含资金费率。
