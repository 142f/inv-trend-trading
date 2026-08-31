# inv-trend-trading 架构与端到端流程指南

> 面向对象：第一次接触本仓库的开发者。  
> 阅读目标：理解“项目做什么 → 每个阶段做什么 → 输入输出是什么 → 模块如何连接 → 最终得到什么”。  
> 代码依据：`src/inv_trend/` 是唯一可发布的 Python 命名空间；本文描述当前实现，不把历史包名当作可用接口。

## 1. 项目要解决什么问题

`inv-trend-trading` 是一个面向日线趋势跟随的研究、扫描和审计系统。它把来自多个行情源的原始 OHLCV 数据治理为可追溯的数据版本，计算海龟突破与多维趋势证据，并以可恢复、可审计的流程产出：

- 已治理的历史行情数据集；
- 日线趋势筛选、候选入场或确认入场决策；
- 去重的正式通知待投递事件；
- JSON、CSV、HTML、SQLite 等可复核结果；
- 单资产扫描及多资产海龟策略回测结果。

它**不是自动下单系统**。`ENTER_LONG` / `ENTER_SHORT` 表示经过数据和资格门禁确认的正式执行决策；系统把该决策写入 outbox 并交给通知适配器，而不直接连接交易所下单。

### 1.1 一张图看全局

```text
市场 Provider / CSV / 已有数据
          │ 原始 OHLCV
          v
data：注册、标准化、质量门禁、版本化、血缘、current 指针
          │ 已完成且可追溯的 D1 DataFrame
          ├───────────────────────┐
          v                       v
application/daily              adapters/detector / adapters/multi_asset
六阶段日线工作流               单资产检测、告警扫描、研究与回测
          │                       │
          v                       v
核心特征、策略证据、趋势决策       订单、交易、权益曲线、扫描信号
          │
          v
SQLite 状态 / outbox ──> NotificationPort ──> 日志或外部通知
          │
          v
JSON（权威结果） ──> HTML / CSV / Manifest（只读派生展示）
```

系统刻意将“计算证据”“形成决策”“写入状态”“渲染或投递”拆开：展示层不能重算策略，通知层不能把普通技术信号提升为正式执行事件。

## 2. 目录结构与分层边界

```text
src/inv_trend/
├── core/                   # 纯内存领域模型、特征、规则、信号、绩效
│   └── strategy/daily/     # 单次 D1 特征准备和多维策略分析
├── data/                   # 行情身份、Provider、数据湖、质量、血缘、审核
│   └── config/assets.yaml  # 标的身份、市场语义与数据源的权威配置
├── config/strategy.yaml    # 规范策略配置的权威来源
├── application/            # 用例编排、运行清单、兼容服务
│   └── daily/              # 可恢复的六阶段日线工作流和端口协议
├── adapters/               # 把数据、SQLite、报告和现有策略实现接入端口
│   ├── daily/              # Daily 工作流组合根、血缘、制品发布
│   ├── detector/           # 单资产 Turtle 扫描、资格、SQLite、通知、回测
│   └── multi_asset/        # 多资产数据处理、风险控制、策略、回测
├── observability/          # JSON/HTML/CSV 的渲染与审计展示
├── integrations/           # 可选 MT5、OKX SDK 适配
└── cli/                    # 命令行薄入口

tests/                      # 行为、边界、Golden Master 与端到端回归
scripts/                    # 测试、基准、Golden Master 刷新等维护工具
research/                   # 非稳定研究脚本；不构成发布 API
data/                       # 运行时数据湖（非源码）
outputs/                    # 日报、回测、SQLite、通知等运行结果（非源码）
```

### 2.1 依赖方向

```text
cli ───────────────> application ───────> core
 │                        │                ▲
 │                        ├──────────────> data
 │                        └──────────────> adapters ──> core / data
 └──────────────────────> observability ─> core

integrations ────────────────────────────> adapters
```

| 层 | 应负责的事情 | 不应负责的事情 |
|---|---|---|
| `core` | 指标、特征、条件、规则、信号身份、绩效计算 | 网络、文件、SQLite、CLI、报告渲染 |
| `data` | 标的注册、Provider 调用、标准化、质量和血缘、数据版本 | 编排日线流程、决定交易信号 |
| `application` | 用例编排、阶段合同、运行上下文、决策投影 | 绑定某个数据库或 HTML 实现 |
| `adapters` | SQLite、文件系统、通知、历史策略实现的具体连接 | 成为新的通用业务规则权威 |
| `observability` | 读取权威 JSON 并渲染 HTML/CSV/审计信息 | 再次读取 K 线并重新判断策略 |
| `cli` | 参数解析、调用应用服务、输出摘要与退出码 | 承载策略规则或数据治理逻辑 |

`adapters/detector` 与 `adapters/multi_asset` 含有仍在收敛的既有 Turtle 实现，因此是显式过渡边界。新增可复用的纯计算优先进入 `core`，新增业务流程优先进入 `application`。

## 3. 统一输入输出与接口规范

模块交接时应优先使用下面四类契约；不要用临时字典或展示对象替代它们。

### 3.1 身份、时间和配置

每个可追溯对象都应携带或能追溯到以下身份维度：

| 字段 | 含义与标准 |
|---|---|
| `symbol` | 面向用户的标的代码，例如 `BTC`、`AAPL`。 |
| `instrument_id` | 唯一且稳定的市场标的身份，例如 `BTCUSDT.BINANCE.SPOT`。 |
| `timeframe` | 周期；日线工作流固定为 `D1`。 |
| `dataset_version` | 已发布、不可变数据集版本；不能在阶段之间换成新的 `current`。 |
| `as_of` / `signal_time` | 产生证据或决策的完成 bar 时间，ISO 8601 字符串。 |
| `run_id` / `report_date` | 一次工作流尝试及其报告日期；它们属于编排身份，不属于策略业务身份。 |
| `strategy_version` | 可执行策略版本；当前正式版本为 `corrected-v2`。 |

新策略参数写入 `src/inv_trend/config/strategy.yaml`。标的身份、交易日历语义、首选/备用数据源写入 `src/inv_trend/data/config/assets.yaml`。应用、检测器和多资产目录中的兼容 YAML 只用于显式旧路径读取，不应作为新功能的默认配置来源。

### 3.2 OHLCV 数据契约

数据层向策略层交付的是 `pandas.DataFrame`。完成 D1 数据至少应具备：

```text
timestamp  : UTC 时间戳
open/high/low/close : 数值价格
volume     : 数值成交量（市场不提供时按数据源规则处理）
is_complete: 是否为已完成 bar
```

数据层还会附带或保留 `dataset_version`、`quality_status` 等治理元数据。策略计算只使用已完成 bar：未完成的当天 K 线不能产生正式信号。进入正式链路的数据必须是 `CURATED`，且通过新鲜度和血缘校验。

### 3.3 阶段结果 JSON 契约

日线阶段使用不可变 dataclass 表达业务结果，再通过 `to_dict()` 写成 UTF-8 JSON。每份阶段结果均包含：

```text
stage + schema_version + symbol + instrument_id + timeframe
业务输入 Hash（如 input_data_hash）
result_id（result_hash 前 24 位）+ result_hash（SHA-256）
```

Hash 基于规范化 JSON：键排序、UTF-8、禁止 NaN。运行 ID、重试时间等不稳定编排字段不进入业务结果 Hash；它们由运行上下文与提交投影 Hash 单独绑定。后续阶段必须验证前置 Hash、标的身份、周期与数据版本，不能隐式重算或换用当前数据。

### 3.4 端口而非具体基础设施

`application/daily/ports.py` 用 `typing.Protocol` 定义应用真正需要的能力。主要端口如下：

| Port | 调用方需要的能力 | 当前实现举例 |
|---|---|---|
| `MarketDataPort` | 查标的、更新数据、读取固定版本或 current 数据 | `HistoricalDataService` |
| `LineagePort` / `FreshnessPort` | 验证版本血缘、评估 D1 新鲜度 | `CurrentLineageAdapter`、`FreshnessPolicy` |
| `DailyStateRepositoryPort` | 事务性写信号、cursor、run、outbox | `SQLiteDailySignalRepository` |
| `EligibilityPort` | 对候选突破执行风险/资格检查 | `TradeEligibilityChecker` |
| `ArtifactPublisherPort` | 原子发布权威 JSON 及派生制品 | `DailyRunArtifactWriter` |
| `NotificationPort` | 投递一个已提交的正式事件 | `LogNotifier` 或其他通知器 |

因此 `DailyWorkflow` 不直接依赖某个 SQLite、数据湖或 HTML 类。新实现只要满足相同 Protocol，就可替换适配器；不要在工作流中反向导入具体基础设施。

## 4. 从原始数据到最终结果：主流程

```text
1. assets.yaml + strategy.yaml
          │
2. Provider 获取原始 OHLCV
          │
3. 标准化、质量门禁、版本化、血缘与 current 发布
          │
4. D1 data-update 固定本次数据/配置上下文
          │
5. strategy-screen 一次准备全部特征，生成技术证据和突破候选
          │
6. trend-decide 消费已有证据，生成候选或确认的执行决策
          │
7. commit 原子写入 SQLite：事件、cursor、outbox、回执
          │
8. publish 仅从已完成 JSON 生成权威目录、HTML、CSV、Manifest
          │
9. deliver 只消费当前 run 的 outbox，写投递回执
```

### 4.1 数据治理：第 1～3 步

核心入口是 `data/api.py` 中的 `HistoricalDataService`。它协同以下组件完成数据流：

```text
InstrumentRegistry / assets.yaml
        → ProviderFactory / Provider
        → 原始响应与规范 OHLCV
        → normalizer、calendar、quality/integrity
        → DataLake（Parquet、Manifest、Lineage、Catalog）
        → 审核 / current 原子指针
```

数据湖的原则是“先写新版本并校验，再发布 current”。刷新失败不会覆盖已有的可用版本。调用方通过 `load_bars_version()` 读取已固定版本，通过 `load_bars()` 读取 current；日线阶段交接使用前者的版本身份来避免 mutable current 漂移。

### 4.2 D1 六阶段工作流

入口为 `turtle-daily run`，也可按顺序执行独立阶段：

```text
data-update → strategy-screen → trend-decide → commit → publish → deliver
```

同一次运行必须复用相同的 `--output-dir`、`--report-date` 和 `--run-id`。staging 工作区为：

```text
<output-dir>/runs/<YYYY-MM-DD>/.staging/<run-id>/
```

以下表格是新开发者最应熟悉的交接契约。

| 阶段 / 核心类 | 输入 | 处理与职责 | 主输出 | 外部副作用 |
|---|---|---|---|---|
| `data-update` / `DailyDataUpdateService` | 选中标的、`started_at`、`bootstrap_days`、数据端口 | 初次 ingest 或增量 update；读取完成 D1；验证质量、新鲜度、血缘；固定本次配置 | `DataUpdateResult` | 可能发布新的数据集版本；写 `run_context.json` 和阶段 JSON |
| `strategy-screen` / `StrategyScreeningService` | `DataUpdateStage`、资产、固定配置、可选 `ExecutionContext` | 一次构建 D1 特征；分析海龟、SMA、EMA、MACD、DMI/ADX、ATR、成交量；生成规则、证据与突破候选 | `StrategyScreeningResult` | 只写阶段 JSON；不写信号、cursor 或 outbox |
| `trend-decide` / `TrendDecisionService` | 已 Hash 绑定的筛选结果 | 只消费筛选证据；判断趋势、风险阻断、候选或确认入场；构建提交投影 | `TrendDecisionResult` | 只写阶段 JSON；绝不重读 K 线或重算指标 |
| `commit` / `DailySignalCommitService` | 三个阶段结果、运行上下文、SQLite port | 验证 Hash 链与提交投影；投影技术事件与执行决策；事务提交 cursor/outbox | `CommitReceipt` 与提交统计 | **唯一**写入 SQLite 信号、cursor、run 与 outbox 的阶段 |
| `publish` / `DailyArtifactPublicationService` | 完整阶段 JSON、提交回执、发布 port | 校验链路；组装完整分析；由权威 JSON 派生展示 | `DailyRunArtifactPublication` | 原子写 JSON、HTML、CSV、审计 Manifest、`latest` 快捷入口 |
| `deliver` / `DailyNotificationDeliveryService` | 本 run 可重试 outbox、通知 port | 逐项投递；成功/失败均记录回执，可重试 | `DeliveryReceipt` | 更新 outbox 状态与投递回执；不重写发布制品 |

#### 阶段 1：`DataUpdateResult`

该结果回答“本标的的数据是否可用于正式策略”。关键字段为：

```json
{
  "stage": "data_update",
  "symbol": "BTC",
  "instrument_id": "BTCUSDT.BINANCE.SPOT",
  "timeframe": "D1",
  "dataset_version": "…",
  "latest_complete_d1": "2026-08-26T00:00:00+00:00",
  "update": {"status": "updated|unchanged|failed", "provider": "…"},
  "quality": {"statuses": ["CURATED"], "passed": true},
  "freshness": {"status": "FRESH", "expected_date": "…"},
  "lineage": {"verified": true, "curated_sha256": "…"},
  "data_readiness": "READY",
  "blocking_reasons": [],
  "result_hash": "…"
}
```

`data_readiness=READY` 的前提是：有可读完整 D1、质量为 `CURATED`、新鲜度为 `FRESH`、血缘已验证。数据刷新失败但已有可验证的 current 版本时，结果会显式记录刷新失败，而不会悄悄改写旧版本。

#### 阶段 2：`StrategyScreeningResult`

该结果回答“最新完成 bar 上看到了什么技术事实”。它包含：

```text
input_data_hash / configuration_hash / dataset_version / as_of
strategy_checks     多维趋势检查结果
conditions          条件的实际值、阈值、通过状态
rules               可解释规则评价
raw_events          SMA、EMA、MACD、Turtle、评级等技术证据
turtle_breakouts    20/55 日同方向突破候选
eligibility         NOT_APPLICABLE / NOT_EVALUATED / PASSED / …
event_snapshots     回放时各候选位置的快照
commit_evidence     供提交和报告使用的 JSON 证据
```

`PreparedDailyAnalysis`（`core/strategy/daily/analysis.py`）负责把所需 `FeatureRequest` 合并，调用 `PreparedBars.build()` **只准备一次**指标，再让信号、策略检查、规则解释和回放共享同一份 DataFrame。这既避免重复计算，也防止同一 bar 的报告与决策使用不同指标值。

#### 阶段 3：`TrendDecisionResult`

该结果回答“这些技术事实意味着什么动作”。其业务输入只来自 `StrategyScreeningResult`，不读取 K 线：

```text
trend_direction      LONG / SHORT / NEUTRAL
decision             LONG / SHORT / WAIT / …
execution_state      WAIT / ENTRY_CANDIDATE_* / ENTER_* / RISK_BLOCKED / …
reason_code          稳定、机器可读的原因码
eligibility_status   资格门禁状态
long/short/reverse_evidence、risk_blocks、confidence
event_decisions      可解释的事件决策
commit_projection    已绑定 Hash 的持久化计划
```

三类对象必须区分：

```text
技术证据事件（SMA / EMA / MACD / Turtle / STRATEGY_GRADE_A_*）
                         │
                         v
TrendDecisionResult（WAIT、ENTRY_CANDIDATE_*、ENTER_*）
                         │ 仅确认 ENTER_*
                         v
ExecutionDecisionEvent（ENTRY_DECISION_LONG / ENTRY_DECISION_SHORT）
```

正式入场同时需要：A 级同向趋势、同一完成 D1 bar 的同向 20/55 Turtle 突破、已实际执行且通过的资格门禁，以及非研究模式的质量/新鲜度/血缘通过。资格未执行或未通过时，只能得到 `ENTRY_CANDIDATE_*`，不能创建正式通知。

#### 阶段 4：提交与幂等性

`DailySignalCommitService` 调用 `DailyStateRepositoryPort.commit_*`，当前适配器为 `SQLiteDailySignalRepository`。它在一个事务中提交事件、cursor、运行记录与 outbox，并输出包含新插入数、重复数、已选择通知事件等信息的回执。

`ExecutionDecisionEvent` 位于 `core/decision_events.py`。其业务 ID 由以下稳定维度生成：

```text
strategy_version + decision_rule_version + instrument_id
+ timeframe + as_of + action
```

它不包含 `run_id` 或重试时间。因此重复执行同一决策会得到同一个 `event_id`；SQLite 的去重与 outbox 共同确保不会重复通知。为兼容原有存储表，执行决策会投影为 `SignalEvent`，其 `indicator_name` 为 `execution_decision`。

#### 阶段 5：发布目录

`publish` 成功后，权威制品位于：

```text
<output-dir>/runs/<YYYY-MM-DD>/<run-id>/<symbol>/
├── 01_canonical/
│   ├── data_update_result.json
│   ├── strategy_screening_result.json
│   ├── trend_decision_result.json
│   └── complete_analysis_result.json
├── 02_report/             # 由 canonical JSON 渲染的 HTML
├── 03_exports/            # CSV
└── 04_audit/              # manifest、配置/血缘快照、SHA-256
```

`01_canonical` 是策略和审计的权威。HTML、CSV、`latest/<symbol>/` 和日期化兼容副本均为派生视图；若展示层有问题，应修复渲染而不是篡改权威 JSON。

#### 阶段 6：投递

`deliver` 只读取当前 `run_id` 的 pending/failed outbox 记录。成功后标记已投递，失败后保存错误以便重试。重跑 `deliver` 不会重算策略、不推进 cursor，也不修改已发布的 JSON/HTML/CSV。

## 5. 推荐执行命令与结果说明

本节给出可直接复制的命令。正式日线流程优先使用 `turtle-daily`；`market-data` 只负责数据治理，`turtle-detect`、`turtle-alert` 和 `turtle-data` 分别服务于单资产检测、批量突破告警和研究/回测。

### 5.1 先查看命令帮助

在首次使用或升级版本后，先检查实际可用参数：

```powershell
market-data --help
turtle-daily --help
turtle-detect --help
turtle-alert --help
turtle-data --help
```

项目在 `pyproject.toml` 中注册上述命令。未安装为命令行脚本时，也可使用等价模块入口，例如：

```powershell
.\.venv\Scripts\python.exe -m inv_trend.cli.daily --help
```

### 5.2 数据治理命令：先得到可读、可追溯的 D1 数据

首次为一个标的建立数据版本：

```powershell
market-data --root data download `
  --symbol BTC --timeframe D1 `
  --start 2017-08-17T00:00:00Z
```

后续日常更新和验收建议按以下顺序执行：

```powershell
market-data --root data update --symbol BTC --timeframe D1
market-data --root data coverage --symbol BTC --timeframe D1
market-data --root data verify --kind catalog
```

| 命令 | 产生或检查的结果 | 如何使用结果 |
|---|---|---|
| `download` | 原始响应、标准化/质量结果、不可变数据集版本、Manifest 与血缘记录 | 第一次建立可供策略读取的 D1 历史数据。 |
| `update` | 新版本或“未变化”的 current 数据集；失败时保留旧 current | 每日扫描前刷新数据；不要直接覆盖旧文件。 |
| `coverage` | 指定标的/周期的覆盖范围、缺口与可用性摘要 | 判断预热区间和策略回放是否足够。 |
| `verify --kind catalog` | Catalog、Manifest、指针关系的校验结果 | 发布或排障前确认数据湖未漂移。 |

这些命令的持久化结果在 `data/` 数据湖中，而非普通 CSV 覆盖文件。后续正式扫描只应消费其中已发布、`CURATED` 且血缘可验证的完成 bar。

### 5.3 推荐方式 A：一键完成 D1 日报

适合本地手工执行、一次性调度任务或不需要在阶段之间暂停的场景：

```powershell
turtle-daily run `
  --symbol BTC `
  --data-root data `
  --output-dir outputs/daily_market_scan `
  --no-color
```

省略 `run` 的旧调用方式仍等价：

```powershell
turtle-daily --symbol BTC --no-color
```

`run` 依次执行 `data-update → strategy-screen → trend-decide → commit → publish → deliver`。终端会输出快照、SQLite SignalStore、信号日志和（未使用 `--no-html` 时）HTML 报告的路径；进程退出码代表整体运行结果。

成功后主要得到：

```text
outputs/daily_market_scan/
├── <YYYY-MM-DD>.json / <YYYY-MM-DD>.html     # 日期化兼容快照
├── latest/<symbol>/                           # 最近一次成功运行的快捷视图
├── runs/<YYYY-MM-DD>/<run-id>/<symbol>/       # 本次不可变权威结果
└── state/signals.sqlite3                      # 信号、cursor、outbox、运行状态

logs/signals/                                  # 已投递事件的日志
```

若只希望检查已有本地数据而不访问 Provider，可使用 `--scan-only`；该模式仍会检查当前数据的新鲜度和治理状态。`--research-mode` 只保留观察性结果，永远不会产生正式通知；`--backfill-signals` 用于研究回放，新增发现也不会通知。

### 5.4 推荐方式 B：分阶段执行、审阅与恢复

适合生产调度、人工审核、故障恢复或希望保存每一步机器可读输出的场景。先固定三个运行身份；所有后续命令必须完全复用它们：

```powershell
$runId = 'd1-20260827-001'
$reportDate = '2026-08-27'
$outputDir = 'outputs/daily_market_scan'
```

按以下顺序执行：

```powershell
turtle-daily data-update `
  --run-id $runId --report-date $reportDate --output-dir $outputDir `
  --data-root data --symbol BTC --bootstrap-days 365

turtle-daily strategy-screen `
  --run-id $runId --report-date $reportDate --output-dir $outputDir

turtle-daily trend-decide `
  --run-id $runId --report-date $reportDate --output-dir $outputDir

turtle-daily commit `
  --run-id $runId --report-date $reportDate --output-dir $outputDir

turtle-daily publish `
  --run-id $runId --report-date $reportDate --output-dir $outputDir

turtle-daily deliver `
  --run-id $runId --report-date $reportDate --output-dir $outputDir
```

每个显式阶段都会向标准输出写一个排序后的 JSON 摘要，便于 Scheduler 采集。阶段详细结果保存在：

```text
<outputDir>/runs/<reportDate>/.staging/<runId>/
```

| 命令 | 立即可见的结果 | 持久化结果 | 何时继续 / 重试 |
|---|---|---|---|
| `data-update` | 每标的数据就绪摘要与可能的 `gate_failures` | `run_context.json`、每标 `DataUpdateResult` | 只有数据就绪为 `READY` 才进入正式后续阶段；修复数据后以同一身份重试。 |
| `strategy-screen` | 已产生的条件、证据、候选和筛选状态摘要 | 每标 `StrategyScreeningResult` | 审阅 `raw_events`、`turtle_breakouts`、`eligibility`、`result_hash`；本阶段不写 SQLite。 |
| `trend-decide` | 趋势方向、执行状态、原因码摘要 | 每标 `TrendDecisionResult` | `WAIT` 是正常结果；`ENTRY_CANDIDATE_*` 表示证据存在但尚不可正式执行。 |
| `commit` | 新插入/重复事件、cursor、outbox 的提交摘要 | SQLite 事务结果与 `commit_receipt.json` | 这是唯一写状态阶段；提交失败时先定位 Hash 或数据库问题，不能跳过它直接发布。 |
| `publish` | `run_directory`、兼容 JSON/HTML 的路径 | canonical JSON、HTML、CSV、审计 Manifest、`latest` | 可用同一 `runId` 恢复发布后的非原子收尾；新一次分析必须使用新 `runId`。 |
| `deliver` | 成功、失败、待重试数量 | outbox 状态与 `delivery_receipt.json` | 网络或通知错误时，用相同参数重复执行 `deliver`；不会重算或重复发布。 |

阶段异常的处理规则：

- `data-update`、`strategy-screen`、`trend-decide` 在正式模式发现数据/筛选门禁失败时返回非零退出码；研究模式的观察结果不视为正式失败。
- `WAIT` 不代表流程失败，只代表当前没有需要执行的趋势动作。
- `deliver` 只要仍有投递错误即返回非零退出码；应只重试 `deliver`，而非重跑整个策略链。
- 阶段 JSON、数据版本、身份或 Hash 链不匹配时会失败关闭。不要手工修改 staging 文件来“修复”流程。

### 5.5 从发布结果中读取什么

一次 `publish` 后，先读 canonical JSON，再读展示文件：

| 位置 | 推荐用途 | 关键内容 |
|---|---|---|
| `01_canonical/data_update_result.json` | 判断数据是否可信 | `dataset_version`、质量、新鲜度、血缘、阻断原因。 |
| `01_canonical/strategy_screening_result.json` | 审阅技术证据 | 条件、规则、原始信号、Turtle 突破、资格结果。 |
| `01_canonical/trend_decision_result.json` | 审阅最终策略结论 | 趋势、`execution_state`、原因码、风险阻断、执行投影。 |
| `01_canonical/complete_analysis_result.json` | 程序集成和审计的首选入口 | 整合三阶段结果、序列、信号、解释、评估和摘要。 |
| `02_report/` | 人工阅读 | 仅由 canonical JSON 渲染的 HTML。 |
| `03_exports/` | 表格分析或外部导入 | CSV 导出。 |
| `04_audit/` | 复核与追踪 | Manifest、SHA-256、配置快照、数据血缘快照。 |

最重要的判断是：`ENTRY_CANDIDATE_*` 不会进入 outbox；只有 `ENTER_*` 投影出的 `ENTRY_DECISION_LONG` 或 `ENTRY_DECISION_SHORT` 才可能出现在 SQLite/outbox 与投递记录中。

### 5.6 单资产检测、批量告警与研究回测

**单资产检测**：使用已治理数据时不传 `--bars`；仅兼容/测试 CSV 才传入 `--bars`。

```powershell
turtle-detect `
  --symbol BTC --timeframe D1 --data-root data `
  --state outputs/turtle_detector/state.json `
  --alerts outputs/turtle_detector/signals.jsonl
```

结果是 JSON 状态仓储中的 cursor、仓位和已提交信号；只有检测到新的可执行状态转换时，才会追加 JSONL 通知记录。该命令适合单标的事件扫描，不替代六阶段日线审计流程。

**批量 D1 突破告警**：扫描配置标的，或扫描 QQQ/SPY 前 50 持仓。

```powershell
turtle-alert --symbol BTC --no-color

turtle-alert --universe qqq-spy-top50 `
  --refresh-official-holdings --refresh-before-scan `
  --max-cycles 1 --no-color
```

结果写入 `outputs/turtle_alerts/`：去重状态、`alerts.jsonl`、扫描报告与控制台状态表。`--watch --interval-seconds 300` 可用于持续轮询；仅在已完成 D1 bar 上判定突破。

**多资产数据构建与回测**：适合研究和结果复现。

```powershell
turtle-data build --output-dir processed_data

turtle-data build-metal-tech-core `
  --processed-dir processed_data `
  --output-dir processed_data `
  --reports-dir outputs

turtle-data us-trend-alerts --output-dir outputs/us_trend_alerts
```

| 命令 | 主要结果 |
|---|---|
| `turtle-data build` | 统一后的 `processed_data` 数据集及构建摘要。 |
| `build-metal-tech-core` | 金属/科技核心数据集、回测输入、订单/交易/权益/指标和可选 HTML 报告。 |
| `us-trend-alerts` | SPY/QQQ 成分股的 20/55 日突破扫描结果与报告。 |

研究与回测输出可以用来比较策略和风险表现，但不能绕过日线工作流的质量、资格和正式 outbox 门禁。

## 6. 另外两条业务路径

### 6.1 单资产 Turtle 检测

适用于 `turtle-detect` 或以事件为中心的扫描。主链如下：

```text
OHLCV DataFrame + AssetConfig + StrategyConfig
      → TurtleScanner.prepare / CandidateDetector
      → 状态机（仓位、加仓、退出、确认）
      → DetectionDecision / TurtleSignal
      → SignalRepository
      → cursor、仓位、信号、outbox
```

核心文件：

| 文件 / 类 | 职责 |
|---|---|
| `adapters/detector/models.py`：`AssetConfig`、`StrategyConfig`、`TurtleSignal` | 单资产策略的输入和输出模型。 |
| `engine/scanner.py`：`TurtleScanner` | 准备指标并发现突破/趋势条件。 |
| `engine/candidate_detector.py`：`CandidateDetector` | 结合 cursor、仓位状态和扫描结果生成决策。 |
| `engine/state_machine.py` | 进入、确认、加仓、平仓等状态转换。 |
| `eligibility/checker.py`：`TradeEligibilityChecker` | 流动性、波动、风险预算、市场状态等资格检查。 |
| `storage/signal_repository.py` | 内存或 JSON 的原子状态仓储。 |
| `storage/daily_signal_repository.py` | 日线工作流使用的 SQLite 事务仓储。 |

### 6.2 多资产回测与研究

适用于研究和可复现绩效评估，主入口是 `application.backtest.BacktestBatchService`；
`TurtleBacktester` 仅作为 adapters 层低级成交执行器：

```text
多个 OHLCV DataFrame
  → multi_asset.data：清洗、字段统一、校验、时间对齐
  → AssetSpec + TurtleRules + BacktestConfig
  → StrategySignalAdapter（预计算策略证据）
  → UnifiedBacktestExecutor / TurtleBacktester
  → BacktestBatchResult v2 + execution_result_hash
  → BacktestReportModel v1
  → 统一 JSON / CSV / Parquet / HTML 回测制品
```

`AssetSpec` 描述可交易属性和精度，`TurtleRules` 描述海龟规则，`BacktestConfig` 描述初始权益、费用、报告等运行参数。风险预算、挂单过期、成交保护和仓位 sizing 位于 `adapters/multi_asset/risk` 与 `strategy` 中。回测输出不能替代日线正式通知：两者是不同用例。

## 7. 核心文件与类速查

| 位置 | 首选阅读对象 | 负责什么 |
|---|---|---|
| `core/features.py` | `FeatureRequest`、`PreparedBars` | 合并指标请求，构造可共享的特征 DataFrame。 |
| `core/strategy/daily/analysis.py` | `prepare_daily_analysis()`、`analyze_prepared_daily_analysis()` | 在一次特征准备后生成 D1 信号、检查与规则解释。 |
| `core/signals.py` | `SignalEvent` | 技术/存储信号的稳定身份与 JSON 结构。 |
| `core/decision_events.py` | `ExecutionDecisionEvent` | 正式执行决策的稳定业务 ID 及其 `SignalEvent` 投影。 |
| `data/api.py` | `HistoricalDataService` | 行情 ingest、update、load、审核与版本读取的应用入口。 |
| `data/storage.py` | `DataLake` | 不可变文件写入、Manifest、Catalog 与 current 指针。 |
| `application/daily/workflow.py` | `DailyWorkflow` | 协调六阶段；验证身份、Hash 与阶段顺序。 |
| `application/daily/data_update.py` | `DailyDataUpdateService` | 形成可审计的数据就绪结论。 |
| `application/daily/strategy_screening.py` | `StrategyScreeningService` | 形成一次性特征计算后的完整策略证据。 |
| `application/daily/trend_decision.py` | `TrendDecisionService` | 把证据收敛为趋势与执行状态。 |
| `application/daily/signal_commit.py` | `DailySignalCommitService` | 唯一允许持久化信号与 outbox 的阶段服务。 |
| `application/daily/ports.py` | 所有 `*Port` Protocol | 应用与具体基础设施之间的稳定接口。 |
| `adapters/daily/composition.py` | 组合工厂 | 将报告渲染和文件发布等具体实现接入工作流。 |
| `adapters/daily/artifact_publisher.py` | `DailyRunArtifactWriter` | 原子创建 canonical、HTML、CSV 和审计目录。 |
| `adapters/detector/storage/daily_signal_repository.py` | `SQLiteDailySignalRepository` | 信号、cursor、run、outbox 的事务和去重。 |
| `application/manifest.py` | `StrategyRunManifest` | 记录回测/运行的代码、数据和配置指纹。 |
| `cli/*.py` | `main()` | 稳定命令行边界，保持薄而可测试。 |

## 8. 开发和扩展时的工作方式

1. **新增指标或纯规则**：放入 `core`，通过 `FeatureRequest` 和纯函数暴露；不要在 HTML 或 CLI 中计算。
2. **新增阶段业务逻辑**：放入 `application/daily`，定义明确输入结果、输出结果及 Hash 关系；不要让后置阶段隐式重算前置数据。
3. **新增外部系统**：先定义或复用 `Protocol`，再在 `adapters` 或 `integrations` 实现；以组合根注入工作流。
4. **新增数据源**：先补充标的/Provider 语义、标准化、质量和血缘，再让策略读取；不能绕过 DataLake 直接把外部 K 线送入正式日线通知。
5. **新增报告字段**：优先增加到 canonical JSON，再让 HTML/CSV 渲染器消费；渲染器不能变成第二个策略实现。
6. **变更决策语义**：补充 `tests/test_daily_execution_decision_events.py`、Golden Master 或等价回归，记录首个业务差异；不要静默刷新基线。

## 9. 新开发者建议的阅读与运行顺序

1. 阅读本文，再阅读 [ARCHITECTURE.md](ARCHITECTURE.md) 与 [DAILY_WORKFLOW_STAGES.md](operations/DAILY_WORKFLOW_STAGES.md)。
2. 查看 `src/inv_trend/config/strategy.yaml` 和 `src/inv_trend/data/config/assets.yaml`，理解策略与标的配置如何分工。
3. 从 `src/inv_trend/cli/daily.py` 跟到 `application/daily/workflow.py`，按六阶段理解控制流。
4. 阅读 `daily_models.py`、`ports.py`、`core/strategy/daily/analysis.py`，掌握交接格式和纯计算边界。
5. 阅读 `adapters/daily` 与 `adapters/detector/storage/daily_signal_repository.py`，理解数据湖、SQLite 和制品如何接入。
6. 运行测试验证理解：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_golden_master.py
.\.venv\Scripts\python.exe -m pytest -q tests\test_daily_execution_decision_events.py
```

Golden Master 固定输入、特征、策略信号、交易、订单、权益和指标的行为；执行决策测试固定“只有确认后的执行事件才能进入正式 outbox”的安全边界。

## 10. 最终总结

这个项目把“市场数据是否可靠”“趋势是否成立”“是否可正式执行”“如何留下可复核证据”拆为清晰的责任边界：

```text
可信数据
  → 可解释技术证据
  → 受资格门禁保护的趋势/执行决策
  → 幂等持久化与可重试通知
  → 不可变审计结果与派生报告
```

最终，开发者和使用者可以得到两类结果：一类是研究与回测的订单、交易、权益曲线和绩效；另一类是生产式 D1 扫描的 canonical JSON、正式执行决策、通知状态与 HTML/CSV 审计报告。它们共享数据治理和核心计算原则，但不会互相绕过各自的边界。
