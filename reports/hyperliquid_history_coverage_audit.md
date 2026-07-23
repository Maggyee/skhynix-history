# Hyperliquid 历史覆盖审计

- requested_start: `2026-06-10 05:50:00+00:00`
- api_earliest_available_1m: `2026-07-19 16:05:00+00:00`
- raw_earliest_any_frequency: `2026-06-10 05:45:00+00:00`
- normalized_earliest_1m: `2026-07-19 16:05:00+00:00`
- metadata_listing_time: `NaT`（不得等同于 API 最早可得时间）
- 15m 审计数据范围: `2026-06-10 05:45:00+00:00 .. 2026-07-23 09:15:00+00:00`；仅作覆盖证据，不进入 1m 分析。

## 结论

resolved coin=`xyz:SKHX`；HIP-3 DEX 前缀正确。官方 candleSnapshot 只返回最近 5,000 根 K 线，旧的封闭 1m 窗口返回空；不是只保留最后一页。

原始 1m 最早时间与标准化 1m 最早时间一致，未发现 normalize 删除早期 raw 的证据。分页参数、HTTP 状态和原始文件逐页列在 `hyperliquid_history_pages.csv`。接口证据不支持把当前最早 K 线解释成产品上市日。

## 官方接口说明

- Gate: https://www.gate.com/docs/developers/apiv4/en/#market-candlesticks
- Hyperliquid: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/candle-snapshot
