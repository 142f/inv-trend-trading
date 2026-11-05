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

## 像素回归（Playwright）环境

`tests/test_美股前100指标_像素.py` 用 `channel="chrome"` 启动本机 Chrome。它需要一个
可执行的 Playwright **驱动 node**：`playwright/driver/node.exe`。若该文件缺失，
`sync_playwright()` 会在 `_transport.connect()` 处抛
`FileNotFoundError [WinError 2]`，看起来像"测试挂了"，实际是驱动缺件。

两种修法（任选其一）：

1. 允许 Python 包自带驱动：`python -m playwright install --with-deps chromium`，
   或重装同版本 `playwright` 让 wheel 里的 `driver/node.exe` 落地；
2. 复用系统 node：
   ```bash
   export PLAYWRIGHT_NODEJS_PATH="$(command -v node)"   # Windows: node.exe 绝对路径
   ```
   或直接把 `node.exe` 复制到 `.venv/Lib/site-packages/playwright/driver/node.exe`。

本机 2026-11-05 按第 2 种修法（系统 node v22.22.2）修复，像素回归 2 项全过；
修复前该用例是唯一的"环境失败"，不是代码缺陷。

