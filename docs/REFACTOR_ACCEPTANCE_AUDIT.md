# 重构验收、清理与回测一致性审计

审计日期：2026-08-16  
审计对象：当前工作树、`趋势交易核心-重构说明v1.md`，以及 Git 基线
`121f9016f8490baf42f5c2212aa46d97b9247290`。

## 裁决

本次审计确认：策略热路径在固定输入和同一组参数下与 Git 直接父提交
一致；未发现首个**业务语义**差异。审计同时发现并修复了四个数据发布
P0 缺口（正式 ingest CAS、策略读取的 Pointer/Catalog 校验、质量报告
identity 校验、`load_bars` 对未过滤数据的谱系校验），以及一次误导性的
Git 忽略规则和一处未使用导入。

因此，当前代码可标记为 **P0-core 本地验收通过**，但不能标记为“MD 所述
全部架构重构完成”或“无需提交即可发布的新正式 Baseline”：

- 正式 CLI 仍有绕过 application 层和共享规则的路径；
- 单一策略配置和单一 Turtle 规则内核尚未落地；
- 本轮新增的正式源码、测试、fixture 与文档仍需被 `git add` 并提交；
- 真实 Provider、真实数据根、生产规模 Catalog、独立进程崩溃恢复仍未验收。

## 审计方法与可追溯输入

| 项目 | 取值 |
|---|---|
| 严格基线 | Git `121f901`，为当前已提交重构 `347bf3f` 的直接父提交 |
| 辅助快照 | `inv-trend-trading-core-20260814.zip`，SHA-256 `554738bb5c8d81a21f7fdb466f6a9cb2ad90cb49fed4f980f7f294b2203f4f7a` |
| 比较输入 | `tests/fixtures/golden_d1.csv`，420 根 D1，2022-01-01 至 2023-02-24 UTC |
| 输入 SHA-256 | `8f41bfc6ee1060fec10e0f7117044839c14c5e65066a8767b778c7ad865e8cfe` |
| 参数快照 SHA-256 | `5d433e3f9163da5e97684c4c1936b8775b58b11fc9deac8161d7eddda01318a7` |
| 比较方式 | 两个隔离 Python 进程分别导入基线/当前源码，均读取同一绝对 fixture 路径；浮点数使用 `1e-10` 容差 |

ZIP 与 `347bf3f` 的策略树（`inv_trend_core`、`inv_trend_application`、
`inv_trend_observability`、`turtle_detector`、`turtle_multi_asset` 与
research 脚本）经 CRLF 规范化后没有策略源或策略 YAML 差异。ZIP 不含该
fixture，故它只作辅助交付快照；Git 父提交是本报告的严格可运行基线。

## MD 验收矩阵

| MD 要求 | 实际实现和调用证据 | 状态 |
|---|---|---|
| Review 审批幂等、拒绝后不可审批、制品先行 | `historical_data/review.py` 由 `HistoricalDataService.approve_review()` 调用；事务测试覆盖同参重试、参数变化拒绝、失败后续发 | ✅ COMPLETE |
| Approval Manifest 完整哈希链 | `historical_data.integrity.sha256_file`、`review.py` 对计划、候选、curated、质量报告和 manifest 重算并验证 | ✅ COMPLETE |
| Coverage / 策略读取 fail-closed | `lineage.load_current_lineage()` 校验 Pointer、Catalog、Dataset Manifest、curated hash、质量报告 hash/identity 与 bars；`load_bars()` 在过滤前调用它 | ✅ COMPLETE（本次补齐） |
| Catalog 后端标记和双库冲突 | `DataLake._select_catalog_backend()` 的 marker/冲突分支，历史数据测试覆盖 | ✅ COMPLETE |
| Pointer + Catalog 锁、journal、回滚、恢复、CAS | `locking.py`、`DataLake.activate_curated()`；本次使普通 `ingest()` 传入 `expected_current_version`，覆盖首次 `None` CAS | ✅ COMPLETE（本次补齐正式调用链） |
| 可选 MT5/OKX 不阻塞核心导入 | `inv_trend_integrations.__getattr__()`；实际导入包后 `okx`、`mt5` 都不在 `sys.modules` | ✅ COMPLETE |
| 420-bar Golden Master | `tests/fixtures/golden_d1.*` 与 `tests/test_golden_master.py` 固定输入、特征、daily、Detector、Multi 的 hash/指标 | ✅ COMPLETE；注意该 fixture 是重构后加入的固定输入，不是重构前预存制品 |
| Core 无 I/O/上层依赖、无静态环 | `tests/test_architecture_boundaries.py`；AST 扫描 129 个主模块、269 条内部边、0 个循环 | ✅ COMPLETE（模块级） |
| `turtle-detect` / `turtle-daily` 通过应用层 | `turtle_detector/cli.py → DetectorService`；`daily_cli.py → DailyMarketScanService` | ✅ COMPLETE |
| `turtle-alert` 通过应用层 | `alert_cli.py` 仍直接调用 `alerts.breakout.scan_configured_d1` | ⚠️ PARTIAL |
| `turtle-data` 回测通过应用层 | `data_cli.py → data/core_dataset.py → TurtleBacktester` 直接构造回测器；仅 research 脚本使用 `BacktestService` | ⚠️ PARTIAL |
| 共享 OHLCV 特征 | Detector、daily、组合回测使用 `PreparedBars`；`us_trend_alerts.py` 仍手写 20/55 Donchian | ⚠️ PARTIAL |
| 单一共享 Turtle 规则状态机 | `TurtleScanner`、多资产策略、breakout alert、daily、US alerts 仍各自处理突破/状态 | ⚠️ PARTIAL |
| 单一策略配置 / `ResolvedRunConfig` 正式使用 | `ResolvedRunConfig` 和 application `strategy.yaml` 只有定义与测试引用；运行仍分别读 detector/multi YAML | ❌ MISSING |
| Legacy 默认读取隔离 | `historical_data.legacy` 仅 `market-data migrate-legacy` 显式入口；正常读取拒绝 legacy | ✅ COMPLETE |
| 文档与运行时版本一致 | 本次已将 CLI/算法文档中的可执行 `legacy-v1` 改为只读归档说明；原 MD 的 248 测试数和旧环境结果是历史交付记录 | ⚠️ PARTIAL |
| 性能改善 | 热路径有可重复实测；没有真实生产数据根、I/O 和 Provider 的端到端 before/after | ⚠️ PARTIAL |
| P0 Final 目标环境封版 | 真实 Provider、真实数据资产、崩溃/并发与生产 Catalog 矩阵尚未执行 | ⚠️ PARTIAL |

## 审计期间修复的 P0 / 交付问题

| 问题 | 修复 | 回归证据 |
|---|---|---|
| `ingest()` 未把已观察 current 传给 CAS，过时首次任务可覆盖新发布 | `api.py` 调用 `activate_curated(..., expected_current_version=parent_dataset_version)` | 双 service 竞态注入 `test_ingest_rejects_a_stale_concurrent_first_publication` |
| `load_bars()` / `coverage()` 没有核 Pointer 与 Catalog current | `lineage.py` 比对 version 和 run_id；不一致即 `DataLineageError` | `test_strategy_read_fails_closed_on_pointer_catalog_drift` |
| 质量报告只校验 hash/version，未校验 symbol/timeframe | `lineage.py` 校验质量报告 identity | `test_coverage_fails_closed_on_quality_report_identity_mismatch` |
| `load_bars()` 在过滤后才间接检查、未传 bars，行数和 dataset_version 验证未发生 | 过滤前将完整 current bars 交给 `load_current_lineage()` | 既有 row-count 测试扩展为 `load_bars` 与 `coverage` 双断言 |
| `storage.sha256_file` 隐式再导出 | 调用方改为从 `historical_data.integrity` 导入，消除未使用导入和隐藏 API | 83 项历史数据分组与全量 pytest 通过 |
| `.gitignore` 忽略正式 `tests/` 与 `research/` | 取消目录级忽略；保留 `/research/**/outputs/`、`logs/`、`.tmp/` 等可再生产物忽略 | `git check-ignore` 确认 test/fixture/research source 不再被忽略 |

## 实际运行路径与剩余“假重构”风险

| 入口 | 当前路径 | 判定 |
|---|---|---|
| `turtle-detect` | CLI → `DetectorService` → detector adapter/repository | 已迁移 |
| `turtle-daily` | CLI → `DailyMarketScanService` → observability HTML | 已迁移 |
| `turtle-alert` | CLI → `alerts.breakout.scan_configured_d1` | 旧/平行路径仍正式执行 |
| `turtle-data build-metal-tech-core` | CLI → `core_dataset` → 直接 `TurtleBacktester` | 绕过 `BacktestService` |
| `turtle-data us-trend-alerts` | CLI → `us_trend_alerts`，允许 Yahoo/CSV/cache 回退且手写 Donchian | 绕过 canonical data/共享特征 |

`inv_trend_application/detector_scan_service.py` 及其 eligibility 组件没有正式
CLI 或生产调用者，但有专门单元测试。它们可能是尚未接入的功能，而不是已
确认废弃的兼容层；在没有产品退役决定前没有删除。

## 重构前后回测结果

固定参数如下：

- Detector：ATR 20，S1 20/10，S2 55/20，volatility 60，MA 20/55，false-breakout 3，初始资金 100,000，成本 5 bps；
- Multi：N 20，fast 20/10，slow 55/20，`skip_fast_after_win=True`，总 1N 风险 12%，初始资金 100,000，`GOLD` crypto、`qty_step=1`。

下表的所有“差异”为零（`1e-10` 内）；`—` 表示该回测器没有产生该标准字段，未以猜测值填充。

| 指标 | Detector 基线 | Detector 当前 | Multi 基线 | Multi 当前 |
|---|---:|---:|---:|---:|
| Initial Capital | 100,000 | 100,000 | 100,000 | 100,000 |
| Final Equity | 101,368.61814123158 | 相同 | 91,722.76474092288 | 相同 |
| Total Return | 1.3686181412% | 相同 | -8.2772352591% | 相同 |
| CAGR | 1.3887151102% | 相同 | -7.2549732769% | 相同 |
| Max Drawdown | -0.0938418240% | 相同 | -54.3747765211% | 相同 |
| Sharpe | 12.5169855574 | 相同 | —（`sharpe_like=-0.3740137342`） | 相同 |
| Sortino | — | — | — | — |
| Volatility | — | — | 0.1649565958 | 相同 |
| Trade Count | 5 | 相同 | 5 | 相同 |
| Win Rate | 1.0 | 相同 | — | — |
| Profit Factor | —（仅有 `payoff_ratio=0`） | 相同 | — | — |
| Turnover | 6,215.560749（由成交 notional 推导） | 相同 | 559,390.130297（仅执行订单 notional） | 相同 |
| Fees | 3.107780（按 5 bps 成交成本推导） | 相同 | 167.817039（`total_cost`） | 相同 |

逐层比较结果：

| 对象 | 结果 |
|---|---|
| Detector signals | 99/99；唯一原始首差是第 0 条的 `generated_at` 运行墙钟，剔除后包括 metadata 在内逐字段一致 |
| Detector trades / equity | 5×11 列交易、361 个权益点、10 项指标全部一致 |
| Multi orders | 166 行；`intent_id` 是每次运行生成的非业务 UUID，剔除后 16 个业务字段全部一致 |
| Multi trades / equity | 5×28 列交易、420 个权益点、8 项指标全部一致 |
| 首个业务语义差异 | **无** |

基线源码的策略相关测试另行执行为 `37 passed in 1.12s`。完整旧套件在
pytest session cleanup 受到本机临时目录 ACL 的 `PermissionError` 阻断，
不是断言或策略失败；因此没有将其误报为基线失败。

## 性能复验

命令：`python scripts/benchmark_refactor.py`。环境为 Python 3.12.6，
确定性 720-bar D1 内存 fixture，预热一次、7 次取中位数；测量循环内没有
磁盘或网络 I/O。

| 路径 | Before 时间 / 峰值内存 | After 时间 / 峰值内存 | 时间变化 | 内存变化 |
|---|---:|---:|---:|---:|
| Feature preparation | 64.648 ms / 219,366 B | 32.581 ms / 246,235 B | 1.984× 更快 | 12.25% 增加 |
| Detector replay | 1,020.463 ms / 1,148,116 B | 607.425 ms / 542,780 B | 1.680× 更快 | 52.72% 降低 |
| Multi-asset timeline | 160.605 ms / 913,539 B | 88.526 ms / 577,891 B | 1.814× 更快 | 36.74% 降低 |

该基准证明三个**内存热路径**的等价 before/after 改善；它不证明数据下载、
Parquet、Catalog 或 Provider 的端到端吞吐提升。

## 删除、保留与清理

已删除：

- `REDEME`：空的已跟踪误文件，无引用；
- 文件名为误粘贴 PowerShell 命令的 5 KiB 已跟踪文本：无运行/测试/文档引用；
- `logs/signals/2026-08-13.jsonl`：无引用的历史运行日志，包含过时策略版本输出；
- `MODIFICATIONS_2026-08-09.md`：无引用的一次性迁移记录，已由重构说明和本审计取代；
- 本审计创建的临时 pytest 与基线解压目录；
- `historical_data/storage.py` 的未使用 hash 导入，并将内部调用者迁移到规范的 integrity 模块。

保留：

- `historical_data.legacy`：只供显式 `market-data migrate-legacy` 数据迁移；默认读取不使用它；
- `inv-trend-trading-core-20260814.zip`：MD 明确引用的辅助审计输入；
- 用户已有 `research/**/outputs/`：不属于本次创建，已保持忽略，避免把运行结果误纳入版本控制或擅自删除用户研究结果；
- 大型 `historical_data/storage.py` / `HistoricalDataService`：有正式调用，属于后续拆分项而非 dead code。

## 检查结果

| 检查 | 实际结果 |
|---|---|
| `pytest -q --basetemp .tmp/a` | **253 passed, 0 failed, 0 skipped**，71.92 s |
| Golden Master | **1 passed**；输入、特征、daily、Detector 和 Multi hash/指标均通过 |
| 历史发布事务回归 | **14 passed**（包含本次新增 P0 故障注入） |
| `ruff check .` | **passed** |
| `python -m compileall -q ...` | **passed** |
| `git diff --check` | **passed** |
| 五个 CLI `--help` | `turtle-data`、`turtle-detect`、`turtle-alert`、`turtle-daily`、`market-data` 均为 **OK** |

Windows 上必须为 pytest 选用短工作区临时路径；较长临时路径会使 Parquet
临时文件超过 260 字符，表现为 `FileNotFoundError`，不是业务断言失败。

## 剩余问题与下一步

### P0 / 发布门槛

- 新增的正式模块、tests、fixtures、docs 当前仍是未跟踪文件。`.gitignore`
  已修复，但在 `git add` 和提交前不能称为可交付 Baseline。
- P0 Final 仍缺真实 Provider/数据根、生产规模 Catalog、独立进程崩溃恢复和
  Windows 文件系统语义的验收。

### P1 / 架构一致性

- 将 `turtle-alert` 与 `turtle-data` 的正式路径迁入 application/data port，
  并消除 `us_trend_alerts` 的共享特征和 canonical data 绕过；
- 将有效策略配置收敛到真正由 CLI 使用的 `ResolvedRunConfig`；
- 收敛多个 Turtle 突破/状态机实现，先逐事件对照再迁移；
- 决定 `DetectorScanService` / eligibility 是接入还是正式退役。

### P2 / 可维护性

- 为两个回测器统一公开绩效字段，尤其是 Sortino、费用和 turnover；
- 拆分仍偏大的 `storage.py` 和 `HistoricalDataService`；
- 在 CI 固定短 pytest 临时目录、真实 PyArrow/DuckDB、Windows 并发/恢复和
  端到端性能阈值。

