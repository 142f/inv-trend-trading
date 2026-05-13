# 趋势 / 海龟交易测试与数据质量升级报告

升级日期：2026-04-20

## 1. 改动文件

- `docs/codex_trend_test_audit.md`
  - 新增审计报告，记录关键文件、现有覆盖、缺口和最小改动计划。
- `docs/codex_trend_test_upgrade_report.md`
  - 新增本报告。
- `examples/download_mt5_data.py`
  - 增强 `data_quality`，新增 market type、expected gap、正常 session gap / 异常 gap 拆分和 spread bps。
  - MT5 下载时读取 `symbol_info.point`，用于把 raw spread points 转为价格后计算 bps。
- `tests/test_download_mt5_data.py`
  - 增加 XAU session gap、BTC 24x7 gap、spread bps 测试。
- `tests/test_turtle_market_scenarios.py`
  - 新增趋势、whipsaw、gap stop、regime shift、混合日历、高成本场景测试。
- `tests/test_real_sample_regression.py`
  - 新增本地 2022 XAU/BTC H4 真实样本结构性回归测试。
- `data_2022_xau_btc/metadata/mt5/data_quality_report.csv`
  - 用新数据质量 schema 重算。
- `data_2020_xau_btc/metadata/mt5/data_quality_report.csv`
  - 用新数据质量 schema 重算。

本次没有修改：

- `turtle_multi_asset/strategy.py`
- `turtle_multi_asset/backtest.py`
- `turtle_multi_asset/profiles.py`

因此没有改变入场、加仓、退出、止损、资金管理或回测成交规则。

## 2. 新增测试生成器 / fixtures / golden cases

新增文件 `tests/test_turtle_market_scenarios.py` 包含：

- `_scenario_rules`
  - 小窗口 TurtleRules，便于少量 K 线人工验证。
- `_asset_spec`
  - 合成品种 spec，可切换成本、滑点、最大加仓数。
- `_trend_bars`
  - 可生成对称上涨 / 下跌趋势。
- golden case 1：多头 gap stop
  - 多头入场价 106，final stop 97.5，下一根开盘 94，断言止损成交价为 94 而不是 97.5。
- golden case 2：空头 gap stop
  - 空头入场价 94，final stop 102.5，下一根开盘 106，断言止损成交价为 106 而不是 102.5。
- golden case 3：多空单边趋势对称
  - 上涨趋势与镜像下跌趋势均产生 open/add/add/exit，unit_count=3，零成本下 PnL 对称。

新增真实样本 fixture：

- `tests/test_real_sample_regression.py`
  - 读取 `data_2022_xau_btc` processed CSV。
  - 使用 `h4-daily-equivalent`、align start/end、初始权益 10000。
  - 使用范围与结构性断言：final equity、orders、trades、两个品种、两个方向、exit type、max drawdown、trade_count。

## 3. 新增真实市场场景覆盖

- 单边上涨趋势
  - 验证 long breakout、加仓、end-of-test 退出、持仓盈利。
- 单边下跌趋势
  - 验证 short breakout、空头加仓、退出、与多头 PnL 对称。
- 假突破 / whipsaw
  - 连续 long breakout 后 stop、short breakout 后 stop，验证多笔 fast 小亏后状态不被卡死。
- fast skip / slow fallback
  - 当 `last_fast_trade_won=True` 时，fast breakout 被跳过，但 slow breakout 仍可发单。
- 跳空穿止损
  - 多头和空头均覆盖，断言使用更差开盘价而不是理想 stop 价。
- ATR / N regime shift
  - 低波动到高波动后 N 显著上升。
  - 高 N 订单 qty 更小、stop 距离更宽。
  - 同样 close 下，低 N 可触发加仓，高 N 不触发加仓。
- XAU/BTC 混合日历
  - 当 XAU snapshot 存在但不在 `tradable_symbols` 时，不生成 XAU 新订单。
  - 既有测试仍覆盖闭市资产使用最近收盘价盯市。
- 高 spread / 高 slippage / 高成本
  - 高成本 spec 的 total_cost > 0，final equity 和单笔净 PnL 均低于零成本场景。

## 4. 数据质量逻辑改进

`data_quality` 新增字段：

- `market_type`
  - BTC/ETH/SOL/LTC/XRP/CRYPTO 识别为 `24x7`。
  - 其他品种默认识别为 `session`，覆盖 XAU/XAG/股票/其他非 7x24 市场。
- `expected_gap_minutes`
  - 根据 timeframe 解析 M/H/D/W。
- `large_gap_count`
  - 大于 expected gap * 1.5 的长间隔总数。
- `normal_session_gap_count`
  - 对 session market，跨周末的长间隔归为正常停盘 gap。
- `abnormal_gap_count`
  - 对 24x7 市场，所有长间隔均归为异常。
  - 对 session market，不跨周末的长间隔归为异常。
- `median_gap_hours` / `max_gap_hours`
  - 用于快速定位 gap 尺度。
- `median_spread_bps` / `p95_spread_bps` / `max_spread_bps`
  - 下载时若能拿到 MT5 `point`，使用 `spread * point / close * 10000`。
  - 没有 point 时，保留按 price unit 估算的 fallback。

本地 metadata 重算后的关键观察：

- `data_2022_xau_btc`
  - BTCUSDc：large_gap=0，abnormal_gap=0。
  - XAUUSDc：large_gap=228，normal_session_gap=224，abnormal_gap=4。
- `data_2020_xau_btc`
  - BTCUSDc：large_gap=0，abnormal_gap=0。
  - XAUUSDc：large_gap=728，normal_session_gap=414，abnormal_gap=314。
  - 2020 XAU 前段存在大量 weekday 24h 间隔，说明该数据集可能混入非 H4 频率或存在严重缺 bar；这比旧的单一 `large_gap_count` 更早暴露数据风险。

## 5. 测试与真实样本命令

已运行：

```powershell
pytest -q
```

结果：

```text
22 passed in 0.89s
```

真实样本回归命令：

```powershell
python -m examples.run_local_turtle_backtest --symbols XAUUSDc BTCUSDc --timeframe H4 --data-dir data_2022_xau_btc --out-dir outputs\codex_check_2022 --equity 10000 --align-start --align-end --rule-profile h4-daily-equivalent
```

结果摘要：

- elapsed：约 0.95s
- final_equity：16021.95
- orders：162
- trades：72
- max_drawdown：-17.82%

另跑 2020 样本：

```powershell
python -m examples.run_local_turtle_backtest --symbols XAUUSDc BTCUSDc --timeframe H4 --data-dir data_2020_xau_btc --out-dir outputs\codex_check_2020 --equity 10000 --align-start --align-end --rule-profile h4-daily-equivalent
```

结果摘要：

- elapsed：约 1.16s
- final_equity：35809.74
- orders：183
- trades：78
- max_drawdown：-23.91%

说明：

- 本次没有修改策略或回测主循环，因此这些数值差异不是由本次升级引入的交易逻辑变化。
- 当前工作区复跑出的 2022/2020 指标与任务描述中的基线数值不完全一致。建议后续核对：运行命令、是否 align start/end、rule profile、symbol spec、成本参数、数据目录快照是否完全一致。

## 6. 哪些风险仍未覆盖

- XAU 真实 session calendar 仍是近似处理。
  - 当前只稳定识别跨周末 session gap。
  - 节假日、券商服务器夏令时、每日维护窗口仍需要外部交易日历或券商 session 表。
- spread bps 依赖 MT5 `point`。
  - 如果历史 CSV 脱离 symbol spec 单独流转，bps fallback 可能失真。
- 真实订单簿流动性、部分成交、拒单、滑点分布仍未建模。
- funding / borrow carry 只有列存在时才计入，尚无独立极端 funding 场景测试。
- 当前真实样本回归使用结构性范围，不替代逐版本基准产物比对。

## 7. 对当前 2022 回测现象的解释是否更可信

更可信，但边界更清楚：

- 新增 whipsaw 和高成本测试说明：BTC 低胜率、靠少数趋势覆盖小亏的现象可以被测试场景直接触发和观察，不再只依赖真实样本汇总。
- 新增 gap stop golden cases 说明：回测没有把穿 stop 的 gap 以理想 stop 价成交，降低了乐观偏差风险。
- 新增 N regime shift 测试说明：波动率突变会进入 sizing、stop 距离和加仓间距，避免低波动参数在高波动段被测试忽略。
- 新增 XAU/BTC calendar 测试和数据质量拆分说明：XAU 大 gap 中大部分可解释为 session/weekend，但异常 weekday gap 会被单独暴露。
- 新增高成本测试说明：成本恶化不会被测试数据美化，净权益会按成本下降。

这些改动没有证明策略收益更好，也没有修改 XAU short / BTC 成本问题；它们只让这些现象更容易被回归测试和数据质量报告捕捉。

## 8. 后续最值得继续补的 5 个测试项

1. 接入显式 XAU session calendar / holiday calendar，区分周末、节假日、每日维护窗口和真正断档。
2. 增加 spread spike 时间序列测试：只在特定 bar 出现极端 spread，验证交易成本和订单筛选影响。
3. 增加 funding / borrow 极端场景：BTC funding、XAU/CFD swap、空头 borrow 对持仓期 PnL 的影响。
4. 增加 intraday trigger mode 的对称测试：同一根 K 线 high/low 同时穿上下通道时不应产生方向歧义订单。
5. 增加真实样本 golden artifact：保存一小段真实 XAU/BTC 脏数据切片，固定订单结构而不是只用合成数据。
