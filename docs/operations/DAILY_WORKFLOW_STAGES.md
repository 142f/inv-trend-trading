# D1 Daily Workflow Stages

`turtle-daily` 提供模块化、可恢复的 D1 工作流。无子命令调用继续兼容，例如
`turtle-daily --symbol BTC` 与 `turtle-daily run --symbol BTC` 走同一条主链；
新调度和自动化应使用显式阶段。

```text
data-update → strategy-screen → trend-decide → commit → publish → deliver
```

## 阶段命令与交接

每个显式阶段都使用相同的 `--output-dir`、`--report-date` 与 `--run-id`；这些值
标识同一个 staging 工作区。`run-id` 是非空 ASCII 路径片段，`report-date` 使用
`YYYY-MM-DD`。中间 JSON 写入：

```text
<output-dir>/runs/YYYY-MM-DD/.staging/<run-id>/
```

只有 `publish` 会创建不可变的公开运行目录。六个显式阶段成功时都向 stdout 输出一个
机器可读 JSON 摘要；`run` 则保留兼容的人类可读终端摘要和既有退出码。

```powershell
$run = '20260823a'
$date = '2026-08-23'
$root = 'outputs\daily_market_scan'

turtle-daily data-update --output-dir $root --run-id $run --report-date $date --symbol BTC
turtle-daily strategy-screen --output-dir $root --run-id $run --report-date $date
turtle-daily trend-decide --output-dir $root --run-id $run --report-date $date
turtle-daily commit --output-dir $root --run-id $run --report-date $date
turtle-daily publish --output-dir $root --run-id $run --report-date $date
turtle-daily deliver --output-dir $root --run-id $run --report-date $date
```

`data-update` 固定数据版本、质量/新鲜度和血缘结论；`strategy-screen` 使用固定的
D1 版本生成一次特征准备后的条件、原始事件和候选突破；`trend-decide` 只消费筛选
证据，不读取 K 线或重算指标。三者均不会写信号状态、推进 cursor、创建 outbox 或
创建订单。

`commit` 是唯一写入既有 SQLite signal/cursor/outbox 状态的阶段。`publish` 会校验
三阶段 Hash 链和提交回执，然后只从完整 JSON 派生 JSON/HTML/CSV。`deliver` 只消费
本运行可重试的 outbox 消息；投递失败可再次运行 `deliver`，不会重写已发布制品。

后续阶段必须使用同一个 `--output-dir`、`--report-date` 和 `--run-id`。若阶段 JSON
被改动、输入 Hash 不连贯、数据版本不再匹配或前置回执缺失，命令会失败而非重新计算
或推进状态。策略和趋势配置在 `data-update` 时被固定在 `run_context.json`，因此在
阶段之间编辑 YAML 不会改变该运行的业务证据。

## 工作区与发布边界

staging 仅用于阶段交接，结构中包含 `run_context.json`、每标的三份 `*Result.json`、
受筛选 Hash 绑定的运行时审计证据，以及 `commit_receipt.json` / `delivery_receipt.json`。
发布后的权威目录位于：

```text
<output-dir>/runs/YYYY-MM-DD/<run-id>/<symbol>/
```

其 `01_canonical/` 包含三阶段结果及 `complete_analysis_result.json`；
`02_report/` 是只读派生的 HTML；`03_exports/` 是 CSV；`04_audit/` 保存 manifest、
配置/血缘快照及 SHA-256。`latest/<symbol>/` 仅保存最近成功运行的 JSON/HTML 快捷入口；
`YYYY-MM-DD.{json,html}` 是旧脚本兼容副本；`state/signals.sqlite3` 独立于运行目录。

## 一键运行与兼容调用

推荐的一键命令是：

```powershell
turtle-daily run --symbol BTC --no-color
```

它按固定顺序执行全部六个阶段。历史调用仍然兼容：

```powershell
turtle-daily --symbol BTC --no-color
```

## 配置迁移

打包内的权威策略配置为 `inv_trend/config/strategy.yaml`，包含：

```text
schema_version
turtle
daily_screening
trend_decision
risk
```

显式指定的旧 application、detector 与 multi-asset YAML 在迁移期仍可读取并映射到同一
配置模型。新配置应使用带版本的规范格式；市场标的身份继续来自数据层 assets 配置。
