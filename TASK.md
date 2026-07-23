你现在位于一台可以访问互联网的 Linux 云服务器，并拥有当前 Git 项目目录的读写权限。

不要只给我方案。请直接创建代码、安装项目依赖、调用交易所公开 API、下载历史数据、执行分析，并生成最终报告。不要向我提澄清问题；遇到部分交易所失败时，记录错误并继续完成其他交易所的数据与报告。

# 一、核心目标

快速研究 SK 海力士相关 `SKHYNIX` 永续合约在以下交易所之间的历史价格差和资金费率差：

* Hyperliquid
* Binance
* Bitget
* OKX
* Gate

历史区间：

```text
开始：2026-06-10T05:50:00Z
结束：程序运行时的当前 UTC 时间
```

研究重点是韩国股票市场收盘后的时段。

本任务只进行公开市场数据采集与研究：

* 不使用交易 API；
* 不下单；
* 不需要交易所 API Key；
* 不创建实时交易机器人；
* 不先部署 PostgreSQL、Grafana、Docker 或复杂服务；
* 优先尽快产出可信的历史对比报告。

# 二、必须遵守的执行原则

1. 先检查服务器环境、Python 版本、磁盘空间和网络。
2. 使用 Python 3.11 或更高版本。
3. 优先使用 `uv` 管理依赖；没有 `uv` 时可以安装，或者使用 Python `venv`。
4. 使用 DuckDB 和 Parquet 存储，避免为了这次快速分析搭建数据库服务。
5. 所有时间统一为带时区的 UTC 时间。
6. 所有请求必须有超时、指数退避、限速和断点续传。
7. 原始响应必须缓存，重复运行时不要重新下载已有数据。
8. 代码和运行过程必须幂等：重复执行不会制造重复数据。
9. 对 HTTP 429、5xx、网络超时最多重试，并遵守响应头中的限速信息。
10. 不使用网页爬虫抓交易页面；优先使用交易所官方公开 REST API。
11. 如果官方接口、路径或参数已经变化，以运行时查到的最新官方文档和 API 实际响应为准，不要死守本任务中的接口提示。
12. 不允许为了“看起来完整”而伪造、插值或猜测缺失数据。
13. 一个交易所失败不能阻止其他交易所出报告。
14. 最终必须实际运行程序，确认输出文件存在，不要只提交源代码。

# 三、产品发现与标的验证

使用以下代码作为候选映射，但启动后必须通过各交易所产品信息接口验证：

```yaml
binance:
  symbol: SKHYNIXUSDT

bitget:
  symbol: SKHYNIXUSDT
  category_or_product_type: USDT-FUTURES

okx:
  inst_id: SKHYNIX-USDT-SWAP

gate:
  settle: usdt
  contract: SKHYNIX_USDT

hyperliquid:
  expected_names:
    - "xyz:SKHX"
    - "SKHX"
    - "SKHYNIX"
  discovery_required: true
```

对每家交易所保存：

```text
exchange
requested_symbol
resolved_symbol
status
listing_time
quote_currency
collateral_currency
contract_type
contract_multiplier
price_tick
quantity_step
funding_interval_current
metadata_retrieved_at
raw_metadata
```

Hyperliquid 不得盲目硬编码产品名称。必须通过官方 `info` 接口查询主永续和 builder-deployed perp/HIP-3 市场 metadata，搜索包含 `SKHX` 或 `SKHYNIX` 的合约，并记录真实 DEX、coin 名称和资产标识。

注意区分：

* 报价货币；
* 保证金币种；
* 合约价格所代表的底层单位。

不要因为 Hyperliquid 使用 USDC 作为保证金，就直接把合约价格视为 USDC 报价。应以官方合约 metadata 和规范中定义的价格计价方式为准。

如果某交易所的价格与其他交易所长期相差约 10 倍、0.1 倍或其他固定比例：

* 不得自动静默缩放；
* 先检查产品是否为不同底层、ADR、合约乘数或指数单位；
* 在报告中标记 `SCALE_MISMATCH`；
* 未确认前将该交易所排除出直接价差排行榜。

本研究不得把美股 `SKHY` ADS 与 `SKHYNIX` 永续混在一起。

# 四、需要下载的数据

## 4.1 历史价格

优先下载 1 分钟数据：

1. 永续成交价格 K 线；
2. 标记价格 K 线；
3. 指数价格 K 线；
4. 成交量。

优先级：

```text
标记价格 > 成交收盘价 > 指数价格
```

但三类数据都要尽可能保存，不能互相覆盖。

统一字段：

```text
exchange
symbol
price_type
open_time
close_time
open
high
low
close
volume_base
volume_quote
source_endpoint
retrieved_at
raw_file
```

其中 `price_type` 使用：

```text
trade
mark
index
```

如果某交易所没有历史标记价格或历史指数价格接口：

* 保存成交 K 线；
* 在数据质量报告注明；
* 不得用成交价格伪装成标记价格。

## 4.2 实际资金费率历史

下载每一笔实际结算的资金费率事件，不只下载当前预测费率。

统一字段：

```text
exchange
symbol
funding_time
funding_rate
funding_interval_hours
mark_price_if_available
index_price_if_available
source_endpoint
retrieved_at
raw_file
```

约定：

* `funding_rate > 0` 表示通常由多头支付给空头；
* 必须保留交易所返回的原始正负号；
* 不得提前把费率转换成 APR 后丢掉原始值；
* 资金费率结算周期可能在历史期间变化。

历史结算周期判断顺序：

1. 官方历史记录自带的 interval；
2. 合约 metadata；
3. 相邻资金费率事件的时间差；
4. 交易所公告或官方文档。

若通过相邻时间差推断，添加：

```text
interval_source = inferred_from_events
```

Hyperliquid 的实际小时资金费率事件应按实际结算事件保存，不要重复按八小时再拆一次。

## 4.3 建议的官方接口起点

以下仅作为实现起点。运行前检查最新官方文档和实际响应。

### Hyperliquid

使用 `POST /info`，研究：

```json
{"type": "fundingHistory", "coin": "...", "startTime": 0, "endTime": 0}
```

以及：

```json
{
  "type": "candleSnapshot",
  "req": {
    "coin": "...",
    "interval": "1m",
    "startTime": 0,
    "endTime": 0
  }
}
```

同时使用相关 metadata 查询发现 HIP-3/builder-deployed perp 的真实名称。

### Binance USD-M Futures

候选接口：

```text
GET /fapi/v1/exchangeInfo
GET /fapi/v1/klines
GET /fapi/v1/markPriceKlines
GET /fapi/v1/indexPriceKlines
GET /fapi/v1/fundingRate
GET /fapi/v1/fundingInfo
```

如果该产品属于特殊的 TradFi perpetual 分类，验证标准接口是否仍适用。

### Bitget

优先检查当前 UTA/V3 公共市场接口：

```text
GET /api/v3/market/instruments
GET /api/v3/market/history-candles
GET /api/v3/market/history-fund-rate
```

如果当前产品或地区仍需使用 V2 合约行情接口，允许实现官方 V2 fallback，但必须在 `sources_used.json` 中记录实际接口版本。

### OKX

候选接口：

```text
GET /api/v5/public/instruments
GET /api/v5/market/history-candles
GET /api/v5/market/history-mark-price-candles
GET /api/v5/market/history-index-candles
GET /api/v5/public/funding-rate-history
```

必须正确处理 OKX 的 `before`、`after` 游标方向及倒序响应。

### Gate

候选接口：

```text
GET /api/v4/futures/usdt/contracts
GET /api/v4/futures/usdt/contracts/SKHYNIX_USDT
GET /api/v4/futures/usdt/candlesticks
GET /api/v4/futures/usdt/funding_rate
```

Gate 标记和指数 K 线可检查官方接口对 `mark_`、`index_` 合约前缀的支持。

# 五、快速产出的执行顺序

严格按照以下优先级执行。

## 阶段 A：最小可用结果

先完成：

1. 五家交易所产品验证；
2. 五家交易所实际资金费率历史；
3. 五家交易所 1 分钟成交 K 线；
4. 对齐数据；
5. 生成第一版资金费率和价格差报告。

一旦阶段 A 数据可用，立即在本地生成：

```text
reports/EXECUTIVE_SUMMARY.md
reports/quick_report.html
reports/exchange_coverage.csv
reports/pairwise_price_summary.csv
reports/pairwise_funding_summary.csv
```

不要为了等待某个次要接口而延迟阶段 A 报告。

## 阶段 B：提高准确度

阶段 A 完成后继续：

1. 回补标记价格；
2. 回补指数价格；
3. 加入数据质量检测；
4. 加入价差持续时间和简单事件回测；
5. 重新生成完整报告。

# 六、韩国盘后时段定义

使用韩国交易所日历。优先尝试支持 XKRX 的可靠交易日历库；如库不支持 2026 年日历，应通过可靠来源核对韩国节假日，并在报告中说明日历来源。

韩国时区：

```text
Asia/Seoul
```

韩国无夏令时。

对韩国正常交易日定义以下 UTC 标签：

```text
PRE_CLOSE_BASELINE:
05:50:00 <= UTC < 06:30:00

POST_CLOSE_TRANSITION:
06:30:00 <= UTC < 06:40:00

KRX_OFFICIAL_AFTER_HOURS:
06:40:00 <= UTC < 09:00:00

KRX_FULLY_CLOSED:
09:00:00 <= UTC < 次日 00:00:00

KRX_REGULAR_EARLIER:
00:00:00 <= UTC < 05:50:00
```

为了主要研究“盘后”，汇总报告重点使用：

```text
AFTER_CLOSE_ALL =
POST_CLOSE_TRANSITION
+ KRX_OFFICIAL_AFTER_HOURS
+ KRX_FULLY_CLOSED
```

周末及韩国休市日使用：

```text
KRX_HOLIDAY_OR_WEEKEND
```

同时保留 `PRE_CLOSE_BASELINE`，用于比较收盘后价差是否明显扩张。

不要把周末数据删除；单独统计周末和节假日。

# 七、标准化和分钟对齐

构建统一的 1 分钟 UTC 时间轴。

规则：

1. 使用 K 线开盘时间作为分钟键；
2. 对齐前先去重和排序；
3. 不使用未来数据；
4. 只允许最多向前填充 2 分钟；
5. 连续缺失超过 2 分钟时，该交易所该时间点记为缺失；
6. 不得跨交易暂停区间长时间填充；
7. 每个价差值必须保存两个交易所对应价格的原始时间戳和数据年龄；
8. 只有两个交易所都有有效价格时才计算价差；
9. 对明显为零、负数、NaN 或无穷的价格直接剔除并记录。

生成：

```text
data/normalized/prices_1m.parquet
data/normalized/funding_events.parquet
data/normalized/instrument_metadata.parquet
data/normalized/aligned_prices_1m.parquet
```

同时创建：

```text
data/research.duckdb
```

# 八、价格差计算

五家交易所最多形成 10 个两两组合。

历史数据通常不是当时真实 BBO，因此禁止把历史 K 线结果称为“可执行价差”。

使用以下名称：

```text
mark_spread_bps
trade_close_spread_bps
index_spread_bps
historical_proxy_spread_bps
```

对交易所 A 和 B：

```text
spread_A_over_B_bps =
10000 * (price_A / price_B - 1)
```

同时计算对称差，避免选择分母造成的表述混乱：

```text
symmetric_spread_bps =
10000 * 2 * (price_A - price_B) / (price_A + price_B)
```

排行榜优先使用：

```text
abs(symmetric_spread_bps)
```

但报告必须保留方向：

```text
higher_exchange
lower_exchange
```

标记价格存在时，以标记价格为主；否则使用成交收盘价，并添加：

```text
comparison_quality = trade_close_proxy
```

# 九、资金费率差计算

资金费率事件按各交易所真实结算时点保存。

对于“交易所 A 做多、交易所 B 做空”，每 1 美元相同名义本金的资金费率现金流：

```text
funding_pnl_long_A_short_B
= -sum(funding_rate_A)
  +sum(funding_rate_B)
```

仅在相应合约和时间范围确实存在时累计。

输出：

1. 原始资金费率事件数；
2. 每家交易所累计资金费率；
3. 每家交易所平均小时资金费率；
4. 每家交易所简单年化展示值；
5. 每一对交易所两个方向的累计资金费率收益；
6. 换算为单边名义本金 10,000 美元时的理论现金流。

简单年化仅用于比较，明确标记：

```text
simple_apr_not_compounded
```

不要使用网页显示的 APR 代替历史结算事件。

对不同结算周期，创建：

```text
hourly_equivalent_rate = funding_rate / interval_hours
```

同时保留原始费率，且不得把小时等效费率当作真实每小时现金流。

# 十、必须生成的统计结果

## 10.1 数据覆盖率

每个交易所分别输出：

```text
resolved_symbol
first_price_time
last_price_time
price_rows
first_funding_time
last_funding_time
funding_rows
expected_minutes
available_minutes
coverage_percent
longest_gap_minutes
data_types_available
errors
```

特别说明：

* 产品上市前没有数据属于正常情况；
* 不能用空值补齐上市前区间；
* 报告必须显示每家交易所真正可比较的共同起点。

## 10.2 每一对交易所的价格差

按以下时段分别计算：

```text
ALL
PRE_CLOSE_BASELINE
AFTER_CLOSE_ALL
KRX_OFFICIAL_AFTER_HOURS
KRX_FULLY_CLOSED
KRX_HOLIDAY_OR_WEEKEND
```

统计：

```text
count
coverage_percent
mean_bps
median_bps
std_bps
min_bps
max_bps
p01_bps
p05_bps
p25_bps
p75_bps
p95_bps
p99_bps
mean_abs_bps
p95_abs_bps
p99_abs_bps
max_abs_bps
percent_A_higher
percent_B_higher
```

## 10.3 阈值事件

分别识别绝对价差超过以下阈值的连续事件：

```text
10 bps
20 bps
50 bps
100 bps
200 bps
```

一个事件中间最多允许缺失 1 分钟。

输出：

```text
pair
session
threshold_bps
event_start
event_end
duration_minutes
peak_abs_spread_bps
peak_time
higher_exchange_at_peak
lower_exchange_at_peak
spread_at_end_bps
```

汇总：

```text
event_count
median_duration_minutes
p95_duration_minutes
max_duration_minutes
```

## 10.4 收敛分析

针对每个阈值事件，计算：

* 达到峰值后回落到 50% 峰值所需时间；
* 回落到 20 bps 所需时间；
* 回落到 10 bps 所需时间；
* 在 1、5、15、30、60、240 分钟后的价差；
* 事件期间的最大继续扩张；
* 是否在下一次 KRX 开盘前收敛。

不要把这部分包装成真实可成交回测；命名为：

```text
historical_convergence_study
```

## 10.5 简单交易成本敏感性

由于没有历史 BBO，不估计精确成交利润，只做总交易成本敏感性。

使用总往返成本场景：

```text
10 bps
20 bps
40 bps
80 bps
```

这里的总成本已经包含两家交易所的开仓和平仓。

对价差事件计算：

```text
gross_convergence_bps
net_after_cost_10bps
net_after_cost_20bps
net_after_cost_40bps
net_after_cost_80bps
```

报告必须明确：

* 这是历史分钟价格代理；
* 不包含当时真实盘口滑点；
* 不代表可以实际成交的利润。

# 十一、图表

至少生成以下图表，保存为 PNG，并嵌入 HTML 报告：

```text
reports/charts/data_coverage.png
reports/charts/after_close_p95_spread_heatmap.png
reports/charts/after_close_max_spread_heatmap.png
reports/charts/funding_cumulative_by_exchange.png
reports/charts/funding_pair_matrix.png
reports/charts/top_3_pair_spread_timeseries.png
reports/charts/spread_event_duration.png
reports/charts/preclose_vs_afterclose.png
```

绘图要求：

* 图标题和说明使用中文；
* 横轴时间显示 UTC；
* 图中注明使用 mark、trade close 还是其他代理；
* 极端值不得悄悄截断；
* 如使用对数轴必须明确注明；
* 数据缺口不能画成连续真实价格。

# 十二、报告文件

最终必须产生：

```text
reports/EXECUTIVE_SUMMARY.md
reports/quick_report.html
reports/data_quality.md
reports/exchange_coverage.csv
reports/pairwise_price_summary.csv
reports/pairwise_funding_summary.csv
reports/funding_events_normalized.csv
reports/spread_events.csv
reports/convergence_events.csv
reports/top_opportunities.csv
reports/sources_used.json
reports/run_manifest.json
```

## EXECUTIVE_SUMMARY.md 必须回答

用中文直接回答：

1. 实际成功获得了哪几家交易所的数据？
2. 各家产品真正开始有数据的时间是什么？
3. 五家共同可比较的历史起点是什么？
4. 盘后价差最大的是哪几个交易所组合？
5. P95、P99 和最大盘后价差分别是多少？
6. 哪家交易所通常价格更高？
7. 最大价差持续多久？
8. 价差通常在多长时间内收敛？
9. 哪些大价差发生在周末、休市或交易暂停附近？
10. 历史资金费率最高的是哪家？
11. 最有利的“做多哪家、做空哪家”资金费率组合是什么？
12. 每 10,000 美元单边名义本金的累计理论资金费率差是多少？
13. 价格高的一边是否同时拥有更有利的做空资金费率？
14. 在总成本 10、20、40 和 80 bps 场景下，还有多少历史事件具有正的价格收敛空间？
15. 哪些结论可能是数据缺失、价格单位差异、休市机制或历史 K 线局限导致的？
16. 下一步是否值得部署实时 BBO 和五档盘口采集？

结论必须区分：

```text
强结论
初步结论
因数据不足无法确认
```

不要只堆表格，要给出清晰判断。

# 十三、top_opportunities.csv

按照以下字段生成历史机会排行榜：

```text
rank
pair
session
event_start
event_peak_time
event_end
long_exchange_at_peak
short_exchange_at_peak
peak_spread_bps
gross_convergence_bps
funding_advantage_bps_during_event
combined_gross_bps
net_10bps
net_20bps
net_40bps
net_80bps
duration_minutes
data_quality
warnings
```

排序优先级：

```text
combined_gross_bps descending
data_quality descending
duration_minutes descending
```

不得把缺少退出价格、存在比例错配或数据过期的事件排在正常事件之前。

# 十四、工程结构

创建类似以下目录：

```text
.
├── pyproject.toml
├── README.md
├── Makefile
├── config.yaml
├── src/
│   └── skhynix_research/
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── http.py
│       ├── storage.py
│       ├── calendar.py
│       ├── normalize.py
│       ├── validation.py
│       ├── analysis.py
│       ├── reporting.py
│       └── exchanges/
│           ├── base.py
│           ├── hyperliquid.py
│           ├── binance.py
│           ├── bitget.py
│           ├── okx.py
│           └── gate.py
├── tests/
├── data/
│   ├── raw/
│   ├── normalized/
│   └── research.duckdb
├── reports/
│   └── charts/
└── logs/
```

推荐依赖：

```text
httpx
tenacity
polars
pandas
duckdb
pyarrow
numpy
scipy
statsmodels
plotly
kaleido
jinja2
pyyaml
exchange-calendars
pytest
```

可以调整依赖，但要保持项目简单。

# 十五、命令行接口

实现以下命令：

```bash
uv run skhynix-research discover

uv run skhynix-research download \
  --start 2026-06-10T05:50:00Z \
  --end now

uv run skhynix-research analyze

uv run skhynix-research report
```

并提供一键命令：

```bash
make quick
```

`make quick` 必须：

1. 创建所需目录；
2. 验证产品；
3. 下载或续传历史数据；
4. 标准化；
5. 分析；
6. 生成全部报告；
7. 运行基本数据检查；
8. 在终端打印报告文件位置和最重要的五条结论。

如果部分交易所失败，`make quick` 仍应尽可能退出成功并生成报告，但在报告和终端明显显示失败情况。

只有完全没有任何可用交易所数据时才整体失败。

# 十六、日志和运行清单

保存：

```text
logs/download.log
logs/analysis.log
reports/run_manifest.json
```

`run_manifest.json` 至少包含：

```json
{
  "requested_start": "",
  "actual_run_end": "",
  "git_commit": "",
  "python_version": "",
  "package_versions": {},
  "successful_exchanges": [],
  "failed_exchanges": [],
  "resolved_symbols": {},
  "raw_file_count": 0,
  "normalized_row_counts": {},
  "report_files": [],
  "warnings": []
}
```

每个 API 请求失败时记录：

```text
exchange
endpoint
parameters_without_secrets
status_code
attempt
error
timestamp
```

# 十七、测试和验收

至少编写并运行测试，覆盖：

1. UTC 时间解析；
2. 韩国盘后时段标签；
3. 资金费率正负号；
4. 不同结算周期标准化；
5. 对称价差公式；
6. 分钟对齐不使用未来数据；
7. 缺失超过 2 分钟不继续填充；
8. 价差事件合并；
9. 重复运行不会产生重复数据；
10. 固定比例价格错配检测。

实际执行：

```bash
pytest -q
make quick
```

验收条件：

* 至少成功获取两家交易所的数据；
* 至少生成一张价格对比表；
* 至少生成一张资金费率对比表；
* HTML 报告可独立打开；
* CSV 文件非空或明确说明无数据；
* 所有报告包含数据截止 UTC 时间；
* 报告写明历史 K 线不是可执行 BBO；
* 报告写明任何缺失和接口失败；
* 不存在静默伪造、无限填充或错误缩放。

# 十八、失败降级策略

如果某个接口失败：

1. 检查最新官方文档；
2. 检查产品 metadata 是否仍存在；
3. 检查参数、时间单位、分页方向和 API 版本；
4. 使用该交易所官方允许的备用公共接口；
5. 保存失败响应；
6. 继续其他交易所。

如果标记价格不可回补：

```text
使用成交收盘价
comparison_quality = trade_close_proxy
```

如果 1 分钟数据不可回补但 5 分钟可用：

```text
使用 5 分钟
comparison_quality = lower_frequency_proxy
```

不得把 5 分钟数据伪装成 1 分钟。

如果某交易所完全不可用：

* 从成对组合中排除；
* 报告失败原因；
* 仍输出剩余交易所的所有结果。

不得使用代理服务器或绕过地域、认证和访问限制。

# 十九、最终 Codex 回复格式

完成代码和运行后，你的最终回复必须简洁列出：

1. 实际执行过的命令；
2. 成功交易所；
3. 失败交易所及原因；
4. 实际数据区间；
5. 五家共同或最大可比较区间；
6. 最重要的五条数据发现；
7. 生成的报告文件路径；
8. 测试结果；
9. 尚未解决的数据限制。

不要以“代码已经准备好，请自行运行”结束。

你必须亲自运行 `make quick`，读取生成的结果，并在最终回复中报告实际数字。
