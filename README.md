# inv-trend-trading-core

多资产趋势交易研究与每日预警代码库。已迁移的路径围绕以下统一链路组织；
仍在迁移中的 CLI 例外以独立验收报告为准：

```text
CLI / Scheduled Job
        ↓
Application Service（用例编排）
        ↓
Core / Strategy（确定性特征、信号、回测、风险规则）
        ↓
Historical Data Repository（版本化读取、质量门禁、血缘校验）
        ↓
Provider / File / Catalog / Output Adapter
```

本版重点重构了历史行情的审核发布、不可变制品、`current` 激活、Catalog 后端选择和血缘校验。历史交付说明见 [`趋势交易核心-重构说明v1.md`](趋势交易核心-重构说明v1.md)；当前验证状态与历史审计说明见 [`docs/REFACTOR_ACCEPTANCE_AUDIT.md`](docs/REFACTOR_ACCEPTANCE_AUDIT.md)。

> **验证状态（2026-08-17，恢复与重新认证进行中）**：下文的历史测试计数和
> Golden 结论不能解释为当前工作树的验证结果。原始 Golden fixture/expected
> 成对制品未能从工作树、交付 ZIP 或 Git 历史中完整找回；将使用新固定的 BTC
> D1 输入重新认证，并在完整门禁执行后更新验收结论。常规测试不会重写 expected。

## 1. 模块边界

| 模块 | 职责 |
|---|---|
| `historical_data/` | Provider 接入、标准化、质量评估、不可变数据湖、审核发布、Catalog、统一 Repository 读取 |
| `inv_trend_core/` | 无外部 I/O 的共享特征、数学、事件、信号和序列化能力 |
| `inv_trend_application/` | 每日扫描、检测、回测等 Use Case 编排 |
| `turtle_detector/` | 单资产海龟候选检测、过滤、风险、回测与提醒 |
| `turtle_multi_asset/` | 多资产海龟回测、数据构建与美股趋势预警 |
| `inv_trend_integrations/` | MT5、OKX 等可选外部适配器；采用惰性导入，未安装可选 SDK 不影响核心包 |
| `inv_trend_observability/` | 审计、Manifest 和 HTML 输出 |
| `tests/` | 恢复与重新认证中的单元、集成、架构边界、事务故障注入与 Golden Master 回归 |
| `scripts/` | 日常运行和基准测试脚本 |

## 2. 安装

```bash
python -m pip install -e ".[test]"
```

可选适配器：

```bash
python -m pip install -e ".[okx]"
python -m pip install -e ".[mt5]"   # MetaTrader5 仅在 Windows 安装
```

Python 要求：`>=3.10`。正式数据存储依赖 `PyArrow`；DuckDB Catalog 依赖 `duckdb`，也可使用 SQLite。

## 3. 历史行情数据

全局参数 `--root` 必须写在子命令前：

```bash
market-data --root data download \
  --symbol BTC --timeframe D1 \
  --start 2017-08-17T00:00:00Z

market-data --root data update --symbol BTC --timeframe D1
market-data --root data missing --symbol BTC --timeframe D1
market-data --root data coverage --symbol BTC --timeframe D1
market-data --root data verify --kind catalog
```

审核冲突候选：

```bash
market-data --root data review list
market-data --root data review approve \
  --run-id <run_id> \
  --decision approve_incoming \
  --reason "已核验供应商原始响应" \
  --actor "reviewer"

market-data --root data review reject \
  --run-id <run_id> --reason "价格冲突无法解释" --actor "reviewer"
```

审批采用“先生成并校验全部不可变制品，再登记 Catalog/审核记录，最后 CAS 激活 current”的流程；失败可重试，指针与 Catalog 不一致时可恢复：

```bash
market-data --root data repair-current --symbol BTC --timeframe D1
```

完整的数据边界、目录、Manifest、审核事务和故障恢复说明见 [`historical_data/README.md`](historical_data/README.md)。

## 4. 策略与每日任务

每日市场扫描：

```powershell
.\scripts\run_daily_market_scan.ps1
```

等价入口：

```bash
turtle-daily --help
turtle-detect --help
turtle-alert --help
turtle-data --help
```

参数、退出码和 Windows 定时任务说明见 [`DAILY_MARKET_SCAN_CLI.md`](DAILY_MARKET_SCAN_CLI.md)。

## 5. 测试与基准

```bash
pytest -q
python scripts/benchmark_refactor.py
```

以下为 2026-08-16 的历史独立验收记录，不是本轮恢复后的当前结果：

```text
Historical passed: 253
Failed: 0
Skipped: 0
```

在 Windows 上请使用短工作区临时路径，避免 Parquet 临时文件超过路径长度限制：

```bash
pytest -q --basetemp .tmp/a
```

历史性能快照保存在 [`性能基准v1.json`](性能基准v1.json)；当前复验结果和适用边界见验收报告。

## 6. 运行数据与版本控制

运行生成的 `data/`、`processed_data/` 和 `outputs/` 默认不入库；源码、测试、Markdown 文档、示例和研究脚本应正常跟踪。敏感配置只放本地 `.env`，模板使用 `.env.example`。

## 7. 当前边界

本版已通过本地 **P0-core** 验收，但完整应用层迁移尚未完成：`turtle-alert` 与部分 `turtle-data` 子命令仍有平行路径，策略配置和 Turtle 规则内核也尚未完全收敛。正式部署封版仍需在目标环境完成真实 Provider 响应、实际数据根、生产规模 Catalog、独立进程恢复和 Windows 文件系统语义验证。交易所官方 holiday/halt、完整历史 QQQ 持仓谱系及全部 research Repository 迁移仍属于后续范围。
