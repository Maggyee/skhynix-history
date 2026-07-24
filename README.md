# SKHYNIX perpetual historical research

Public-API-only research pipeline for Hyperliquid, Binance, Bitget, OKX and Gate.
Data is cached under `data/raw`, normalized to Parquet/DuckDB, and reported in
Chinese under `reports`. Historical minute bars are proxies, not executable BBO.

```bash
uv run skhynix-research discover
uv run skhynix-research download --start 2026-06-10T05:50:00Z --end now
uv run skhynix-research analyze
uv run skhynix-research report
make quick
```

## 持续记录未来 1 分钟数据

五家平台的原生、已闭合 1 分钟 K 线由独立采集器写入
`data/live_1m/prices/` 分区 Parquet 数据集。`trade` 是五家统一的主价格；
Binance、Bitget、Gate 和 OKX 同时尽量记录官方 `mark`、`index`，Hyperliquid
仅记录其可用的 `trade` K 线。原始 API 响应保存在 `data/raw/live_1m/`。

```bash
# 单轮采集与回补最近 5 分钟
uv run skhynix-research collect-1m

# 持续运行（每个 UTC 分钟结束后 8 秒采集）
uv run skhynix-research collect-1m --forever

# 检查每个平台覆盖率、缺口、最新延迟和健康状态
uv run skhynix-research monitor-1m

# 生成只含五家连续共同覆盖率 100% 区间的新 1m 报告
uv run skhynix-research report-live-1m
```

`data/live_1m/collection_runs.csv` 是逐轮采集账本，`monitor.csv` 是各平台/价格
类型的滚动 24 小时覆盖快照，`status.json` 汇总五家 `trade` 数据是否按规律运行。
原始响应按“平台/UTC 日期”归档为 NDJSON，避免长期运行产生海量小文件。

实时统计报告输出到 `reports_live_1m/`。它只分析五家原生 `trade close 1m`
最长连续严格交集；不填充缺失分钟、不跨缺口、不混用 mark/index，也不包含资金费率。
