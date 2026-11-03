# scripts

可重复运行的运维、核验、性能与研究入口。所有脚本默认只读或写入显式 `--output`，
不写入 `data/`、`processed_data/` 业务文件。

## 谁写入

维护者写入可重复入口。已完成且无引用的一次性脚本不留在本目录，归档到
`artifacts/history/scripts/`。

## 谁读取

`README.md`、现行规范、测试与运维命令读取。被源码或测试引用的脚本（如
`refresh_golden_master.py`、`美股前100指标.py`、`结构化研究_v3.py`）不得移动或删除。

## 什么可以删

`__pycache__/`、`*.pyc` 只能通过 `scripts/目录治理_v1.py` 清理。脚本本身在引用清零前
不得删除。
