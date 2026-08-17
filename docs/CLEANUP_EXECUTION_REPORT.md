# 安全清理与测试资产恢复执行报告

执行日期：2026-08-17
执行范围：当前工作树；未创建提交、未推送、未执行 Git 清理或 ACL/所有权变更。

## 结果摘要

本轮先恢复了可提交的测试资产，再完成了限定范围内的缓存和历史展示文件
清理。完整回归为 **239 passed**；重新认证的 Golden Master 及 Git
`121f901` 的共同策略路径均无首个业务语义差异。

## 测试恢复与 Golden Master

- 已校验并使用跟踪的 `inv-trend-trading-core-20260814.zip`，SHA-256 为
  `554738bb5c8d81a21f7fdb466f6a9cb2ad90cb49fed4f980f7f294b2203f4f7a`。
  其中 25 个 `tests/test_*.py` 已在隔离目录逐字节提取后恢复。
- `tests/RECOVERY_MANIFEST.json` 记录 ZIP、Git `347bf3f`、恢复前工作树和
  当前文件的逐项 SHA-256。22 个测试保持 ZIP 字节一致；3 个有明确且受限的
  适配：Golden fixture 重新认证，以及两个 `sha256_file` 导入迁至
  `historical_data.integrity`。
- `.gitignore` 已不再忽略 `tests/`；测试、fixture 和恢复清单均可由 Git
  发现。
- 原声明的 Golden 输入 SHA-256
  `8f41bfc6ee1060fec10e0f7117044839c14c5e65066a8767b778c7ad865e8cfe`
  未能在工作树、Git 历史、交付 ZIP 或本地数据湖中恢复。它没有被伪称为已找回。
- 新固定输入为本地 BTC D1 已发布数据版本
  `028585a633484f05948afa5f` 的 420 根 bar（2022-01-01 至 2023-02-24 UTC），
  静态 LF CSV SHA-256 为
  `ceb9583021462a960a1078e720727ec85dcb86687ca92f9b49e1cbf5227e8744`。
  它及 expected 结果均标记为 `reauthenticated`。
- Git `121f9016f8490baf42f5c2212aa46d97b9247290` 与当前代码对共同可运行
  范围（共享特征、Detector、组合订单/交易/权益/指标）在 `1e-10` 相对误差内
  等价，首个业务差异为无。`PreparedBars` 与 Daily 信号为当前新增路径，作为
  当前 Golden 覆盖，不被伪称为旧基线可直接导入的模块。
- 常规 pytest 只读 expected。`python tests/refresh_golden_master.py` 无
  `--confirm` 时以退出码 2 拒绝写入；确认刷新前会打印首个差异。

## 受保护资产与清理

`docs/PROTECTED_ASSET_MANIFEST_PRE_CLEANUP.json` 和
`docs/PROTECTED_ASSET_MANIFEST_POST_CLEANUP.json` 字节一致，SHA-256 均为
`63d47d56c9456aed4b38d48bcb16ff82bd519788bfa6f2e9ccb600953e41e5eb`。清单
覆盖 9 类资产、2,813 个文件、192,751,934 bytes：`data/`、`processed_data/`、
`deliverables/`、基线 ZIP、两套回测证据、Daily SQLite，以及 Turtle Alert
`state.json` 与 `alerts.jsonl`。

删除的对象均在删除前经绝对路径校验，合计 179,692,729 bytes：

| 类别 | 数量 | 回收空间 | 内容 |
| --- | ---: | ---: | --- |
| 本轮 pytest 临时目录、pytest/Ruff 缓存 | 6 目录 / 1,699 文件 | 176,378,707 B | 本次验证产生的 `t`、`u`、`v`、`test-temp` 及缓存 |
| 非 `.venv`、非 `deliverables/` 的 Python 缓存 | 37 目录 / 321 文件 | 2,602,723 B | `__pycache__` 与 `.pyc` |
| 历史展示文件 | 9 文件 | 711,299 B | Daily HTML/JSON、日期化 Turtle 日志、US trend 展示输出 |

保留项包括 `signals.sqlite3`、`state.json`、`alerts.jsonl`、两套回测输出、
`deliverables/`、顶层 ZIP、`data/`、`processed_data/`、`historical_data.legacy`、
`requirements-okx.txt`、性能资产以及 `.tmp/k`。`.tmp/k` 因 Windows ACL 无法安全
审计，未遍历、未删除、未改变权限。

## 验证结果

| 检查 | 当前结果 |
| --- | --- |
| pytest 收集 | 239 项，0 collection error |
| 完整 pytest | **239 passed**，60.29 s（短工作区 basetemp） |
| Golden Master | 2 passed；共同基线路径首差为无 |
| Ruff | `ruff check .` 通过 |
| 编译 | `compileall` 覆盖核心、应用、适配器、CLI、研究、脚本、示例和测试，通过 |
| CLI 冒烟 | `turtle-data`、`turtle-detect`、`turtle-alert`、`turtle-daily`、`market-data` 的 `--help` 均通过 |
| Git | `git diff --check` 通过；未运行 `git clean` 或 `git gc` |

性能基准在 Python 3.12.6、720 bar、预热后 7 次中位数下重新运行：

| 路径 | Before | After | 时间 | 峰值内存 |
| --- | ---: | ---: | ---: | ---: |
| Feature preparation | 58.197 ms / 219,752 B | 30.015 ms / 246,382 B | 1.94× 更快 | 12.1% 增加 |
| Detector replay | 878.301 ms / 1,151,175 B | 538.996 ms / 542,635 B | 1.63× 更快 | 52.9% 降低 |
| Multi-asset timeline | 131.713 ms / 913,539 B | 76.744 ms / 577,891 B | 1.72× 更快 | 36.8% 降低 |

历史 `性能基准v1.json` 的 Python 3.13.5 结果保留为历史快照，不能与本次
Python 3.12.6 数字作直接逐毫秒比较。

## 仍存在的例外

- `processed_data/` 仍有约 13.1 MB 精确重复数据，但它仍被 CLI、研究脚本和
  离线回退缓存使用；未迁移引用前不删除。
- `historical_data.legacy` 仅保留给显式 `market-data migrate-legacy`，不参与
  默认读取；`requirements-okx.txt` 仍被可选依赖错误提示引用。
- `turtle-alert`、`turtle-data` 的部分正式路径仍绕过 application 层；这属于
  后续架构迁移，不被本报告误称为已解决。
- 未执行真实 Provider、生产 Catalog、独立进程崩溃恢复或对象存储环境验证。
