# 项目目录职责

本项目采用“源码、验证、研究、运行资产、交付物”分组。目录移动不改变行情数据、
策略定义、CLI 名称、参数或默认输出路径。

```text
repo/
├── src/inv_trend/            # 唯一可发布 Python 命名空间
│   ├── core/                 # 无 I/O 的算法与领域原语
│   ├── data/                 # 行情数据与数据湖
│   ├── application/          # 用例编排
│   ├── adapters/             # 过渡中的 Turtle 具体实现
│   ├── integrations/         # 可选交易/行情 SDK 适配
│   ├── observability/        # 报告与审计输出
│   └── cli/                  # 五个 CLI 的参数边界
├── tests/                    # 单元、集成、架构与 Golden 回归
├── research/                 # 实验定义、参数覆盖和分析脚本
├── scripts/                  # 运维与可复现基准脚本
├── examples/                 # 非生产示例
├── docs/                     # 架构、运维、审计、重构与基准文档
├── data/                     # 运行数据湖（保留既有默认路径）
├── processed_data/           # 研究数据工件（保留既有默认路径）
├── outputs/                  # 状态、回测证据和可再生报告
├── logs/                     # 本地运行日志
├── deliverables/             # 受保护的历史交付物
├── inv-trend-trading-core-20260814.zip  # 受保护的基线快照
├── pyproject.toml            # 打包、依赖与 CLI 定义
├── pytest.ini                # 测试收集与 src Python 路径
└── README.md                 # 项目入口说明
```

## 使用入口

| 需求 | 入口 |
|---|---|
| 获取、审核、验证市场数据 | `market-data` / `inv_trend.data` |
| 执行日扫、检测或回测用例 | `inv_trend.application` |
| 复用因果特征与基础绩效 | `inv_trend.core` |
| 调用稳定 CLI | `turtle-data`、`turtle-detect`、`turtle-alert`、`turtle-daily`、`market-data` |
| 进行实验 | `research/`，通过 Application API，而不是直接构造 CLI 或数据湖对象 |

`adapters/` 不应成为新功能的默认落点。新增规则优先放入 `core`（纯计算）或
`application`（用例编排）；只有对历史 Turtle 行为的行为等价迁移才暂放入该目录。
