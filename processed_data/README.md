# processed_data

数据流水线的阶段产物，目录内容不入库（仅本 README 跟踪）。按阶段组织，便于在进入
回测前审计。

## 布局

```text
processed_data/
├── raw_index/       来源发现目录
├── cleaned/         单标的清洗后 OHLCV
├── merged/          多标的合并数据
├── backtest_ready/  回测直接消费的稳定数据集
├── logs/            质量、校验、对齐日志
├── metadata/        清单、字段字典、标的池决策
└── us_trend_alerts/ 提醒生成的运行缓存（代码默认路径）
```

## 谁写入

数据流水线按阶段写入；`us_trend_alerts/cache/` 由提醒入口按默认缓存路径写入。

## 谁读取

回测、审计与研究入口读取 `backtest_ready/`；`research/d1_suite` 与
`src/inv_trend/storage` 读取 `cleaned/` 与 `backtest_ready/` 的固定文件。

## 重复与删除

同一阶段只留一份权威文件。能从 `data/` 或上一级阶段文件引用时写清单，不再复制。
业务文件不删除、不移动；`us_trend_alerts/cache/` 是代码默认缓存，保留。
`__pycache__` 只能通过 `scripts/目录治理_v1.py` 清理。
