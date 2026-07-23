# RECENT_1M_MICROSTRUCTURE_ANALYSIS — SKHYNIX 永续近期微观结构研究

**截止 UTC：2026-07-23 09:20:53+00:00**。本报告仅用于近期尖峰、分钟事件持续时间与局部 regime；Gate 1m 从 2026-07-16 18:34 开始，Hyperliquid 1m 从 2026-07-19 16:05 开始，五家共同窗口很短。不得用于 2026-06-10 起的五家全历史价格或资金排名；全历史主结论见 `reports_15m/EXECUTIVE_SUMMARY_15M.md`。旧非同窗榜仅作 `NOT_COMPARABLE` 审计。

## 强结论

1. 五家全局共同窗口为 `[2026-07-19 16:05:00+00:00, 2026-07-23 07:04:00+00:00)`，有效共同分钟 5,219，覆盖 100.00%。起点限制：hyperliquid；终点限制：okx。
   该五家统一样本的绝对价差 P95 最高组合为 gate/hyperliquid（66.46 bps），不混用 pair 各自更长窗口。
2. 原 `long Bitget / short OKX = $580.45` 是非同窗结果；严格联合同窗后为 **$578.88**，即单边 $10,000 的 **5.7888%**。
3. 修正后资金前三名：long bitget / short okx $578.88; long binance / short okx $550.81; long bitget / short gate $90.48。旧前三为 long bitget / short okx, long binance / short okx, long hyperliquid / short okx，新前三为 long bitget / short okx, long binance / short okx, long bitget / short gate，**第三名发生变化**。
4. Gate regime 分段结果：
- binance/gate: 全共同窗口=130.76/179.23/189.72；regime=171.17/181.87/189.72；排除后=18.79/26.92/56.53；7月20日后=18.79/26.92/56.53 bps
- bitget/gate: 全共同窗口=106.02/176.19/189.13；regime=170.28/179.08/189.13；排除后=20.81/25.27/47.73；7月20日后=20.81/25.27/47.73 bps
- gate/hyperliquid: 全共同窗口=65.98/80.58/100.24；regime=85.46/89.65/100.24；排除后=43.22/49.32/78.56；7月20日后=43.22/49.32/78.56 bps
- gate/okx: 全共同窗口=157.53/201.15/207.65；regime=196.79/203.24/207.65；排除后=28.07/36.98/57.60；7月20日后=28.07/36.98/57.60 bps
5. 若排除 7 月16–19 日后 P95 明显下降，原高 P95 由特定 regime 驱动，不能解释为长期稳定套利空间。
6. Gate 价格较高的 regime 内，做空 Gate 的资金方向确为正：long bitget / short gate $110.38; long binance / short gate $103.40; long okx / short gate $74.67; long hyperliquid / short gate $2.38。但约 75–110 美元资金现金流小于相关组合约 189–208 bps 的峰值偏离，不能覆盖继续扩张、滑点和不可成交风险。7 月20日后这些做空 Gate 方向多数反转为负（long binance / short gate $-19.71; long bitget / short gate $-19.90; long hyperliquid / short gate $2.83; long okx / short gate $-16.05）。

## Pair 价格共同窗口

- binance/bitget: price `[2026-06-10 05:50:00+00:00, 2026-07-23 09:19:00+00:00)`，有效 62,129 分钟，覆盖 100.00%
- binance/gate: price `[2026-07-16 18:34:00+00:00, 2026-07-23 09:20:00+00:00)`，有效 9,526 分钟，覆盖 100.00%
- binance/hyperliquid: price `[2026-07-19 16:05:00+00:00, 2026-07-23 09:20:00+00:00)`，有效 5,355 分钟，覆盖 100.00%
- binance/okx: price `[2026-06-10 05:50:00+00:00, 2026-07-23 07:04:00+00:00)`，有效 61,994 分钟，覆盖 100.00%
- bitget/gate: price `[2026-07-16 18:34:00+00:00, 2026-07-23 09:19:00+00:00)`，有效 9,525 分钟，覆盖 100.00%
- bitget/hyperliquid: price `[2026-07-19 16:05:00+00:00, 2026-07-23 09:19:00+00:00)`，有效 5,354 分钟，覆盖 100.00%
- bitget/okx: price `[2026-06-10 05:50:00+00:00, 2026-07-23 07:04:00+00:00)`，有效 61,994 分钟，覆盖 100.00%
- gate/hyperliquid: price `[2026-07-19 16:05:00+00:00, 2026-07-23 09:20:00+00:00)`，有效 5,355 分钟，覆盖 100.00%
- gate/okx: price `[2026-07-16 18:34:00+00:00, 2026-07-23 07:04:00+00:00)`，有效 9,390 分钟，覆盖 100.00%
- hyperliquid/okx: price `[2026-07-19 16:05:00+00:00, 2026-07-23 07:04:00+00:00)`，有效 5,219 分钟，覆盖 100.00%

## Pair 价格＋资金联合窗口（无方向组合）

- binance/bitget: joint `[2026-06-10 08:00:00.001000+00:00, 2026-07-23 04:00:00+00:00)`
- binance/gate: joint `[2026-07-16 18:34:00+00:00, 2026-07-23 08:00:00.001000+00:00)`
- binance/hyperliquid: joint `[2026-07-19 16:05:00+00:00, 2026-07-23 08:00:00.001000+00:00)`
- binance/okx: joint `[2026-06-10 08:00:00.001000+00:00, 2026-07-23 00:00:00+00:00)`
- bitget/gate: joint `[2026-07-16 18:34:00+00:00, 2026-07-23 04:00:00+00:00)`
- bitget/hyperliquid: joint `[2026-07-19 16:05:00+00:00, 2026-07-23 04:00:00+00:00)`
- bitget/okx: joint `[2026-06-10 08:00:00+00:00, 2026-07-23 00:00:00+00:00)`
- gate/hyperliquid: joint `[2026-07-19 16:05:00+00:00, 2026-07-23 08:00:03+00:00)`
- gate/okx: joint `[2026-07-16 18:34:00+00:00, 2026-07-23 00:00:00+00:00)`
- hyperliquid/okx: joint `[2026-07-19 16:05:00+00:00, 2026-07-23 00:00:00+00:00)`

## 初步结论

- 同窗修正会改变资金累计排序与金额；最大修正包括：okx/hyperliquid -535.93→-15.19 USD; hyperliquid/okx 535.93→15.19 USD; gate/okx 363.82→-58.62 USD。这些旧结果主要因短历史交易所的非同窗累计被高估；逐项变化见 `old_vs_corrected_results.csv`。
- Gate mark–trade 绝对差 P99=15.95 bps，而 mark–index P99=138.15 bps；分钟连续、无重复、零量极少。证据更偏向 Gate 指数/标记口径及自身价格发现 regime，而非时间戳或分页错误。
- Gate 7 月20日后价差显著收窄但仍有 19–43 bps 的 P95 残余；不能称为完全恢复一致。分段资金见 `gate_regime_funding_summary.csv`。

## 无法确认

- 历史分钟 K 线不是可执行 BBO，无法确认真实滑点、深度、成交容量或暂停期间盘口。
- 仅凭公开 K 线无法最终区分真实市场偏离、指数成分/休市机制与交易所内部定价口径；不做静默缩放。


## 历史覆盖、资金贡献与可交易性补充（本轮）

### 强结论

1. Gate 缺失不是 normalize 或分页方向错误。官方 1m 请求返回“仅最近 10,000 点”，raw 1m 与 normalized 1m 同为 2026-07-16 18:34 起；15m 审计可回到 2026-06-10 05:45，但未混入 1m 主样本。
2. Hyperliquid resolved coin 为 `xyz:SKHX`，DEX 前缀正确；旧封闭 1m 窗口为空，`candleSnapshot` 为最近约 5,000 根限制。15m 可回到 2026-06-10 05:45，但不能伪装成 1m。五家共同窗口未延长；四家最长为 `binance|bitget|gate|okx` `[2026-07-16 18:34:00+00:00,2026-07-23 07:04:00+00:00)`，三家最长为 `binance|bitget|okx` `[2026-06-10 05:50:00+00:00,2026-07-23 07:04:00+00:00)`。
3. 严格同窗资金前三：long bitget / short okx $578.88; long binance / short okx $550.81; long bitget / short gate $90.48。
4. Long Bitget / Short OKX 的 $578.88 来自 157 个单边结算事件时间点；最大单次 $51.28，最大5次有符号合计 $135.67（占净额 23.44%；绝对贡献/净额 40.35%）。排除绝对值最大1%后为 **$477.59**，排除最大5%后为 **$407.41**；收益并非只由五次事件构成，但对尾部事件敏感。
   结算间隔分段：binance=4h,8h; bitget=4h,8h; gate=4h,8h; hyperliquid=1h; okx=4h,8h。Binance/Bitget/Gate 在7月14日前后由8h转4h；OKX 短暂8h→4h后恢复8h；Hyperliquid保持1h。`.001` 秒边界按真实时间保留，左闭右开去重。CSV 中 min/max 是样本观察值，不冒充官方上下限。
5. 8小时固定持有的历史代理路径在成本情景下：20 bps：funding_optimal_trailing_24h: 1991/11550 (17.24%)；price_convergence: 2480/11550 (21.47%)；40 bps：funding_optimal_trailing_24h: 659/11550 (5.71%)；price_convergence: 771/11550 (6.68%)；80 bps：funding_optimal_trailing_24h: 165/11550 (1.43%)；price_convergence: 165/11550 (1.43%)。这是分钟代理规则集合，不是可执行利润。
6. Gate regime 的8小时固定持有：funding_optimal_trailing_24h: n=516, gross中位=20.44 bps, funding中位=10.00 bps, 成本20/40/80后正比例=51.2%/31.2%/6.4%, 最大MAE=88.24 bps；price_convergence: n=516, gross中位=23.03 bps, funding中位=10.00 bps, 成本20/40/80后正比例=56.2%/36.2%/7.6%, 最大MAE=57.07 bps。
7. 最大 MAE 为 **563.40 bps**：binance/okx，funding_optimal_trailing_24h，入场 2026-06-11 12:34:00+00:00，阈值 150 bps，退出规则 hold_24h；最大单腿浮亏 12.60%。

### 初步结论

- 价格收敛方向与仅用入场前24小时资金事件选出的方向，按8小时样本入场的一致率：一致/多数一致 gate/hyperliquid(56.4%), gate/okx(67.3%), bitget/gate(88.5%), binance/gate(88.5%), hyperliquid/okx(93.5%)；冲突/多数冲突 bitget/okx(23.7%), binance/okx(32.8%), binance/hyperliquid(37.3%), bitget/hyperliquid(39.0%), binance/bitget(47.7%)。Binance/OKX 与 Bitget/OKX 的冲突最明显。
- Gate 相对主三所 mark 中位数：regime 内绝对 P95/P99/最大 174.31/184.38/191.53 bps；7月20日后 17.05/26.79/56.69 bps，中位有向溢价 5.93 bps。异常大幅消退，但仍有少量20–40 bps以上残余尾部。
- 跨 pair 的 session P95 中位数最高是 `US_PREMARKET`；20 bps事件在1小时内严格回到20 bps以下的完整观测比例最高的时段为 US_AFTER_HOURS=98.81%, KRX_HOLIDAY_OR_WEEKEND=98.14%, KRX_REGULAR_EARLIER=97.97%, US_REGULAR=97.08%；这不是Kaplan–Meier无偏概率。Long Bitget/Short OKX 资金贡献最高时段为 `KRX_REGULAR_EARLIER`（$331.93/$10,000）。

### 无法确认

- 价格路径使用 mark/trade 分钟代理，不是历史 BBO；目标退出、止损、成本和杠杆压力均不含盘口深度、真实滑点、强平公式或成交容量。
- Gate regime 前18小时34分钟没有官方可回补的1m数据；无法确认该段峰值和真实起点。现有数据也不足以最终区分 Gate 的可成交市场偏离与指数/休市定价口径。


## 唯一基础价差事件与持续时间修正版

### 强结论

- 20 bps唯一 `base_event` 共 **8492** 个：完成 8488，右删失 4，数据缺口删失 0。修正前20 bps市场事件也是 4810 个（数量差 +3682），但旧表没有删失状态和稳定ID；策略方向、退出规则和成本不再进入持续时间计数。
- 已观测持续时间中位数/P75/P90/P95/P99：**2.0/4.0/13.0/32.0/234.5 分钟**。
- 仅已完成事件在5/15/30/60/240分钟内结束的比例：79.24%/91.22%/94.91%/97.34%/99.02%；全部已观测事件中超过1天 13 个。
- 最长事件：bitget/okx，`[2026-07-10 09:11:00+00:00, 2026-07-13 00:06:00+00:00]`，已观测 3776 分钟，状态 COMPLETED，开始regime=PRE_GATE_REGIME；删失事件不称为完整真实持续时间。
- Gate regime相关事件中位数/P95：2.0/69.3 分钟；7月20日后：2.0/23.5 分钟。

### 初步结论

- 排除Gate regime后，P95=31.0、P99=223.5、最长已观测=3776分钟，说明长尾是否仍存在应以这些数字判断，而不是由旧全范围直方图目测。
- 数日级、完成后仍长期高于20 bps的事件更接近结构性基差候选；详见 `longest_spread_events_20bps.csv`。是否可交易仍无法仅凭分钟mark/trade确认。
- 收敛率现在使用真实目标命中；`observed_rate_complete_cases` 仅基于完整可观察事件，不是Kaplan–Meier或无偏总体概率。

### 无法确认

所有持续时间均来自历史mark/trade分钟代理，不是BBO；数据缺口删失、右删失、盘口深度和真实成交状态仍限制套利解释。
