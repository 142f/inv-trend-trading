# inv-trend-trading 核心代码架构、模块设计与数据流

生成日期：2026-08-23  
修订版本：P0 执行决策事件修复版

打包基线：`inv-trend-trading-core-architecture-20260823.zip`

## 1. 交付范围

压缩包包含可安装、可审阅的核心交付物：

- `src/inv_trend/`：唯一发布型 Python 命名空间，包含业务内核、数据治理、应用用例、适配器、可观测性和 CLI；
- `pyproject.toml`：Python 版本、依赖、构建配置和命令行入口；
- `requirements-okx.txt`：OKX 可选集成依赖；
- `README.md`：安装与使用入口；
- 本文件：架构、数据流、模块职责与边界说明。

压缩包不包含 `.env`、密钥、虚拟环境、Git 元数据、`__pycache__`/`.pyc`、本机缓存、日志、运行数据湖、回测输出和历史交付物。运行所需行情应在目标环境按数据治理流程获取或挂载。

## 2. 总体分层

```text
CLI / Scheduler
        |
        v
Application（用例编排、运行合同、阶段工作流）
  |           |                    \
  v           v                     v
Data        Core                 Adapters
数据获取治理  无 I/O 策略内核        存储、外部服务和既有策略实现
  |           |                     |
  +-----------+---------------------+
              |
              v
Provider / Parquet / SQLite / 文件系统 / 通知服务 / JSON、HTML、CSV
```

依赖方向为：`cli -> application -> core/data`，而 `adapters` 负责把外部实现接到端口上。 `core` 不依赖网络、文件、数据库、CLI 或应用层；`data` 不依赖应用层和 Turtle 策略适配器；`observability` 只渲染、记录和导出，不定义交易规则。

## 3. 代码模块设计

| 目录 | 职责 | 主要内容 |
|---|---|---|
| `core/` | 无副作用的领域与计算内核 | OHLCV 特征、数学指标、技术证据事件、`ExecutionDecisionEvent`、指纹、绩效统计、日线策略分析、规则评价、状态迁移 |
| `core/strategy/` | 规范化策略语义 | 日线信号构造、准备后的策略分析、异常检测、趋势/交易决策；不直接访问外部状态 |
| `data/` | 历史行情治理 | 标的注册、Provider 工厂、下载、标准化、质量门禁、血缘、不可变版本、Catalog、审核和 `current` 激活 |
| `config/` | 规范配置 | 版本化 `strategy.yaml`，统一 Turtle、日线筛选、趋势决策和风险参数；历史配置仍经兼容层读取 |
| `application/` | 用例编排 | 回测、单次检测、检测扫描、日线市场扫描、运行 Manifest 和结果模型 |
| `application/daily/` | 可恢复日线工作流 | 阶段合同、工作区、端口、数据更新、策略筛选、趋势决策、信号提交、制品发布、通知投递 |
| `adapters/daily/` | 日线工作流基础设施 | 组合根、当前数据血缘适配、阶段运行适配、文件制品发布 |
| `adapters/detector/` | 单资产 Turtle 适配 | 候选检测、状态机、指标、资格筛选、止损与仓位、SQLite 信号仓储、通知和回测 |
| `adapters/multi_asset/` | 多资产研究和回测适配 | 数据清洗/对齐、资产配置、回测引擎、预算/仓位/风险控制、美股趋势预警 |
| `integrations/` | 可选第三方 SDK | MT5、OKX；惰性导入，不安装可选 SDK 时核心包仍可导入 |
| `observability/` | 展示和审计输出 | 每日结果、回测和审计的 JSON/HTML/CSV 渲染 |
| `cli/` | 稳定命令边界 | `market-data`、`turtle-data`、`turtle-detect`、`turtle-alert`、`turtle-daily` |

## 4. 历史行情数据流

```text
CLI / 定时任务 + assets.yaml
  -> InstrumentRegistry（标的身份、市场语义）
  -> ProviderFactory（Yahoo、Binance、CSV、Dukascopy 等）
  -> 原始 OHLCV
  -> normalize_bars / assess_quality（字段、时区、重复、缺口、异常校验）
  -> DataLake 不可变版本 + Manifest + Lineage
  -> ReviewApprovalCoordinator 审核、Catalog 登记
  -> CAS 原子切换 current
  -> HistoricalDataService.load_bars 读取已完成数据
```

发布采用“先生成并验证不可变制品，再登记审核与 Catalog，最后切换 `current`”的流程。失败不会覆盖已发布版本；数据版本、质量和血缘在后续扫描中继续传递。

## 5. 日线扫描数据流与阶段边界

`turtle-daily` 支持一键 `run`，也支持以下可独立恢复的显式阶段：

```text
data-update -> strategy-screen -> trend-decide -> commit -> publish -> deliver
```

| 阶段 | 输入 | 输出 / 副作用 | 设计约束 |
|---|---|---|---|
| `data-update` | 标的、数据根目录、运行配置 | 固定的数据版本、质量、新鲜度、血缘和 `run_context.json` | 仅更新/读取数据；固定本次运行的配置和数据快照 |
| `strategy-screen` | 已固定的运行上下文和 D1 数据 | 一次特征准备后的指标、规则评价、原始事件和突破候选 | 不写信号状态、不推进 cursor、不创建 outbox |
| `trend-decide` | 筛选证据 | 趋势结论、原因码、`ENTRY_CANDIDATE_*` 或确认后的 `ENTER_*` | 只消费证据，不重新读取 K 线或计算指标；资格未确认时 fail closed |
| `commit` | 三个阶段结果与 Hash 链 | 技术证据、执行决策事件、cursor、outbox 和提交回执 | 唯一写入阶段；outbox 只接受确认后的执行决策事件 |
| `publish` | 完整阶段 JSON、Hash 链和提交回执 | 不可变 JSON、HTML、CSV、审计 Manifest、`latest` 快捷入口 | 只从已完成 JSON 派生展示制品 |
| `deliver` | 本次运行可重试 outbox | 通知投递和投递回执 | 可安全重试；不修改已发布制品 |

阶段中间结果保存在：

```text
<output-dir>/runs/YYYY-MM-DD/.staging/<run-id>/
```

成功发布后，权威结果位于：

```text
<output-dir>/runs/YYYY-MM-DD/<run-id>/<symbol>/
  01_canonical/  # 各阶段结果与完整分析 JSON
  02_report/     # 只读派生的 HTML
  03_exports/    # CSV
  04_audit/      # Manifest、配置/血缘快照与 SHA-256
```

后续阶段必须复用同一个 `output-dir`、`report-date` 和 `run-id`。若发现阶段文件被改动、Hash 不连续、数据版本不匹配或回执缺失，工作流会失败而不是隐式重算或推进状态。

### 5.1 证据、候选与正式执行事件

日线链路现在明确区分三类对象：

```text
技术证据事件
  SMA / EMA / MACD / Turtle / STRATEGY_GRADE_A_*
          ↓
TrendDecisionResult
  WAIT / NEUTRAL / ENTRY_CANDIDATE_* / ENTER_*
          ↓ 仅 ENTER_LONG / ENTER_SHORT
ExecutionDecisionEvent
  ENTRY_DECISION_LONG / ENTRY_DECISION_SHORT
          ↓
commit → signal_outbox → deliver
```

正式入场必须同时满足：

1. A 级同方向趋势；
2. 同一完成 D1 bar 上存在同方向 20/55 日 Turtle 突破；
3. eligibility 已实际执行，且 `status=PASSED`、`evaluated=true`、`passed=true`；
4. 非研究模式，数据质量、新鲜度与血缘门禁通过。

资格闸门未执行或未确认时输出 `ENTRY_CANDIDATE_LONG/SHORT`，保留趋势和突破证据，但不生成 `ExecutionDecisionEvent`，也不创建正式通知。A 级评级事件本身永远不能进入正式 outbox。

`ExecutionDecisionEvent.event_id` 由策略版本、决策规则版本、标的、周期、决策 bar 和动作生成，不依赖 `run_id` 或重试时间。相同业务决策重跑时 SQLite `INSERT OR IGNORE` 与 outbox 共同保证幂等。

### 5.2 持久化兼容

本次修复复用现有 `SignalEvent`、`signal_events` 和 `signal_outbox` 表：执行决策事件以 `indicator_name=execution_decision` 投影为 `SignalEvent`，因此不需要数据库迁移。新接口使用 `notification_event_ids`；旧参数名 `notification_signal_ids` 仍可调用，但同样只允许确认执行事件。schema v1 阶段结果仍可读取，旧 A 级通知候选在恢复提交时会被安全抑制。

## 6. 单资产检测与多资产回测

### 单资产检测

```text
K 线 DataFrame + AssetConfig + StrategyConfig
  -> DetectorService
  -> 读取 ScanCursor / PositionState
  -> CandidateDetector 与状态机
  -> DetectionDecision
  -> SignalRepository 原子写入 cursor、仓位、信号和 outbox
```

决策键重复时不再提交，避免重复信号与重复通知。

### 多资产回测

```text
多标的 OHLCV
  -> multi_asset.data（清洗、校验、对齐）
  -> AssetSpec + TurtleRules + BacktestConfig
  -> BacktestService / TurtleBacktester
  -> 权益曲线、订单、交易、绩效指标
  -> StrategyRunManifest（代码版本、配置指纹、数据指纹）
  -> JSON / CSV / HTML 输出
```

`StrategyRunManifest` 将数据、策略与配置的指纹记录在同一运行中，使回测结果可追溯和复现。

## 7. 关键设计规则

1. 新增纯计算能力优先放入 `core`，新增业务流程优先放入 `application`；外部 I/O 放入 adapter、data、integration 或 observability。
2. `data/config/assets.yaml` 是市场标的身份和数据语义的权威来源；`config/strategy.yaml` 是规范策略配置来源。
3. 每次正式日线运行需保留数据版本、策略版本、配置快照、血缘和 SHA-256 审计链。
4. `adapters/` 是基础设施与历史实现的隔离边界，不能成为新增通用业务规则的默认落点。
5. 研究模式结果须与正式告警分离；正式告警需要满足数据治理质量和新鲜度门禁。
6. A/B/C、均线、MACD 和 Turtle 事件是证据，不是正式通知事件。
7. 只有确认的 `ExecutionDecisionEvent` 可以进入 outbox；`ENTRY_CANDIDATE_*` 不进入。
8. 展示层只读取 `TrendDecisionResult.reason_code`、`eligibility_status` 和结构化事件，不重新判断策略。
9. 本次 P0 不改变现有指标参数、SQLite 表结构、CLI 名称和六阶段边界。

## 8. 安装与命令入口

```bash
python -m pip install -e .
python -m pip install -e ".[test]"  # 测试与开发依赖
python -m pip install -e ".[okx]"   # 可选 OKX 集成
python -m pip install -e ".[mt5]"   # Windows 下可选 MT5 集成
```

Python 版本要求为 3.10 或更高。核心命令入口为 `market-data`、`turtle-data`、`turtle-detect`、`turtle-alert` 和 `turtle-daily`。
