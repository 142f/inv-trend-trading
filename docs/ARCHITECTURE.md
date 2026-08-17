# 架构与依赖边界

## 规范源码布局

所有可发布 Python 代码只位于 `src/inv_trend/`。不再提供
`historical_data`、`inv_trend_core`、`turtle_detector` 等旧顶级 Python
导入路径；公开的五个命令名称及其参数保持不变。

```text
src/inv_trend/
├── core/            # 纯内存特征、数学、事件、信号和绩效计算
├── data/            # 行情身份、Provider、质量、数据湖、Catalog、审核迁移
├── application/     # Daily Scan、Detector、Backtest 用例与运行清单
├── adapters/        # 仍在收敛中的 Turtle detector / multi-asset 实现
├── integrations/    # 可选 MT5、OKX 适配器
├── observability/   # 审计与 HTML / JSON 表现层
└── cli/             # turtle-data/detect/alert/daily/market-data 薄入口
```

`adapters` 是刻意显式的过渡边界：它避免将仍含有状态、回测和旧数据构建职责的
两套 Turtle 实现伪装成 Core。共享指标、因果特征和绩效基础计算已由 `core` 提供；
剩余策略适配器将按行为等价测试逐步收敛，而不是在目录迁移中改变交易语义。

## 依赖方向

```text
cli ───────────────> application ───────> core
  │                         │              ▲
  │                         ├────────────> data
  │                         └────────────> adapters ───> core / data
  └──────────────> observability ───────> core

integrations ───────────────────────────> adapters  (当前 AssetSpec 过渡例外)
```

- `core` 不能依赖数据湖、网络、SQLite、CLI、报告、Application 或 Adapter。
- `data` 不能依赖 Application 或 Turtle Adapter；`assets.yaml` 是市场身份与数据
  语义的唯一默认权威。
- `application` 只编排用例并返回结果对象；运行清单属于此层，不属于报告层。
- `observability` 只渲染和记录，不能成为规则或指标的权威。
- `cli` 仅解析参数、调用服务并呈现结果。`turtle-alert` 仍直接调用一个
  Adapter 扫描用例，是记录在审计报告中的过渡例外。

## 运行目录

为保持既有命令的默认路径、Scheduler 配置和通知去重状态，以下目录暂留仓库根部，
并且不属于源码包：

```text
data/            # 版本化市场数据湖
processed_data/  # 可复现实验/旧数据构建输入
outputs/         # 报告、SQLite、通知状态和回测证据
logs/            # 运行日志
deliverables/    # 受保护的历史交付物
```

根目录其余内容以项目配置、README 和受保护的基线 ZIP 为主；说明文档已按
`docs/{audits,operations,refactoring,benchmarks}` 分组。
