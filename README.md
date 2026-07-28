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
同一常驻服务还会轮询五家真实已结算资金费率，回看最近 24 小时以处理延迟发布，
并幂等写入 `data/live_1m/funding/` 分区 Parquet 数据集。

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
类型的滚动 24 小时覆盖快照，`funding_monitor.csv` 记录五家资金费率轮询健康度，
`status.json` 汇总五家 `trade` 与资金费率数据是否按规律运行。
原始响应按“平台/UTC 日期”归档为 NDJSON，避免长期运行产生海量小文件。

实时统计报告输出到 `reports_live_1m/`。它只分析五家原生 `trade close 1m`
最长连续严格交集；不填充缺失分钟、不跨缺口、不混用 mark/index，也不包含资金费率。

## 公共实时 BBO 采集器

五家平台各自保持一条独立公共 WebSocket，自动心跳、指数退避重连，并把服务端原始
frame 以 gzip NDJSON 写入 `data/raw/live_bbo/`。标准化的可执行 BBO 以 5 分钟 Zstd
Parquet part 写入
`data/live_bbo/bbo/date=.../exchange=.../`，字段包括 bid/ask、两侧 size、交易所与
本机接收时间、序列号及序列号来源。

启动时会获取并归档五家产品 metadata；原生 size、合约单位/乘数、标准化标的数量和
USD notional 会同时保存。每条 v2 BBO 带 `schema_version` 和
`metadata_snapshot_id`，对应的时间版本化元数据保存在
`data/live_bbo/metadata_history/`。无法解析单位的平台标记为 `SIZE_UNIT_UNKNOWN`；
旧 schema 可以统一读取，但没有同期元数据证据时始终是 `CAPACITY_UNKNOWN`。

```bash
uv run skhynix-research collect-live-bbo
uv run skhynix-research collect-live-bbo --duration-seconds 600
uv run skhynix-research audit-bbo-capacity
```

本模块只包含公共市场数据。它不读取 API key、不连接账户或私有频道，不包含仓位、
PnL、策略门槛或真实下单代码。健康快照写入 `data/live_bbo/health/latest.csv`。
每日合并、raw 保留期、磁盘水位、近期全量和历史 250ms/1s 降采样均由
`live_bbo` 存储配置控制；登记在 `candidate_event_windows.parquet` 的窗口不会降采样。

## BBO paper trading

`paper-bbo` 是采集器之上的独立纸面执行层，只模拟公开 BBO 上的成交。它仅处理含
Gate 的四组 pair，并只接受 Gate 的 `NORMAL` / `TRANSIENT_DISLOCATION` 因果标签。
100/150/200 bps 门槛作用于 `net_entry_edge_bps`：原始 long ask / short bid 边际先扣除
四次保守 taker fee、四次滑点假设和安全缓冲，再连续满足 5 秒才开仓。

容量判断只使用 metadata 标准化后的 underlying quantity 和 USD notional；单位未知、
断连、陈旧、跨所时间差过大或容量不足都会 fail closed。总毛名义上限为 $1,000。

```bash
uv run skhynix-research paper-bbo
uv run skhynix-research paper-bbo --duration-seconds 600
```

`paper-bbo` 同时启动五家 BBO、`FundingSettlementService` 和统一健康监控。服务通过
公共接口回补最近 48 小时并持续轮询已结算资金费率，以确定性 ID 幂等调用
`PaperEngine.on_funding_event`。游标、事件日志和健康状态位于
`data/paper_bbo/funding_service/`，统一运行报告位于 `reports_runtime_health/`。

ledger 和日报位于 `data/paper_bbo/`。严格只计入
`opened_at < settled_at < closed_at` 的结算；尚未平仓时立即累计，延迟事件可补记到
已平仓交易且只记一次。没有导入真实已结算 funding event 的结果明确标为
`PRICE_ONLY_BEFORE_FUNDING`，不称为完整套利净收益。本模块没有认证、账户或真实下单路径。
