# v2 滚动研究交付：Research RC

## 数据存储 v3（与交易策略版本分开）

`完整数据_v3.zip` 必须与 v3 代码补丁一起使用。元数据与长期结果的唯一数据库为
`data/metadata/研究目录_v3.sqlite3`；原始/大规模时序文件采用内容寻址保存。
工程版本为 `0.3.0rc3`，交易策略仍为 `滚动策略_v2`，执行框架仍为 B6，未批准实盘升级。

- 结构、迁移、全量字段和验收限制：`docs/数据结构与迁移说明_v3.md`。
- 数据核验：`python scripts/数据目录管理_v3.py verify --data data`。
- 真实 Parquet 解码验收：上述命令追加 `--strict-parquet`，必须安装 PyArrow。
- 正式研究：`python scripts/结构化研究_v3.py --study 冻结研究_v3`。
- 旧 CSV/JSON 路径仅为兼容读取或显式导出；不得重新建成第二份正式结果库。
- 不要混合覆盖新旧 data；先停止写入并备份，再整体切换目录。不存在数据质量或实盘“自动通过”。


当前维护版本为 **0.3.0rc2 / 框架B6 / 滚动策略_v2**。未批准实盘，也没有把历史回测冠军升级为默认策略。此前冻结v4仅作参考基准。完整结论以 `docs/滚动验证与修改说明_v2.md`、`config/发布状态_v2.json` 为准。

```bash
python scripts/环境核验_v2.py --strict
python scripts/滚动审计复现_v2.py --output outputs/滚动研究_v2
python scripts/回放制品核验_v2.py outputs/滚动研究_v2
```

需保留原始附件的 `processed_data/`；行情SHA256由协议核验，禁止静默替换。输出目录必须不存在。旧CLI保留兼容，不等于获得实盘认证。增量覆盖不能自行删除文件，执行 `python scripts/旧实验清理_v2.py --apply` 才会移除内容与v1哈希完全匹配的11个旧实验入口；冲突时停止并保留本地工作。

---

# inv-trend-trading-core

多资产日线趋势/突破研究与每日预警系统。项目以受治理、可版本化的 OHLCV 为输入，生成可复现的策略证据、趋势判断、执行候选、正式执行决策、审计制品和可重试通知。

本交付基于 `inv-trend-trading-core-architecture-20260823.zip` 完成 **P0 业务正确性修复**：正式通知不再由 A 级评级事件触发，而只由最终确认的执行决策事件触发。完整修改范围、文件清单和兼容性见 [`项目代码修改说明_v1.md`](项目代码修改说明_v1.md)。

## 1. 代码边界

```text
CLI / Scheduler
        ↓
Application：用例编排、阶段合同、决策投影
        ↓
Core：无 I/O 指标、策略证据、ExecutionDecisionEvent
        ↓
Data / Adapters：数据治理、SQLite、Provider、通知、报告
```

| 路径 | 职责 |
|---|---|
| `src/inv_trend/core/` | 无外部 I/O 的特征、指标、规则、信号和执行决策事件 |
| `src/inv_trend/data/` | Provider、标准化、质量门禁、不可变版本、Review、Catalog、current 激活与血缘 |
| `src/inv_trend/application/` | 日报、检测、回测和阶段工作流编排 |
| `src/inv_trend/application/daily/` | `data-update → strategy-screen → trend-decide → commit → publish → deliver` |
| `src/inv_trend/adapters/detector/` | 单资产 Turtle 适配、SQLite 状态/outbox、通知 |
| `src/inv_trend/adapters/multi_asset/` | 多资产 Turtle 研究、回测、组合风险 |
| `src/inv_trend/observability/` | JSON、HTML、CSV 与审计展示，不重新计算策略 |
| `tests/` | 本次 P0 执行决策与通知语义回归测试 |

更完整的模块和数据流见 [`CORE_CODE_ARCHITECTURE.md`](CORE_CODE_ARCHITECTURE.md)。

## 2. 安装

```bash
python -m pip install -e .
python -m pip install -e ".[test]"
```

### 国内网络：按需使用 PyPI 镜像

以下命令仅为本次安装指定清华 TUNA PyPI 镜像，不会写入 `pip.ini` 或改变用户全局 pip 配置。镜像用法请参阅 [TUNA 官方说明](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)。

```bash
python -m pip install --index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -e .
python -m pip install --index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -e ".[test]"
python -m pip install --index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -e ".[okx]"
python -m pip install --index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple -e ".[mt5]"
```

如果环境设置了 `PIP_NO_INDEX=1`，pip 会处于离线模式，不能访问任何镜像。在已获准联网的 PowerShell 会话中，可先执行 `Remove-Item Env:\PIP_NO_INDEX -ErrorAction SilentlyContinue`，再运行上述命令；该设置只影响当前会话。

可选集成：

```bash
python -m pip install -e ".[okx]"
python -m pip install -e ".[mt5]"   # MetaTrader5 仅在 Windows 安装
```

要求 Python `>=3.10`。正式 Parquet 数据链依赖 PyArrow；DuckDB Catalog 依赖 DuckDB，也可使用 SQLite Catalog。

## 3. 日线工作流

```text
data-update → strategy-screen → trend-decide → commit → publish → deliver
```

- `data-update`：固定数据版本、质量、新鲜度、血缘和运行配置；
- `strategy-screen`：生成指标、评级、突破候选和资格证据，不写状态；
- `trend-decide`：只消费筛选证据，生成最终趋势与动作；
- `commit`：唯一允许写入信号、cursor 和 outbox 的阶段；
- `publish`：只从已完成 JSON 派生 HTML/CSV/Manifest；
- `deliver`：只投递 outbox，可安全重试。

一键运行：

```bash
turtle-daily run --symbol BTC --no-color
```

阶段恢复必须复用相同的 `--output-dir`、`--report-date` 和 `--run-id`。阶段文件 Hash、数据版本或运行上下文不一致时失败，不隐式重算。

## 4. 正式通知语义

正式动作由三个条件共同决定：

```text
A 级同向趋势
+ 同方向 Turtle 20/55 突破
+ eligibility 已实际评估且 PASSED
```

| 证据状态 | `execution_state` | 正式 outbox |
|---|---|---|
| A 级、无同向突破 | `WAIT` | 不写入 |
| A 级 + 突破、资格未评估/未确认 | `ENTRY_CANDIDATE_LONG/SHORT` | 不写入 |
| A 级 + 突破、资格阻断 | `WAIT` / `RISK_BLOCKED` | 不写入 |
| A 级 + 突破、资格确认通过 | `ENTER_LONG/SHORT` | 写入一个 `ENTRY_DECISION_LONG/SHORT` |

SMA、EMA、MACD、Turtle、A/B/C 等事件继续作为技术证据保存和展示，但不能直接进入正式通知队列。

`ExecutionDecisionEvent` 的 ID 由以下稳定业务维度生成，不包含 `run_id` 或重试时间：

```text
strategy_version
+ decision_rule_version
+ instrument_id
+ timeframe
+ as_of
+ action
```

因此同一决策重复运行只保留一条信号和一条 outbox 记录。

> `eligibility_gate_enabled: false` 或未注入执行上下文时，系统只输出 `ENTRY_CANDIDATE_*`。要生成正式 `ENTER_*`，必须启用资格闸门并提供实际评估结果。

## 5. 其他命令

```bash
market-data --help
turtle-data --help
turtle-detect --help
turtle-alert --help
turtle-daily --help
strategy-backtest --help
```

历史行情示例：

```bash
market-data --root data download \
  --symbol BTC --timeframe D1 \
  --start 2017-08-17T00:00:00Z

market-data --root data update --symbol BTC --timeframe D1
market-data --root data coverage --symbol BTC --timeframe D1
market-data --root data verify --kind catalog
```

## 6. 验证

本交付包内可执行：

```bash
PYTHONPATH=src pytest -q tests/test_daily_execution_decision_events.py
PYTHONPATH=src python -m compileall -q src tests
```

本次交付验证结果：

```text
P0 targeted tests: 19 passed
compileall: passed
```

原始 ZIP 未包含其 README 所引用的完整历史测试、Golden、数据 fixture、脚本和运行输出，因此本交付**不继承也不重新声明**历史 `239 passed`、Golden 或生产环境验收结论。PyArrow、DuckDB、真实 Provider、Windows、多进程和故障注入仍需在目标环境执行。

## 7. 兼容性

- CLI 名称和参数不变；
- SQLite 的 `signal_events`、`signal_outbox` 表结构不变，无数据库迁移；
- `notification_signal_ids` 参数名继续接受，但执行“仅允许确认执行事件”的新安全语义；
- 新投影使用 `notification_event_ids`，同时保留同值的旧字段作为读取兼容别名；
- schema v1 的 `TrendDecisionResult` Hash 计算保持兼容，可读取旧阶段结果；旧阶段中以 A 级技术事件作为通知候选的记录会被安全抑制，不再进入正式 outbox；
- 主要行为变化：过去可能输出 `ENTER_*` 的“资格未评估”场景，现在降级为 `ENTRY_CANDIDATE_*`。这是有意的 fail-closed 修复。

## 8. 当前范围

本版本完成的是 P0 通知/决策语义闭环。以下仍属于后续 P1/P2，不在本次代码中伪装为已完成：

- daily、detector、multi-asset backtest 共用同一个 canonical 策略内核；
- 多份策略 YAML 收敛为单一权威 schema；
- application 对具体 adapter 的依赖反转；
- A/B/C 与多期限趋势 challenger 的成本后样本外比较；
- HMM/复杂 regime、性能并行化及生产环境全量验收。
