# 趋势 / 海龟交易测试与数据质量审计

审计日期：2026-04-20

## 1. 关键文件清单

### 策略实现

- `turtle_multi_asset/strategy.py`
  - `compute_turtle_indicators`：计算 TR、Wilder N、入场/退出通道，通道全部 `shift(1)`，避免当前 K 线泄漏。
  - `MultiAssetTurtleStrategy.generate_orders`：基于当前已完成 K 线 snapshot 生成下一根 K 线开盘执行的订单。
  - `_entry_signal` / `_entry_order`：处理 fast/slow breakout、fast skip、方向约束、风控 sizing。
  - `_add_order`：处理 0.5N 加仓。
  - `_exit_order`：处理收盘跌破/突破 stop 与反向通道退出。
  - `PortfolioState.last_fast_trade_won`：承载 Turtle System 1 的获利后 skip 规则。

### 回测主循环

- `turtle_multi_asset/backtest.py`
  - `TurtleBacktester.run`：统一多资产 calendar，先执行 pending order，再处理 intraday stop，再 carry cost，再盯市，再生成新订单。
  - `_execute_orders`：普通订单以下一根 K 线 `open` 成交。
  - `_process_intraday_stops` / `_execute_stop_orders`：处理 bar 内止损；gap 穿 stop 时使用更不利的开盘价，且通过 `Order.forced_fill_price` 传递，不写回 DataFrame。
  - `_mark_equity`：闭市资产使用 `_last_price_on_or_before` 的最近可用收盘价盯市。
  - `_snapshots_through` / `_tradable_symbols`：支持 XAU 与 BTC 不同交易日历；无当前 bar 或无下一根可成交 bar 的品种不生成新订单。
  - `_index_pos` / `_records`：当前性能优化的重要缓存，不应破坏。

### 指标计算

- `turtle_multi_asset/strategy.py`
  - `compute_turtle_indicators`
  - `_wilder_average`
  - `_indicator_columns`

### 测试数据生成器

- `tests/test_turtle_multi_asset.py`
  - `_trend_bars`：当前唯一通用合成行情生成器，生成平滑单边趋势。
- `examples/run_turtle_demo.py`
  - `make_ohlc`：随机合成示例数据，仅用于 demo，不是测试 fixture。

### 数据质量检查

- `examples/download_mt5_data.py`
  - `normalize_ohlc`：去重、排序、过滤非正价格与 bad OHLC。
  - `data_quality`：当前输出 bar 数、重复数、bad OHLC、大 gap 数、median/max spread。

### 真实数据下载 / 运行脚本

- `turtle_multi_asset/mt5_data.py`
  - MT5 timeframe、session、OHLC 拉取、symbol spec 推断。
- `examples/download_mt5_data.py`
  - 下载 raw / processed CSV，生成 `metadata/mt5/data_quality_report.csv`。
- `examples/run_local_turtle_backtest.py`
  - 读取本地 processed CSV，支持 `--align-start` / `--align-end` 和 `h4-daily-equivalent` profile。
- `examples/run_mt5_turtle_backtest.py`
  - 直接连接 MT5 拉取并回测。

## 2. 当前测试覆盖了什么

- 指标窗口使用上一根及更早 K 线，避免 breakout level 使用当前 K 线。
- Wilder N 的基础递推公式。
- 风险 sizing 在最小数量边界上的行为。
- 策略能对简单上涨趋势发出多头 breakout 订单。
- 回测器能跑通并生成 equity curve、metrics、trade。
- 闭市资产盯市时使用最近可用价格。
- MT5 symbol 类型推断和 UTC 时间转换。
- `normalize_ohlc` 能过滤 duplicate / bad OHLC。
- `data_quality` 能输出基础 bar 数和 raw spread 中位数。
- 当前基线：`pytest -q` 为 11 passed。

## 3. 当前测试缺了什么

- 缺少空头单边趋势与多头趋势的对称性校验。
- 缺少连续假突破 / whipsaw 场景，无法验证反复小亏后的状态是否正确。
- 缺少 gap 穿 stop 的手工可验证 golden case，尤其是开盘价比 stop 更差时的成交价。
- 缺少空头 gap stop 的对称 golden case。
- 缺少 volatility regime shift 场景，无法验证 N 变化对 sizing、stop 距离和加仓间距的影响。
- 混合日历只覆盖了盯市，没有覆盖“只有 BTC 有 bar 时 XAU 不应生成新订单”的逻辑。
- 缺少高 spread / 高 slippage / 高成本压力测试。
- 缺少真实样本的结构性回归测试；当前更多证明“能跑”，不足以证明关键真实样本形态仍然合理。
- 数据质量测试没有覆盖 market type、正常 session gap 与异常断档的区别。

## 4. 过于理想化的测试数据

- `_trend_bars` 是线性、平滑、无 gap、无成本恶化、无缺失 bar 的理想趋势。
- 当前测试没有 XAU 周末休市、BTC 7x24、交易时段错位、weekday 异常断档。
- 当前测试没有 spread spike，也没有把成本恶化反映到 PnL 断言中。
- 当前 demo 随机数据不是稳定 golden case，无法人工推导具体入场、加仓、止损价格。

## 5. 未进入测试的真实市场风险

- XAU 周末 / 休市导致的正常长间隔被误判为异常数据断档。
- BTC 应接近 24x7 连续，任何 H4 长间隔都更可能是数据缺失。
- 突破后一根开盘大幅跳空，普通订单 fill 与 stop fill 都可能远离 signal price。
- gap 穿止损时，如果用理想 stop 价而不是不利开盘价，会系统性高估回测。
- ATR/N 在低波动到高波动之间突变，可能导致 sizing、加仓间距、止损距离变化不符合预期。
- 高成本品种或极端 spread 时，策略仍能产生交易，但净收益应被成本真实压低。
- 多资产 calendar 下，缺 bar 的品种如果仍生成订单，会制造不可成交或未来污染。

## 6. 最可能导致“回测看起来对，实盘容易出问题”的位置

- `examples/download_mt5_data.py::data_quality` 的 gap 统计：当前只用 median gap 倍数判断，不能区分 XAU 正常休市和异常断档。
- spread 统计只有 raw median/max，缺少 bps 口径；跨 XAU/BTC 比较时 raw spread 不可直接比较。
- 现有单边上涨测试会掩盖空头路径、反向退出、连续假突破和 stop fill 的问题。
- 如果只看 final equity，可能漏掉交易结构异常，例如订单数量、止损退出比例、多空覆盖、成本占比异常。
- `tradable_symbols` 是混合日历正确性的关键保护，但当前只间接测试了盯市，没有直接验证不开市品种的新订单抑制。

## 7. 最小可行改动计划（按优先级）

1. 新增手工可验证 golden cases：
   - long gap stop：多头持仓后下一根开盘低于 stop，成交价应为更差开盘价。
   - short gap stop：空头持仓后下一根开盘高于 stop，成交价应为更差开盘价。
   - long/short 单边趋势对称：入场、加仓、end-of-test 退出与 PnL 对称。
2. 新增场景级测试：
   - 连续 whipsaw 导致多笔小亏，fast state 不应卡死。
   - volatility regime shift 导致 N 上升、qty 下降、stop 距离扩大、加仓更难触发。
   - BTC-only bar 时，XAU snapshot 存在但不在 tradable set，不应生成 XAU 新订单。
   - 高成本场景下总成本上升，净 equity 低于零成本场景。
3. 增强数据质量逻辑：
   - 增加 `market_type`：BTC/ETH 等 crypto 为 `24x7`，XAU/XAG/Gold/Silver 等为 `session`。
   - 增加 timeframe 对应 expected bar gap。
   - 同时输出 `large_gap_count`、`normal_session_gap_count`、`abnormal_gap_count`，避免把 XAU 周末正常停盘当异常。
   - 增加 `median_spread_bps`、`p95_spread_bps`、`max_spread_bps`；MT5 下载时尽量用 symbol `point` 把 raw spread points 转成价格。
4. 新增真实样本结构性回归测试：
   - 对 `data_2022_xau_btc` 使用 H4 daily-equivalent、align start/end、初始权益 10000。
   - 使用宽范围和结构性断言，不做脆弱绝对相等。
5. 保留策略交易规则与回测热路径：
   - 不修改 `strategy.py` / `backtest.py` 的交易规则。
   - 如需改动，只允许在发现 bug 后单独说明并用结果一致性与性能证明。

## 不确定项

- XAU 的真实休市窗口会随券商服务器、夏令时、节假日变化。当前最小实现只能稳定识别周末/跨周末 session gap；节假日异常需要接入交易所/券商 session calendar 后进一步细分。
- MT5 `spread` 原始单位通常是 points；如果没有 symbol `point`，只能按 price unit 估算 spread bps，跨品种比较可能失真。下载脚本应在可获得 `symbol_info.point` 时传入 point。
