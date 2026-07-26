# 历史行情数据模块

## 边界与品种定义

- `BTC`/`ETH` 是 Binance `BTCUSDT`/`ETHUSDT` 现货，不与 USD、永续或其他交易所序列拼接；配置的最早边界均为 2017-08-17，下载器不会请求或生成更早数据。
- `XAU`/`XAG` 是 `XAUUSD_DUKAS`/`XAGUSD_DUKAS` 的 OTC 现货/CFD 报价，按 UTC、24×5 保存；它们不是 COMEX 期货，也不与 GLD/SLV 混用。受许可导出文件由 `CsvBarsProvider` 接入。
- 股票、ETF、期货和永续合约必须注册为独立 instrument。期货连续序列使用显式 `contract`、`roll_flag` 和 `continuous_method`。
- D1、H4、H1 独立分区。没有真实来源的数据不会聚合、拆分或插值产生。

## 数据源

- 加密货币主源：Binance 公共 Spot REST klines；可在配置中增加独立备用/交叉验证源。
- 贵金属：明确许可的 DukasCopy/vendor 批量导出；模块不抓取网页展示值。
- 美股：面向 Nasdaq Data Link 等持牌 OHLCV/公司行动导出使用 CSV 适配器。API 密钥、授权 URL 和许可由部署环境配置。
- QQQ：`QqqHoldingsCsvProvider` 只解析基金管理人或授权供应商的 CSV。来源必须给出快照日期；保存前 20、名称、权重、行业和来源。历史回测只能选取 `as_of` 当日或更早快照，没有快照会抛出 `SurvivorshipBiasError`，绝不以当前持仓代替。

## 数据流和存储

```text
provider -> immutable raw -> normalize/validate
         -> quarantine + audit flags
         -> partitioned normalized parquet
         -> quality report + manifest + SQLite catalog
```

目录为：

```text
data/
  raw/{asset_class}/{symbol}/{run_id}.parquet
  normalized/{d1|h4|h1}/asset_class=.../symbol=.../timeframe=.../year=.../
  reference/qqq_holdings/snapshot_date=YYYY-MM-DD/top20.parquet
  manifests/{symbol}/{timeframe}/{run_id}.json
  quality_reports/{symbol}/{timeframe}/{run_id}.json
  quarantine/{symbol}/{timeframe}/{run_id}.parquet
  catalog.sqlite3
```

原始文件按内容生成 `run_id`，只读、不可覆盖。Manifest 记录来源、请求/实际品种、范围、频率、行数、路径、SHA-256、许可、缺失、异常、清洗版本与质量结论。异常不会被静默删除：无效数据进入 quarantine，极端跳变保留在标准化数据并标记人工复核。

## 使用

```powershell
pip install -e .
market-data download --symbol BTC --timeframe D1 `
  --start 2017-08-17T00:00:00Z --root data
```

受许可金属 CSV：

```powershell
market-data download --symbol XAU --timeframe D1 `
  --start 2000-01-01T00:00:00Z --csv C:\licensed\xau.csv
```

QQQ 快照：

```powershell
market-data qqq-holdings --source C:\licensed\qqq_holdings.csv `
  --snapshot-date 2026-07-24
```

海龟检测器统一输入：

```python
from historical_data import load_bars

bars = load_bars("BTC", "D1", adjusted=True, root="data")
# timestamp 升序唯一、仅完整 K 线；attrs 含来源、复权口径、质量报告路径
```

`HistoricalDataService.update()` 从本地末尾向前重查 5 根 K 线，失败重试并按配置切换备用源；内容寻址分区与读取时去重使重复更新幂等。股票 `adjusted=True` 会按 `adjusted_close / close` 同步调整 OHLC，避免拆股形成虚假突破。

## 质量和实施阶段

- P0（已实现）：统一 Schema、Binance/CSV 下载、真实历史下限、清洗隔离、Parquet、Manifest、质量报告、SQLite 索引、增量更新、统一读取、QQQ 快照日期保护、复权和期货换月工具。
- P1：部署持牌 Nasdaq/贵金属账户适配器，补齐交易所节假日日历、公司行动和历史 QQQ 快照，并配置真实备用源交叉验证。
- P2：任务调度、数据延迟监控、供应商 SLA、对象存储镜像和全量历史回填审计。

许可证和覆盖范围取决于部署者实际订阅。配置中的 2000-01-01 是 XAU/XAG 的目标请求下限，不是对供应商实际覆盖的声明；Manifest 始终记录实际首尾时间。
