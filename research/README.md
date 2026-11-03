# research

可复现研究套件，不是稳定包接口。当前条目为 `d1_suite/`（D1 多资产回测与剪枝实验）。

## 谁写入

研究者写入研究脚本与实验定义。运行结果写入 `research/**/outputs/`，该目录不入库。

## 谁读取

研究者复现实验时读取；稳定后的通用逻辑应迁入 `src/inv_trend/core` 或
`src/inv_trend/application`，不留在本目录。

## 什么可以删

`__pycache__/`、`*.pyc` 只能通过 `scripts/目录治理_v1.py` 清理。`outputs/` 可按需清理
（它本身不入库），研究脚本与说明在退役前不得删除。
