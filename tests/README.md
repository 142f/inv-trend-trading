# tests

正式测试与夹具。`tests/*.py`、`tests/unit/*.py` 由 pytest 收集，样本文件放
`tests/fixtures/`。

## 谁写入

维护者为回归、契约和边界测试写入。一次性核验脚本不放这里，应放 `scripts/`；已完成的
一次性脚本归档到 `artifacts/history/scripts/`。

## 谁读取

`python -m pytest`、`scripts/run_tests.ps1` 与 CI 读取。固定夹具被测试直接引用，
例如 `tests/fixtures/数据流水线基准_v1.json`。

## 什么可以删

`__pycache__/`、`*.pyc`、`.pytest_cache/` 只能通过 `scripts/目录治理_v1.py` 清理。
夹具在对应测试退役前不得删除。
