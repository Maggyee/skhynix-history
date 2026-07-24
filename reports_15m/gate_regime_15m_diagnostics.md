# Gate 原生15分钟价差 regime 原因诊断

## 1. 直接结论

- **高置信度**：严格3-of-3外部基准窗口为 `2026-06-10 06:00:00+00:00` 至 `2026-07-24 04:15:00+00:00`（左闭右开）。首次连续阈值事件：50bps=2026-06-23 00:45, 100bps=2026-06-23 07:00, 150bps=2026-06-24 01:45, 200bps=2026-06-24 23:30 UTC；数据驱动上移候选在 6月22—24日，因此7月16日不是起点。
- **高置信度**：这是长期结构性Gate基差叠加波动regime。Gate/外部中位数P95在 PRE 为 204.3 bps、7/16—20 为 176.5 bps、POST 为 16.9 bps；7/16工作日延续高位，但完整预设窗口不是相对PRE的额外放大，7/17后的下移才是最强变点。
- **中等置信度**：7/16—20典型偏差最大层为 `gate_mark_minus_gate_index`，尾部P95最大层为 `gate_index_minus_external_market`；PRE主要是index-market层。对称bps分量不是严格可加，残差已单列。
- **高置信度**：低15分钟成交量和陈旧trade close不足以解释；高溢价桶落入最低成交量十分位的最高分段占比仅 1.18%。由于缺少历史BBO和深度，订单簿流动性仍无法确认。相关性不作因果解释。
- **高置信度**：数据错误判定为 `DATA_ERROR_NOT_SUPPORTED`（个别不可验证项仍为 INCONCLUSIVE）。
- **无法确认**：15分钟成交收盘不是BBO；没有历史bid/ask和深度，不能确认实际可成交价差或容量。

## 2. 三类分段严格分离

### 2.1 人工日期段（仅描述）

`PRE_20260716`、`GATE_REGIME_20260716_20260720`、`POST_20260720` 仅用于人工日期对照，不是实时标签。

### 2.2 数据驱动历史解释段（retrospective）

这些边界可使用变点前后窗口与完整样本，只用于历史解释，不能用于策略，也不能称为实时可识别标签。

| retrospective_segment | start_time | end_time | analysis_scope | strategy_eligible |
| --- | --- | --- | --- | --- |
| BASELINE | 2026-06-10 06:00:00+00:00 | 2026-06-29 12:45:00+00:00 | RETROSPECTIVE_CHANGE_POINTS | False |
| BUILDUP | 2026-06-29 12:45:00+00:00 | 2026-07-11 07:30:00+00:00 | RETROSPECTIVE_CHANGE_POINTS | False |
| STRUCTURAL_PREMIUM | 2026-07-11 07:30:00+00:00 | 2026-07-12 07:30:00+00:00 | RETROSPECTIVE_CHANGE_POINTS | False |
| NORMALIZATION | 2026-07-12 07:30:00+00:00 | 2026-07-13 07:30:00+00:00 | RETROSPECTIVE_CHANGE_POINTS | False |
| POST_NORMALIZATION | 2026-07-13 07:30:00+00:00 | 2026-07-24 04:30:00+00:00 | RETROSPECTIVE_CHANGE_POINTS | False |

### 2.3 因果实时标签统计

仅使用当前及过去bar；研究参数单独输出，不宣称为完整样本最优的未来策略参数。

| causal_regime | bar_count |
| --- | --- |
| NORMAL | 4134 |
| TRANSIENT_DISLOCATION | 0 |
| STRUCTURAL_PREMIUM | 0 |
| STALE_OR_INVALID | 84 |

### Gate/四家分人工日期段 P95 绝对价差（bps）

| regime | gate/binance | gate/bitget | gate/hyperliquid | gate/okx |
| --- | --- | --- | --- | --- |
| GATE_REGIME_20260716_20260720 | 176.9 | 172.2 | 229.9 | 200.4 |
| POST_20260720 | 18.4 | 19.4 | 44.0 | 26.8 |
| PRE_20260716 | 205.1 | 183.8 | 256.3 | 222.6 |

### trade / mark / index 分解中位数（bps）

| regime | decomposition_residual | gate_index_minus_external_market | gate_mark_minus_gate_index | gate_trade_minus_gate_mark | total_gate_trade_vs_market |
| --- | --- | --- | --- | --- | --- |
| GATE_REGIME_20260716_20260720 | -0.00 | 3.87 | 23.40 | 0.86 | 36.02 |
| POST_20260720 | 0.00 | -2.98 | 5.33 | 0.77 | 4.97 |
| PRE_20260716 | -0.00 | 38.05 | 7.61 | 0.56 | 57.81 |

## 2. 已由数据确认

- 主比较只用原生15分钟trade close，完全相同 `open_time`；未填充缺口。外部trade中位数固定为 binance, bitget, okx 且要求严格3-of-3齐全；两家结果只在 sensitivity CSV 单列。
- 外部mark中位数只含 Binance、Bitget、OKX；Hyperliquid没有可比历史mark，未加入。
- KRX实际交易日历重开后1小时的溢价变化中位数（负数为收敛）：PRE_20260716=11.8bps, GATE_REGIME_20260716_20260720=-5.3bps, POST_20260720=-5.1bps；并非所有regime都在开盘后系统性收敛。周末、工作日、美国时段与UTC小时的完整分组见 sessions CSV。
- 自动变点候选共 53 个；最高置信候选如下：

| change_time | pre_median_bps | post_median_bps | median_shift_bps | pre_p95_abs_bps | post_p95_abs_bps | confidence_metric | method | analysis_scope | uses_pre_and_post_windows | uses_full_sample | strategy_eligible |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-07-13 07:30:00+00:00 | 204.80 | 149.35 | -55.46 | 248.41 | 261.52 | 97.15 | robust_cusum | RETROSPECTIVE_CHANGE_POINTS | True | True | False |
| 2026-07-12 07:30:00+00:00 | 202.52 | 204.80 | 2.28 | 217.14 | 248.41 | 83.69 | robust_cusum | RETROSPECTIVE_CHANGE_POINTS | True | True | False |
| 2026-06-29 12:45:00+00:00 | 160.69 | 138.48 | -22.21 | 268.09 | 170.74 | 80.91 | robust_cusum | RETROSPECTIVE_CHANGE_POINTS | True | True | False |
| 2026-07-11 07:30:00+00:00 | 199.20 | 202.52 | 3.32 | 212.47 | 217.14 | 80.51 | robust_cusum | RETROSPECTIVE_CHANGE_POINTS | True | True | False |
| 2026-07-12 19:30:00+00:00 | 203.50 | 215.94 | 12.45 | 217.14 | 262.07 | 80.27 | robust_cusum | RETROSPECTIVE_CHANGE_POINTS | True | True | False |
| 2026-07-11 19:30:00+00:00 | 199.93 | 203.50 | 3.56 | 211.26 | 217.14 | 76.51 | robust_cusum | RETROSPECTIVE_CHANGE_POINTS | True | True | False |

- 连续阈值事件的end_time为右开边界，持续时间严格为15分钟倍数。

## 3. 较强支持的解释

- 以分解中绝对中位数/P95最大的层作为主要价格口径来源；这识别的是偏差所在层，不等于证明其业务原因。
- 若四个Gate组合同时同向而非Gate基准明显更小，更支持Gate自身定价层级，而不是只有Gate/OKX的双边定义差异。

## 4. 只能作为候选的解释

- Gate用户流/做市库存、mark公式、index休市处理、合约映射及外部事件都需要官方历史参数或盘口数据才能确认。
- 资金滞后相关使用 `descriptive_forward_hold`，只作描述；真实现金流仅保留真实结算事件。已使用 9 条已核验外部来源；它们仅作为候选机制。

- 最大绝对滞后相关仅 0.246，不同交易所领先/滞后方向不一致；无法支持稳定的资金费率领先或滞后关系。

- 真实共同资金结算前后4小时的绝对溢价变化中位数（负数为结算后收敛）：GATE_REGIME_20260716_20260720=-4.8bps, POST_20260720=-0.1bps, PRE_20260716=0.2bps；该事件研究仍不识别因果。

## 5. 当前不支持的解释

- 没有把7月16日当作首次异常；完整15分钟PRE窗口直接反对该叙事。
- 校验未支持秒/毫秒、1000倍乘数、重复时间戳或OHLC破坏足以制造整体regime。

## 6. 仍缺少的关键数据

- Gate历史BBO、逐笔成交、订单簿深度、trade_count、指数成分及权重、mark公式逐时参数、做市库存。
- 其他交易所同口径历史BBO；$1,000/$5,000/$10,000名义订单冲击成本。

## 7. 对套利策略的影响

1. 15分钟trade close价差不等于可执行BBO。
2. 长期同方向基差不保证快速收敛；做空Gate有基差继续扩大的风险。
3. Gate持续更高可能伴随做空资金收入，但资金规则、mark/index差异会改变现金流和强平风险。
4. 历史最大价差不代表容量；必须用实时Gate best bid对另一所best ask并测量深度/slippage。

## 8. 下一步需要采集的数据

- 同步采集五家1秒或逐笔BBO、至少20档深度、主动成交方向和真实资金结算。
- 保存Gate mark/index原始快照、指数成分、合约规则版本与公告生效时间。

## 数据质量逐项结论

| check | evidence | conclusion |
| --- | --- | --- |
| quarter_hour_alignment | aligned=True; rows=12654 | DATA_ERROR_NOT_SUPPORTED |
| duplicate_timestamps | duplicate rows=0 | DATA_ERROR_NOT_SUPPORTED |
| missing_buckets | index:0; mark:0; trade:0 | DATA_ERROR_NOT_SUPPORTED |
| pagination_order | normalized rows are sorted; raw API page ordering is not preserved as a column (observed backward count after normalization=0) | INCONCLUSIVE |
| seconds_milliseconds | implausible years=0 | DATA_ERROR_NOT_SUPPORTED |
| price_multiplier_1000 | median Gate/external ratios=1.004743,1.003421,1.006032 | DATA_ERROR_NOT_SUPPORTED |
| trade_mark_index_symbol | {"index": "SKHYNIX_USDT", "mark": "SKHYNIX_USDT", "trade": "SKHYNIX_USDT"} | DATA_ERROR_NOT_SUPPORTED |
| ohlc_invariants | violations=0 | DATA_ERROR_NOT_SUPPORTED |
| zero_or_missing_volume | ratio=0.0000% | DATA_ERROR_NOT_SUPPORTED |
| trade_count_availability | trade_count column is absent from native 15m schema | INCONCLUSIVE |
| extremes_at_low_volume | lowest-volume-decile share among top-5% premium=1.4354% | INCONCLUSIVE |
| extremes_after_stale_trade | first-price-change share among top-5% premium=1.9139% | INCONCLUSIVE |
| discrete_ratio_steps | {"1.0":0.1621168582,"1.001":0.1542145594,"1.002":0.0486111111,"0.999":0.0476532567,"1.005":0.0464559387} | INCONCLUSIVE |

## 流动性摘要

| regime | count | corr_abs_premium_log1p_volume | high_premium_lowest_volume_decile_ratio | p95_abs_premium_low_volume_bps | p95_abs_premium_normal_volume_bps | p95_abs_premium_unchanged_bps | p95_abs_premium_updated_bps | median_volume | median_range_bps | corr_abs_premium_range_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| PRE_20260716 | 3398 | 0.5326 | 0.0118 | 54.1531 | 205.1109 | 206.5107 | 204.3119 | 33197.0000 | 61.3108 | 0.0924 |
| GATE_REGIME_20260716_20260720 | 380 | 0.5109 | 0.0000 | 33.4513 | 176.6772 | 69.4873 | 176.5510 | 205548.0000 | 56.0855 | 0.4988 |
| POST_20260720 | 398 | -0.1316 | 0.0000 | 15.3682 | 17.4010 | 12.8393 | 16.8981 | 559458.5000 | 76.7450 | 0.0042 |

## 资金与非Gate基准

| pair | regime | count | median_signed_bps | median_abs_bps | p95_abs_bps |
| --- | --- | --- | --- | --- | --- |
| binance/bitget | PRE_20260716 | 3398 | -22.11 | 22.11 | 61.59 |
| binance/hyperliquid | PRE_20260716 | 3432 | 38.97 | 39.19 | 90.30 |
| binance/okx | PRE_20260716 | 3432 | 10.51 | 11.53 | 51.96 |
| bitget/hyperliquid | PRE_20260716 | 3398 | 63.50 | 63.50 | 119.28 |
| bitget/okx | PRE_20260716 | 3398 | 35.92 | 35.92 | 88.57 |
| hyperliquid/okx | PRE_20260716 | 3432 | -27.19 | 27.29 | 61.49 |
| NON_GATE_CROSS_SECTION_MEDIAN | PRE_20260716 | 3432 | NA | 32.11 | 64.66 |
| GATE_EXCESS_VS_NON_GATE_MEDIAN | PRE_20260716 | 3398 | NA | 30.22 | 174.96 |
| binance/bitget | GATE_REGIME_20260716_20260720 | 380 | -11.93 | 11.93 | 20.74 |
| binance/hyperliquid | GATE_REGIME_20260716_20260720 | 384 | 31.19 | 31.19 | 66.87 |
| binance/okx | GATE_REGIME_20260716_20260720 | 384 | 16.09 | 16.09 | 28.41 |
| bitget/hyperliquid | GATE_REGIME_20260716_20260720 | 380 | 43.63 | 43.63 | 75.95 |
| bitget/okx | GATE_REGIME_20260716_20260720 | 380 | 27.30 | 27.30 | 41.03 |
| hyperliquid/okx | GATE_REGIME_20260716_20260720 | 384 | -17.31 | 17.31 | 42.91 |
| NON_GATE_CROSS_SECTION_MEDIAN | GATE_REGIME_20260716_20260720 | 384 | NA | 23.10 | 38.83 |
| GATE_EXCESS_VS_NON_GATE_MEDIAN | GATE_REGIME_20260716_20260720 | 380 | NA | 16.88 | 151.17 |
| binance/bitget | POST_20260720 | 398 | -12.99 | 12.99 | 23.56 |
| binance/hyperliquid | POST_20260720 | 402 | 22.07 | 22.07 | 36.83 |
| binance/okx | POST_20260720 | 402 | 11.19 | 11.30 | 19.72 |
| bitget/hyperliquid | POST_20260720 | 398 | 34.36 | 34.36 | 51.60 |
| bitget/okx | POST_20260720 | 398 | 22.68 | 22.68 | 37.08 |
| hyperliquid/okx | POST_20260720 | 402 | -11.36 | 11.36 | 28.24 |
| NON_GATE_CROSS_SECTION_MEDIAN | POST_20260720 | 402 | NA | 18.45 | 26.31 |
| GATE_EXCESS_VS_NON_GATE_MEDIAN | POST_20260720 | 398 | NA | -11.31 | 2.15 |

## 假设评分

| hypothesis | supporting_evidence | contradicting_evidence | missing_evidence | confidence | status |
| --- | --- | --- | --- | --- | --- |
| H1 数据采集或标准化错误 | 无 | 时间、OHLC、比例、symbol和重复检查未支持系统性错误 | 交易所逐笔原始回放 | 高 | NOT_SUPPORTED |
| H2 低成交量及陈旧trade close | 局部相关性 | 高溢价并非主要集中于最低成交量十分位 | 历史逐笔成交与盘口 | 中等 | NOT_SUPPORTED |
| H3 Gate内部订单簿或用户多头需求 | trade-mark分量和四组同向可与该机制一致 | 15分钟close不能识别订单流因果 | 历史BBO、深度、主动买卖流 | 低 | INCONCLUSIVE |
| H4 Gate mark机制导致偏离 | mark-index分量可定量观测 | 分量只描述价格层，不证明公式原因 | 官方逐时公式参数 | 中等 | PARTIALLY_SUPPORTED |
| H5 Gate index成分或休市处理不同 | index-market分量可定量观测且按交易时段分组 | 缺少历史指数成分值与权重 | Gate历史指数成分/权重 | 中等 | PARTIALLY_SUPPORTED |
| H6 产品映射或合约定义不同 | metadata中乘数与产品类型存在跨所差异 | 价格比例不存在1000倍错误 | 各所正式产品条款的同口径映射 | 低 | INCONCLUSIVE |
| H7 资金费率规则改变造成或放大偏离 | 资金差与溢价存在描述性滞后相关 | 相关性不能证明因果 | 规则变更的官方时间戳与库存数据 | 低 | INCONCLUSIVE |
| H8 全市场共同price discovery分裂，并非Gate特有 | 非Gate P95提供同期基准 | Gate excess若显著为正则反对完全共同分裂 | 跨所历史BBO | 中等 | PARTIALLY_SUPPORTED |
| H9 外部事件导致短期价格锚分叉 | 交易日历/时段效应和外部时间线可比对 | 时间重合不构成因果 | 可验证公告及分钟级事件研究 | 低 | INCONCLUSIVE |

## 外部资料（与仓库数据结论分栏）

| event_time | event_type | title | source | source_url | verified | possible_mechanism | supports_hypothesis | contradicts_hypothesis |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2026-06-02T06:00:00Z | product_launch | Gate launched SKHYNIX USDT-M perpetual (1-20x) | Gate official announcement | https://www.gate.com/fr/announcements/article/51477 | True | Establishes product identity and start; does not explain later spread by itself | H6 | NA |
| 2026-06-15T00:00:00Z | methodology_document | Gate mark price is median of index-plus-funding-basis, spot-index-plus-moving-basis, and last fill | Gate Help Center | https://miniapp.gate.com/help/futures/futures-logic/22067/mark-price-calculation/mark-price-calculation | True | Formula allows mark-index separation through funding/moving basis and permits protective freezing | H4\|H5 | NA |
| 2026-06-24T09:00:00Z | trading_campaign | SKHYNIX perpetual trading reward campaign began; ran through 2026-07-03 09:00 UTC | Gate official announcement | https://www.gate.com/zh-tw/announcements/article/100318 | True | Could change Gate-specific participation and order flow; timing is close to first 150/200-bps events but is not causal proof | H3\|H9 | NA |
| 2026-07-02T07:00:00Z | trading_campaign | SKHYNIX3L ETF reward campaign began; ran through 2026-07-16 07:00 UTC | Gate official announcement | https://www.gate.com/zh/announcements/article/100455 | True | Could increase related-product attention but it is an ETF campaign rather than direct evidence about the perpetual order book | H3\|H9 | H6 |
| 2026-07-10T20:00:00Z | underlying_market | SK hynix ADR began Nasdaq trading as SKHY | Nasdaq official newsroom | https://www.nasdaq.com/newsroom/global-innovation-meets-global-capital-sk-hynix-lists-on-nasdaq | True | A new US-session price anchor could affect index/market discovery | H5\|H9 | NA |
| 2026-07-10T17:20:00Z | separate_product | Gate migrated SKHYUSDT pre-market perpetual to official trading | Gate official announcement | https://www.gate.com/id/announcements/article/100613 | True | Confirms a separate SKHY ADR-linked symbol; it must not be conflated with SKHYNIX_USDT | H6 | H6 |
| 2026-07-14T16:00:00Z | funding_rule_change | Gate changed SKHYNIXUSDT funding to 4-hour intervals and cap/floor to +/-0.05% | Gate official announcement | https://www.gate.com/zh-tw/announcements/article/100658 | True | Could change inventory carry and convergence incentives after the effective time; cannot explain June onset | H7 | H7 |
| 2026-07-24T02:40:00Z | index_constituents_snapshot | Current Gate SKHYNIX_USDT index listed Binance/Bitget/Bybit/Gate/OKX futures and Binance/OKX indexes | Gate API v4 | https://api.gateio.ws/api/v4/futures/usdt/index_constituents/SKHYNIX_USDT | True | A derivatives-based and self-including current index can differ from the three-venue external trade median; snapshot is not historical composition | H5 | NA |
| NA | market_calendar | KRX equities regular session is 09:00-15:30 Korea time; exchange closes on official holidays and Saturdays | Korea Exchange official guide | https://global.krx.co.kr/contents/GLB/01/0109/0109000000/guide_to_trading_in_the_korean_stock_market.pdf | True | Supports exchange-calendar session classification; does not itself explain a premium | H5\|H9 | NA |

## 复现

```bash
uv run python -m skhynix_research.gate_regime_15m
```

所有时间UTC；主价格为原生15分钟成交K线收盘，不是BBO。