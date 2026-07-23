# Gate 历史覆盖审计

- requested_start: `2026-06-10 05:50:00+00:00`
- api_earliest_available_1m: `2026-07-16 18:34:00+00:00`
- raw_earliest_any_frequency: `2026-06-10 05:45:00+00:00`
- normalized_earliest_1m: `2026-07-16 18:34:00+00:00`
- metadata_listing_time: `2026-06-02 03:39:54+00:00`（不得等同于 API 最早可得时间）
- 15m 审计数据范围: `2026-06-10 05:45:00+00:00 .. 2026-07-16 18:30:00+00:00`；仅作覆盖证据，不进入 1m 分析。

## 结论

官方 1m 接口对旧请求返回 `Candlestick too long ago. Maximum 10000 points recently are allowed`；from/to 为秒，返回升序，mark_/index_ 均有缓存响应。

原始 1m 最早时间与标准化 1m 最早时间一致，未发现 normalize 删除早期 raw 的证据。分页参数、HTTP 状态和原始文件逐页列在 `gate_history_pages.csv`。接口证据不支持把当前最早 K 线解释成产品上市日。

## 官方接口说明

- Gate: https://www.gate.com/docs/developers/apiv4/en/#market-candlesticks
- Hyperliquid: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/candle-snapshot
