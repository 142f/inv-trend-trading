# turtle-daily 命令行使用说明

## 基本命令

```powershell
.\.venv\Scripts\python.exe -m inv_trend.cli.daily --symbol BTC --symbol ETH --no-color
```

或使用启动脚本（优先项目 `.venv`）：

```powershell
.\scripts\run_daily_market_scan.ps1 --symbol BTC --symbol ETH --no-color
```

默认更新配置中的全部 D1 品种，然后进行质量、新鲜度、海龟、兼容的 SMA10/20 与 D1 MACD 检查，以及 SMA 排列、EMA144/169、D1/D2/D5/D7 MACD、ADX/DMI、ATR、相对成交量的平行策略检查，写入 JSON、SQLite、JSONL 和 HTML。底层策略事件都会入 SQLite；仅进入 A 级共振的事件进入 JSONL 通知。

## 参数

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--symbol SYMBOL` | 全部 D1 | 可重复指定，例如 `--symbol BTC --symbol ETH` |
| `--data-root PATH` | `data` | Parquet 数据湖和 Catalog 根目录 |
| `--output-dir PATH` | `outputs/daily_market_scan` | JSON、HTML、SQLite 输出目录 |
| `--database PATH` | `<output-dir>/signals.sqlite3` | SignalStore 路径 |
| `--signal-log-dir PATH` | `logs/signals` | 正式新信号 JSONL 目录 |
| `--bootstrap-days N` | `400` | 首次初始化自然日数 |
| `--provider-timeout N` | `10` | Provider 单次 HTTP 超时（秒） |
| `--provider-retries N` | `1` | Provider / 数据服务尝试次数 |
| `--scan-only` | 关闭 | 不访问网络，只扫描本地 current；仍执行新鲜度门禁 |
| `--research-mode` | 关闭 | 在快照显示研究数据事件，但不正式持久化/通知 |
| `--strategy-version` | `corrected-v2` | 仅支持可执行版本 `corrected-v2`；`legacy-v1` 仅是只读归档元数据 |
| `--backfill-signals` | 关闭 | 研究性回放完整历史；新增事件不发送通知 |
| `--chart-bars N` | `180` | HTML 每品种显示最近 N 根 bar |
| `--no-html` | 关闭 | 不生成 HTML，JSON/SQLite 仍正常生成 |
| `--open-report` | 关闭 | 生成后打开 HTML；只有显式指定才打开浏览器 |
| `--no-color` | 关闭 | 使用纯文本终端输出 |

`--open-report` 不能与 `--no-html` 同时使用。所有天数、超时、重试和图表根数必须为正整数。

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
  --database outputs\daily_market_scan_test\signals.sqlite3 `
  --signal-log-dir logs\signals_test `
  --no-color
```

## 输出和状态

- `YYYY-MM-DD.json`：机器可读快照，同日重跑覆盖。
- `YYYY-MM-DD.html`：UTF-8、自包含 CSS/SVG 日报，不依赖 CDN。
- `signals.sqlite3`：信号、唯一业务 key、DailyRun、cursor 和 outbox。
- `logs/signals/YYYY-MM-DD.jsonl`：仅追加成功进入 SignalStore 的正式新信号。

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
