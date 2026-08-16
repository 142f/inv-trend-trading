# 历史行情数据基础设施

## 1. 设计边界

本模块负责“外部行情 → 可审计不可变数据 → 质量门禁 → 审核发布 → 策略只读 Repository”。核心约束如下：

- `BTC` / `ETH`：Binance Spot `BTCUSDT` / `ETHUSDT`，不与永续、USD 或其他交易所序列拼接；配置最早边界为 `2017-08-17`。
- `XAU` / `XAG`：`XAUUSD_DUKAS` / `XAGUSD_DUKAS` 的 OTC Spot/CFD 报价，不与 COMEX、GLD、SLV 混用。
- 股票、ETF、期货、永续分别注册独立 instrument；连续期货必须显式记录 `contract`、`roll_flag`、`continuous_method`。
- `D1`、`H4`、`H1` 独立分区；系统不插值、拆分或聚合伪造缺失历史。
- 策略正式读取只接受 `CURATED` 且 `is_complete == True` 的 K 线。

## 2. 模块与依赖方向

```text
historical_data.cli
        ↓
HistoricalDataService                    # 用例编排
        ├── Provider Adapter             # 网络/CSV 来源
        ├── processing / calendar        # 标准化与质量规则
        ├── ReviewApprovalCoordinator    # 审核发布事务
        └── DataLake                     # 文件、Catalog、Pointer 基础设施
                ├── integrity            # 统一哈希
                ├── locking              # 跨进程文件锁
                └── lineage              # current 全链路独立核验
```

职责：

| 文件 | 职责 |
|---|---|
| `api.py` | ingest/update/reprocess/load/coverage 等应用服务编排 |
| `review.py` | 审批上下文、审批计划、不可变审批产物、幂等与最终激活 |
| `lineage.py` | Pointer、Dataset Manifest、Curated、Quality Report、Catalog 的一致性校验 |
| `storage.py` | Parquet/JSON I/O、Catalog、Pointer、激活日志、回滚和恢复 |
| `integrity.py` | JSON、文件和 DataFrame 的确定性哈希 |
| `locking.py` | 无第三方依赖的跨进程文件锁及所有权保护 |
| `providers.py` | Binance、Dukascopy、CSV、持仓等 Adapter |
| `processing.py` | Schema、去重冲突、质量评估和隔离规则 |

核心策略代码不需要知道 Provider、Catalog 后端或文件路径；统一通过 `load_bars()` 读取已发布数据。

## 3. 数据流

```text
Provider fetch
  → immutable raw payload + parsed raw frame
  → normalize / validate / duplicate-conflict detection
  → normalized parquet + quarantine/review candidate
  → quality report + ingestion manifest
  → publish candidate artifacts without activation
  → review approval transaction
  → curated current
  → load_bars() independent lineage verification
```

完全相同的重复 K 线按审计规则去重；同一时间戳但 OHLCV 不同会进入 `REVIEW_REQUIRED`，保留 `base.parquet`、`incoming.parquet`、`conflicts.json` 和 `candidate_manifest.json`，不静默 `keep="last"`。

## 4. 数据目录

```text
data/
├── raw/provider=.../instrument=.../request_date=YYYY-MM-DD/
│   ├── <run_id>.<payload_suffix>
│   └── <run_id>.parquet
├── normalized/asset_class=.../instrument=.../timeframe=.../year=.../
│   └── part-*.parquet
├── curated/asset_class=.../instrument=.../timeframe=.../version=.../
│   └── bars.parquet
├── reference/holdings/fund=.../snapshot_date=.../
├── quarantine/run_id=.../
├── reviews/
│   ├── candidate=<run_id>/
│   └── <run_id>.json
├── manifests/
│   ├── <run_id>.json
│   ├── dataset-<dataset_version>.json
│   └── approval-<dataset_version>.json
├── quality_reports/<dataset_version>.json
├── current/<symbol>/<timeframe>.json
├── operations/current/*.json
├── locks/*.lock
├── catalog_backend.json
└── catalog.sqlite3 | catalog.duckdb
```

Manifest、Pointer、Catalog 中的路径统一保存为数据根相对路径；旧绝对路径仍可解析以维持兼容。

## 5. 不可变写入与 Catalog 后端

- Parquet 使用 ZSTD；先写同目录临时文件，`fsync` 后 `os.replace` 原子提交。
- 同一路径重试时按 DataFrame 语义哈希检查：内容相同幂等返回，内容不同直接报错，禁止覆盖不可变制品。
- JSON/原始 payload 同样采用不可变写入；可变 Pointer 和激活日志采用原子替换。
- `catalog_backend.json` 是后端选择的唯一权威标记，记录 `backend`、`catalog_path`、`schema_version`。
- 同时存在 SQLite 与 DuckDB 且缺少标记时 fail closed，避免猜测错误 Catalog；已有旧单一 Catalog 会自动生成标记。
- 选择 DuckDB 但环境未安装 `duckdb` 时立即抛出明确依赖错误。

## 6. 审核发布事务

`ReviewApprovalCoordinator` 的审批顺序：

```text
1. 获取 reviews/<run_id> 专用锁
2. 校验候选 Manifest、候选文件、父版本及全部输入哈希
3. 读取或创建不可变 approval_plan.json
4. 生成 approved bars
5. 生成并校验 approved quality report
6. 生成 dataset manifest
7. 预计算审核记录并生成完整 approval manifest
8. 写 Catalog dataset 记录和不可变 review record
9. 重新校验所有审批制品及哈希
10. 以 expected-current CAS 最后激活 Pointer + Catalog current
```

Approval Manifest 覆盖：批准后的 bars、质量报告、Dataset Manifest、候选 bars、候选 Manifest、ingestion Manifest/Report、审批计划和审核记录哈希。

幂等规则：

- 已批准且 `reason/actor/decision` 完全相同：返回既有 `published_version`，不会生成第二个版本。
- 已批准但请求参数不同：拒绝，且不修改 current。
- 已拒绝：不得直接批准；必须通过未来显式 reopen 流程。
- 中断发生在最终激活前：不可变制品可复用，重复调用从已有审批计划继续。

## 7. Pointer、Catalog 与故障恢复

`current` 激活不是简单的两次写入。系统在品种/周期级锁内：

1. 校验 expected current（包含“首次发布时预期为空”的 CAS）；
2. 写入 `operations/current/` 激活日志，保存 previous/target 状态；
3. 更新 Pointer；
4. 更新 Catalog current；
5. 再次校验两者一致；
6. 成功后删除激活日志。

任一步骤失败会恢复 previous Pointer/Catalog；进程崩溃后，`DataLake` 启动时扫描日志并回滚未完成激活。`repair-current` 用于显式重新对齐：

```bash
market-data --root data repair-current --symbol BTC --timeframe D1
```

文件锁保存 PID、创建时间和随机 token：活进程持有的长锁不会因超龄被抢；旧锁对象释放时也不能删除后继者的新锁。

## 8. Coverage 与策略读取：fail closed

`coverage()` 和 `load_bars()` 不再只相信当前 Parquet。`load_current_lineage()` 会独立核验：

- current Pointer 存在且含 version；
- Pointer 与 Dataset Manifest 指向同一 Curated 文件；
- Dataset Manifest 的 symbol/timeframe/version 正确；
- Curated SHA-256 正确；
- Quality Report 存在、哈希正确、版本/品种/周期一致；
- `actual_bars` 与正式数据行数一致；
- Bars 中 `dataset_version` 与 current 一致；
- Pointer 与 Catalog current 一致。

报告缺失、哈希不匹配、行数不一致或版本错配都会抛 `DataLineageError`，不会把“无法验证”误报为“无缺口”。

## 9. 增量、质量与数据源

- `update()` 计算尾部和适用品种的内部缺口，只请求缺失区间；无缺失时不联网。
- `update-gaps` 只补内部缺口；`missing` 只读展示。
- 请求边界使用 `max(requested_start, listing_start)`；上市前不算缺失，上市后真实交易日缺失按规则分类。
- 24×7 加密资产日线必须连续；美股/ETF 使用规则型 NYSE 日历，holiday/weekend 不计缺失，未知 halt 标记 `UNKNOWN`。
- 当前规则日历不是交易所官方 halt/holiday 数据源；正式高等级审计仍需持牌/官方日历。
- Dukascopy 已支持分页和逐页原始响应留存路径，但本交付环境未连接真实服务验证其实际响应契约。

## 10. CLI 示例

```bash
# 下载、增量、缺口、重放
market-data --root data download --symbol BTC --timeframe D1 \
  --start 2017-08-17T00:00:00Z
market-data --root data update --symbol BTC --timeframe D1
market-data --root data missing --symbol BTC --timeframe D1
market-data --root data update-gaps --symbol BTC --timeframe D1
market-data --root data reprocess --symbol BTC --timeframe D1 --run-id <run_id>

# 审计和版本
market-data --root data status --symbol BTC --timeframe D1
market-data --root data coverage --symbol BTC --timeframe D1
market-data --root data versions --symbol BTC --timeframe D1
market-data --root data verify --kind catalog
market-data --root data audit --symbols BTC ETH --timeframe D1

# 审核
market-data --root data review list
market-data --root data review approve --run-id <run_id> \
  --decision approve_existing --reason "reviewed" --actor "alice"
market-data --root data review reject --run-id <run_id> \
  --reason "unresolved conflict" --actor "alice"
```

统一策略输入：

```python
from historical_data import load_bars

bars = load_bars("BTC", "D1", adjusted=True, root="data")
```

返回数据按 timestamp 升序且唯一，只含完整 K 线；`attrs` 包含来源、版本、质量报告等谱系信息。

## 11. 当前验收边界

| 范围 | 状态 |
|---|---|
| 审批幂等、完整 Manifest、Coverage fail-closed、Catalog 冲突检测 | ✅ COMPLETE |
| Pointer/Catalog 锁、日志、回滚、启动恢复、CAS | ✅ COMPLETE（逻辑与故障注入测试） |
| 真实 PyArrow ZSTD 元数据与编码失败原子性 | ⚠️ 当前环境未执行 |
| 真实 DuckDB 事务、Windows `fsync/os.replace` | ⚠️ 需目标环境验证 |
| 真实 Binance/Dukascopy/MT5 响应与实际数据根 | ⚠️ 未随代码包提供，无法独立复核 |
| 官方交易日历/halt、完整 QQQ 历史谱系、research 全量 Repository 迁移 | ⚠️ 后续范围 |

因此当前结论是：**P0-core 代码闭环完成；P0 Final 仍需真实依赖、目标操作系统、Provider 与数据资产验收。**
