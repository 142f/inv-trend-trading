# turtle-daily 命令行使用说明

## 基本命令

```powershell
.\.venv\Scripts\python.exe -m inv_trend.cli.daily --symbol BTC --symbol ETH --no-color
```

或使用启动脚本（优先项目 `.venv`）：

```powershell
.\scripts\run_daily_market_scan.ps1 --symbol BTC --symbol ETH --no-color
```

以上无子命令形式仍然完全兼容，等价于 `turtle-daily run`。新自动化应优先
使用显式 `run` 或下文的分阶段命令；它们都执行同一条 D1 主链：

```text
data-update → strategy-screen → trend-decide → commit → publish → deliver
```

一键运行示例：

```powershell
turtle-daily run --symbol BTC --symbol ETH --no-color
```

`run` 自动生成 `run_id` 和报告日期，并按上述顺序调用六个阶段。它保留原有的
人类可读终端摘要、参数和退出码；无子命令调用也是同一实现，不再走一条平行的
旧流程。

默认更新配置中的全部 D1 品种，然后进行质量、新鲜度、海龟、兼容的 SMA10/20 与 D1 MACD 检查，以及 SMA 排列、EMA144/169、D1/D2/D5/D7 MACD、ADX/DMI、ATR、相对成交量的平行策略检查，写入 JSON、SQLite、JSONL 和 HTML。底层策略事件都会入 SQLite；仅进入 A 级共振的事件进入 JSONL 通知。

## 分阶段执行（推荐用于调度、审计与重试）

每个显式阶段均以同一组定位参数交接工作区：`--output-dir`、`--report-date`
和 `--run-id`。`run_id` 必须是非空 ASCII 路径片段（可使用字母、数字、`.`,
`_`, `-`）；`report-date` 使用 `YYYY-MM-DD`。只有 `data-update` 接收本次
扫描的 `--symbol`、刷新/研究/回放和图表参数；后续阶段只读取已固定的上下文与
权威 JSON。

```powershell
$date = '2026-08-23'
$run = '20260823-090000'
$root = 'outputs\daily_market_scan'

turtle-daily data-update --output-dir $root --report-date $date --run-id $run `
  --symbol BTC --symbol ETH
turtle-daily strategy-screen --output-dir $root --report-date $date --run-id $run
turtle-daily trend-decide --output-dir $root --report-date $date --run-id $run
turtle-daily commit --output-dir $root --report-date $date --run-id $run
turtle-daily publish --output-dir $root --report-date $date --run-id $run
turtle-daily deliver --output-dir $root --report-date $date --run-id $run
```

`data-update`、`strategy-screen` 与 `trend-decide` 是只读业务阶段：它们不推进
cursor、不写正式信号、不创建 outbox，也不创建订单。`commit` 是唯一写入既有
SQLite 信号、cursor 和 outbox 的阶段；`publish` 只校验已提交结果并派生
JSON/HTML/CSV；`deliver` 只投递或重试已有 outbox。重复执行 `commit`、`publish`
或已无待投递消息的 `deliver` 是可恢复的，不会重复产生正式信号。

显式阶段命令成功时向 stdout 输出单个机器可读 JSON 摘要，适合 PowerShell、任务
调度器或 CI 解析。`publish` 的摘要包含最终运行目录和兼容副本路径；错误的 Hash、
缺失前置制品或不匹配的数据版本会拒绝继续，而不是重新计算或猜测输入。

详细的阶段职责、配置迁移和交接规则见
[`DAILY_WORKFLOW_STAGES.md`](DAILY_WORKFLOW_STAGES.md)。

## 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--symbol SYMBOL` | 全部 D1 | 可重复指定，例如 `--symbol BTC --symbol ETH` |
| `--data-root PATH` | `data` | Parquet 数据湖和 Catalog 根目录 |
| `--output-dir PATH` | `outputs/daily_market_scan` | 日扫制品根目录；staging、发布制品、`latest`、兼容快照和状态库在其下分层保存 |
| `--database PATH` | `<output-dir>/state/signals.sqlite3` | SignalStore 路径 |
| `--signal-log-dir PATH` | `logs/signals` | 正式新信号 JSONL 目录 |
| `--bootstrap-days N` | `400` | 首次初始化自然日数 |
| `--provider-timeout N` | `10` | Provider 单次 HTTP 超时（秒） |
| `--provider-retries N` | `1` | Provider / 数据服务尝试次数 |
| `--scan-only` | 关闭 | 不访问网络，只扫描本地 current；仍执行新鲜度门禁 |
| `--research-mode` | 关闭 | 在快照显示研究数据事件，但不正式持久化/通知 |
| `--strategy-version` | `corrected-v2` | 仅支持可执行版本 `corrected-v2`；`legacy-v1` 仅是只读归档元数据 |
| `--backfill-signals` | 关闭 | 研究性回放完整历史；新增事件不发送通知 |
| `--chart-bars N` | `180` | HTML 每品种显示最近 N 根 bar |
| `--no-html` | 关闭 | 对 `run` 或 `publish` 不生成 HTML；JSON/SQLite 仍正常生成 |
| `--open-report` | 关闭 | 生成后打开 HTML；只有显式指定才打开浏览器 |
| `--no-color` | 关闭 | 使用纯文本终端输出 |

`--open-report` 不能与 `--no-html` 同时使用。所有天数、超时、重试和图表根数必须为正整数。
`--report-date`、`--run-id` 仅用于显式阶段；`run` 和兼容的无子命令调用会自动生成它们。

## 常用命令

正常更新 BTC/ETH：

```powershell
.\scripts\run_daily_market_scan.ps1 --symbol BTC --symbol ETH --no-color
```

网络不可用时扫描本地 current：

```powershell
.\scripts\run_daily_market_scan.ps1 --symbol BTC --symbol ETH --scan-only --no-color
```

生成并显式打开 240 根 bar 的报告：

```powershell
.\scripts\run_daily_market_scan.ps1 --symbol BTC --chart-bars 240 --open-report
```

研究性历史回放（不发送正式通知）：

```powershell
.\scripts\run_daily_market_scan.ps1 --symbol BTC --backfill-signals --research-mode --no-color
```

独立测试目录：

```powershell
.\.venv\Scripts\python.exe -m inv_trend.cli.daily `
  --symbol BTC `
  --scan-only `
  --output-dir outputs\daily_market_scan_test `
  --database outputs\daily_market_scan_test\state\signals.sqlite3 `
  --signal-log-dir logs\signals_test `
  --no-color
```

## 输出和状态

在 `publish` 前，阶段间仅通过私有、可重试的 staging 工作区交接：

```text
<output-dir>/runs/YYYY-MM-DD/.staging/<run_id>/
├─ run_context.json
├─ <symbol>/01_canonical/
│  ├─ data_update_result.json
│  ├─ strategy_screening_result.json
│  └─ trend_decision_result.json
├─ <symbol>/04_audit/screening_runtime.json
├─ commit_receipt.json
└─ delivery_receipt.json
```

其中三份 `*Result.json` 是阶段权威载荷；`strategy-screen` 和 `trend-decide`
会验证它们的输入 Hash 链。`screening_runtime.json` 是受筛选结果 Hash 绑定的审计
证据，不是独立的决策输入。staging 目录不是归档目录，不能直接被下游报告或回测
当作发布结果。

发布后目录如下：

```text
<output-dir>/
├─ 输出索引.json                       # 当前批次与各类快捷入口
├─ runs/YYYY-MM-DD/<run_id>/
│  ├─ 批次索引.json
│  └─ <symbol>/
│     ├─ 结果索引.json
│     ├─ 01_canonical/              # 三阶段 JSON + complete analysis JSON
│     ├─ 02_report/                 # HTML；图表资源内嵌，不依赖外部文件
│     ├─ 03_exports/                # 条件、信号、海龟观察、异常/异常阶段、状态变更 CSV
│     └─ 04_audit/                  # manifest、配置、血缘与 artifact SHA-256
├─ 汇总结果/YYYY-MM-DD/<run_id>/    # 带批次、周期和版本的汇总 JSON / HTML
├─ latest/<symbol>/                 # 最近一次成功发布的完整 JSON / HTML / 结果索引
├─ state/signals.sqlite3            # 信号、cursor、DailyRun、outbox
└─ YYYY-MM-DD.{json,html}           # 旧路径兼容副本
```

- `runs/YYYY-MM-DD/<run_id>/` 是不可变的历史审计证据；同日重跑必须使用新的
  `run_id`，不会覆盖已发布运行。
- `complete_analysis_result.json` 是业务基准。HTML 与 CSV 只从它派生，不重新计算
  指标或交易决策。
- 每个类型目录另有
  `<标的>_<周期>_<日期>_<run_id>_<结果类型>_v<报告版本>.<扩展名>` 描述性入口；
  固定英文文件名继续作为程序集成兼容契约。
- 查找制品时先读根目录 `输出索引.json`，再读 `批次索引.json` 和标的下的
  `结果索引.json`，无需遍历整个输出目录。
- `latest/<symbol>/` 只指向最近一次成功发布的完整 JSON 与 HTML，不承担归档职责。
- `YYYY-MM-DD.json` 与 `YYYY-MM-DD.html` 是给旧脚本的平铺兼容副本；同日重跑可覆盖，
  不能替代 `runs/` 中的权威制品。
- `state/signals.sqlite3` 不混入一次报告目录，也不新增表；信号 JSONL 仍由
  `--signal-log-dir` 管理（默认 `logs/signals/YYYY-MM-DD.jsonl`），审计清单仅记录
  其引用和本次投递摘要。

状态为 `updated`、`unchanged`、`blocked`、`stale` 或 `failed`。任一品种 `blocked/stale/failed` 时退出码为 1；参数错误为 2；Ctrl+C 为 130。通知失败时 DailyRun 为 `COMPLETED_WITH_DELIVERY_ERRORS`，不会永久停留在 `RUNNING`。

## Windows 每日 09:00

管理员 PowerShell：

```powershell
$repo = (Resolve-Path E:\Project\inv-trend-trading).Path
$script = Join-Path $repo 'scripts\run_daily_market_scan.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
  -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}" --no-color' -f $script)
$trigger = New-ScheduledTaskTrigger -Daily -At 09:00
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'TurtleDailyMarketScan' -Action $action `
  -Trigger $trigger -Settings $settings `
  -Description 'Refresh D1 data and scan technical signals.' -Force
```

确认本机时区为北京时间：

```powershell
Get-TimeZone
```

TLS 握手卡住属于 Provider 网络可达性问题。可缩短超时，或在本地 current 仍新鲜时使用 `--scan-only`；该参数不会绕过新鲜度与质量门禁。
