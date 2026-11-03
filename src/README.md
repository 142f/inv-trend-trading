# src

唯一可维护实现。包根为 `src/inv_trend/`，按 `domain`、`core`、`application`、`adapters`、
`ports`、`data`、`storage`、`infra`、`monitoring`、`cli` 分层。

## 谁写入

开发者在修改包内代码时写入；`__pycache__/` 由 Python 运行时写入，不属于源码。

## 谁读取

`tests/`、`scripts/`、`research/` 与已安装的命令行入口都只从这里导入实现。
配置数据只从 `config/`、`execution/` 读取，业务数据只从 `data/`、`processed_data/` 读取。

## 什么可以删

`__pycache__/`、`*.pyc` 只能通过 `scripts/目录治理_v1.py` 清理。源码、包内
`*.yaml`、`*.sql`、`*.css`、`*.js` 与 README 不得按“临时文件”删除。
