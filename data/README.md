# data

权威数据根，目录内容不入库（仅本 README 跟踪）。

## 布局

```text
data/
├── raw/          内容寻址的原始行情对象（json.gz）
├── processed/    内容寻址的加工对象（parquet/json）
├── features/     指标与特征产物（预留）
├── signals/      信号产物（预留）
├── backtests/    回测记录（预留）
├── reports/      报告产物（预留）
└── metadata/     研究目录_v3.sqlite3、迁移备份与运行锁
```

## 谁写入

数据流水线写入 `raw/`、`processed/`；研究/回测写入 `features/`、`signals/`、
`backtests/`、`reports/`。`metadata/研究目录_v3.sqlite3` 是目录权威，旁边的
`-wal`、`-shm` 是数据库运行文件，不单独当垃圾删除。

## 谁读取

`src/inv_trend/data` 与回测、审计入口按显式路径读取；`processed_data/` 通过清单引用
这里的对象，不复制第二份权威副本。

## 什么可以删

业务文件不删除、不移动。`metadata/目标迁移备份_*.sqlite3` 是迁移备份，保留。
缓存不进入本目录；`__pycache__` 只能通过 `scripts/目录治理_v1.py` 清理。
