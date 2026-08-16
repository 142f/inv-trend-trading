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
provider adapter -> immutable raw (payload + parsed frame, 双写)
   -> normalize/validate -> quarantine + audit flags
   -> partitioned normalized parquet (zstd, 原子提交)
   -> quality report + manifest + DuckDB/SQLite catalog
```

目录为：

```text
data/
  raw/provider=.../instrument=.../request_date=YYYY-MM-DD/{run_id}.{parquet,json}
  normalized/asset_class=.../instrument=.../timeframe=.../year=.../part-*.parquet
  reference/qqq_holdings/snapshot_date=YYYY-MM-DD/top20.parquet
  manifests/{run_id}.json
  quality_reports/{dataset_version}.json
  quarantine/run_id={run_id}/rows.parquet
  reviews/candidate={run_id}/bars.parquet
  catalog.sqlite3 | catalog.duckdb (+ catalog_backend.json 标记)
```

原始响应（payload）与 provider 解析后的原始 frame 都按 `run_id` 只读落盘，
绝不覆盖；`market-data reprocess --run-id ...` 可在不联网的情况下从 Raw 重跑
预处理，内容寻址保证复现完全相同的 dataset_version。所有 Parquet 使用 zstd
压缩并通过临时文件 + fsync + 原子改名提交，进程中断不会留下半成品。

`HistoricalDataService.update()` 先计算缺失区间（尾部 + 24×7 品种的内部缺口），
只下载真正缺失的区间，重复执行幂等：无缺失时直接返回当前版本的 manifest，
不产生任何网络请求。`update-gaps` 仅补齐内部缺口，`missing` 命令只读展示
缺失区间。

Catalog（datasets 表）记录 run_id、symbol、instrument_id、timeframe、source、
status、version、asset_class、start_time、end_time、row_count、schema_version、
checksum、raw_source、created_at、updated_at，保证 Dataset→Raw→Provider
血缘可追溯；旧库在首次打开时自动迁移新增列。

Curated 层只发布 `is_complete == True` 的完整 K 线（当天未收盘 D1 不进入正式
回测数据）；若所有 bar 均未收盘，数据集不得发布（QUARANTINED，绝不 fallback
发布全 partial 版本）。Quality Report 同时记录 stored/complete/incomplete
行数与 latest_stored_bar/latest_complete_bar，`backtest_suitable` 仅按完整
K 线视图判定。`market-data audit --symbols ...` 独立复算全部统计（按
requested/listing/日历重建期望区间，不信任已有报告），`market-data verify`
审计 Catalog 行、Manifest、全部 artifact（raw/normalized/quality/curated）、
hash 链与 pointer/catalog 一致性，并单独列出 legacy 污染的 current 指针。

质量评估按请求区间（expected_start = max(requested_start, listing_start)）
计算缺失：股票请求 20 年只返回 10 年 → 头部缺失如实计数并 fail closed；
上市日之后缺失的真实交易日计入 provider_gap；请求早于上市日的时间不算缺失。
`provider_available_start` 只在 Provider 显式声明时记录，绝不等于 actual_start。

去重是冲突检测而非静默 `keep="last"`：完全相同（timestamp+OHLCV）→ 去重并
记入 audit；相同 timestamp 但 OHLCV 不同 → `CONFLICTING_DUPLICATE` →
REVIEW_REQUIRED。Review 候选保留双方数据（base.parquet / incoming.parquet /
conflicts.json / candidate_manifest.json），审批可显式选择
`approve_existing` 或 `approve_incoming`。Repository 读取正式 Curated 时若
仍检测到冲突抛 `DataConflictError`。

美股/ETF（xnas/xnys）的缺失按 RULE_BASED_NYSE_CALENDAR 规则日历分类：
weekend/holiday 不计缺失，真实交易日缺失记为 provider_gap，未知 halt 标记
为 UNKNOWN（规则日历是近似，非交易所官方日历）。加密资产（24x7）要求
日线连续：缺一天即质量失败（QUARANTINED）。stock instrument 请求早于上市日
的时间不算缺失。

Provider 适配器统一实现 `fetch / fetch_range / normalize_symbol /
validate_response`；HTTP 请求带超时、指数退避重试，429/5xx 遵循
Retry-After 退避。

Manifest、current pointer 与 Catalog 中的路径一律保存 **root-relative**
路径（如 `curated/asset_class=crypto/...`），读取时以数据根目录解析；
旧版绝对/工程相对路径仍可读取（向后兼容）。发布时 pointer 与 Catalog
先写后验、不一致即回滚；`market-data repair-current --symbol ... --timeframe ...`
可将 Catalog current 与文件系统指针重新对齐。XAU/XAG D1 采用 Dukascopy
provider-native UTC 日线（`session_timezone=UTC`、`bar_close_rule=
provider_native_utc`、`dayStartTime=UTC`），与 Registry 声明一致；纽约 17:00
OTC 日线属于派生数据集，不作为原生 D1。

## 使用

```powershell
pip install -e .
market-data download --symbol BTC --timeframe D1 `
  --start 2017-08-17T00:00:00Z --root data
```

受许可金属 CSV：

```powershell
market-data download --symbol BTC --timeframe D1 `
  --start 2017-08-17T00:00:00Z --root data
```

增量与缺口管理：

```powershell
market-data update --symbol BTC --timeframe D1 --root data
market-data missing --symbol BTC --timeframe D1 --root data
market-data update-gaps --symbol BTC --timeframe D1 --root data
market-data reprocess --symbol BTC --timeframe D1 --run-id <run_id> --root data
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

`HistoricalDataService.update()` 从本地末尾向前重查 5 根 K 线，失败重试并按配置切换备用源；内容寻址分区与读取时去重使重复更新幂等。股票 `adjusted=True` 会按 `adjusted_close / close` 同步调整 OHLC，避免拆股形成虚假突破。质量评估按请求区间计算理论 K 线数：只有实际数据首尾的"部分下载"会被如实判定为缺失并隔离，未收盘的尾部边界不计缺失。

## 质量和实施阶段

- P0（已实现）：统一 Schema、Provider 适配器（重试/限流/响应校验）、Binance/CSV 下载、真实历史下限、清洗隔离、Parquet(zstd)+原子写入、Manifest、质量报告、DuckDB/SQLite 索引、缺失区间增量更新、统一读取、Raw 重放预处理、QQQ 快照日期保护、复权和期货换月工具。
- P1：部署持牌 Nasdaq/贵金属账户适配器，补齐交易所节假日日历、公司行动和历史 QQQ 快照，并配置真实备用源交叉验证。
- P2：任务调度、数据延迟监控、供应商 SLA、对象存储镜像和全量历史回填审计。

许可证和覆盖范围取决于部署者实际订阅。配置中的 2000-01-01 是 XAU/XAG 的目标请求下限，不是对供应商实际覆盖的声明；Manifest 始终记录实际首尾时间。
