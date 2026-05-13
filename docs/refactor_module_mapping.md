# 海龟多资产系统模块化重构审计与回测对比

日期: 2026-04-22

## A. 当前仓库结构扫描摘要

本次只把核心源码纳入重构目标，历史报告、历史输出和行情数据只作为理解项目用途与回测验证的参考。

| 类别 | 路径 | 摘要 |
|---|---|---|
| 核心源码目录 | `turtle_multi_asset/` | 策略、回测、MT5 数据适配、规则 profile。重构前 `strategy.py` 过胖，重构后已拆出 `domain.py`、`indicators.py`、`sizing.py`、`engine.py`。 |
| 入口脚本目录 | `examples/` | 包含本地 CSV 回测、MT5 在线回测、D1 多资产实验、数据下载和诊断脚本。部分脚本承载 universe/spec/rules 组装逻辑，后续应下沉到配置层。 |
| 测试目录 | `tests/` | 覆盖指标、仓位 sizing、策略信号、回测撮合、行情场景、MT5 adapter 和真实样本结构回归。缺少更细的规则等价性快照测试。 |
| 文档目录 | `docs/` | 历史审计与回测报告，不作为核心逻辑来源。 |
| 应忽略目录 | `outputs/`、`data_*`、`.venv/`、`.pytest_cache/` | 回测输出、行情数据、本地环境和缓存，不参与重构。 |

## B. 当前模块职责审计表

| 文件路径 | 当前职责 | 存在的问题 | 风险级别 | 建议动作 |
|---|---|---|---|---|
| `turtle_multi_asset/strategy.py` | 兼容导出层，保留旧导入路径 | 重构前同时包含模型、指标、信号、sizing、风险预算，任何局部修改都容易误伤交易行为。已改为兼容层。 | 低 | 仅保留兼容层 |
| `turtle_multi_asset/domain.py` | `AssetSpec`、`TurtleRules`、`PositionUnit`、`Position`、`PortfolioState`、`Order`、`EntrySignal`、方向常量 | 新增模块。后续如果引入持久化或消息协议，需要注意 dataclass 的 `__module__` 已从 `strategy` 变为 `domain`。 | 低 | 提升为核心模块 |
| `turtle_multi_asset/indicators.py` | TR、Wilder N、突破和出场通道计算 | 新增模块。当前仍是单文件指标层，足够支撑现有范围。 | 低 | 提升为核心模块 |
| `turtle_multi_asset/sizing.py` | 1N 风险仓位计算与数量步长向下取整 | 新增模块。命名仍使用私有函数 `_risk_sized_qty` 以保持测试兼容。 | 低 | 提升为核心模块 |
| `turtle_multi_asset/engine.py` | `MultiAssetTurtleStrategy`、入场/加仓/退出信号、订单意图、风险和杠杆预算分配 | 仍同时包含 signals、risk gate 和 order intent 构建。第一阶段保留在一个 engine 文件以降低行为漂移。 | 中 | 拆分 |
| `turtle_multi_asset/backtest.py` | 回测循环、订单成交、止损成交、账户权益、交易明细、绩效指标 | 撮合、持仓状态更新、交易记录和绩效统计仍耦合在一个类里。 | 高 | 拆分 |
| `turtle_multi_asset/mt5_data.py` | MT5 会话、行情抓取、MT5 timeframe、symbol spec 构建、品种画像推断 | 数据接入和 `_infer_asset_fields` 的策略画像/成本/风险参数混层。 | 中 | 拆分 |
| `turtle_multi_asset/profiles.py` | 常用 `TurtleRules` profile 工厂 | profile 硬编码在 Python 中，适合小项目；后续多实验应转为配置或 registry。 | 低 | 保留 |
| `examples/run_local_turtle_backtest.py` | 本地 processed CSV 加载、对齐、spec 构建、回测输出 | `load_asset_specs` 复用 `_infer_asset_fields`，入口脚本承担配置组装。 | 中 | 下沉到子模块 |
| `examples/run_mt5_turtle_backtest.py` | MT5 在线抓取并回测 | 是入口脚本，应只拼装流程，不应承载核心交易逻辑。当前基本符合。 | 低 | 保留 |
| `examples/run_d1_multi_asset_equity_overlay.py` | D1 universe、H4 转 D1、rules、specs、汇总统计 | 实验逻辑和 symbol universe/config 大量硬编码在 example 中。 | 中 | 下沉到子模块 |
| `examples/run_d1_pruned_universe_experiments.py` | 多组剪枝实验和结果汇总 | 复用 overlay 脚本函数，适合保留为实验入口，但 universe 配置应迁出。 | 中 | 移到 examples |
| `examples/run_d1_candidate9_audit.py` | 9 品种候选审计、分段/成本/规则扰动 | 实验脚本合理，但复用了大量本地 helper，后续应复用 config/analytics。 | 中 | 移到 examples |
| `examples/run_eth_single_diagnostics.py` | ETH 单品种诊断 | 诊断逻辑可留在 examples，通用统计函数可后续抽到 analytics。 | 低 | 移到 examples |
| `tests/` | 单元、场景和真实样本回归 | 覆盖了核心行为，但没有对订单 CSV 或交易明细做精确快照等价性测试。 | 中 | 保留 |

## C. 目标模块架构

第一阶段已落地的目录保持平铺，以避免一次性目录大迁移。第二阶段可以再演进为子包。

```text
turtle_multi_asset/
  __init__.py
  strategy.py              # compatibility facade only
  domain.py                # domain dataclasses and constants
  indicators.py            # TR, N, channel calculations
  sizing.py                # unit sizing and quantity rounding
  engine.py                # signal-to-order engine and budget allocation
  backtest.py              # current backtest engine, future split target
  mt5_data.py              # MT5 adapter, future data/registry split target
  profiles.py              # rule profile factories
```

建议的后续目标目录:

| 目录 | 负责什么 | 不负责什么 |
|---|---|---|
| `turtle_multi_asset/domain/` | 领域模型、方向常量、订单意图、组合状态接口 | 不计算指标，不读取行情，不撮合成交 |
| `turtle_multi_asset/indicators/` | ATR/N、通道、规则无关的技术指标 | 不生成订单，不读写状态 |
| `turtle_multi_asset/signals/` | 入场、加仓、退出信号判断 | 不计算仓位，不决定组合预算 |
| `turtle_multi_asset/sizing/` | 根据 equity、N、point value、qty step 算数量 | 不做组合级预算，不撮合 |
| `turtle_multi_asset/risk/` | 总风险、方向风险、cluster 风险、杠杆闸门和优先级 | 不产生原始突破信号 |
| `turtle_multi_asset/positions/` | 持仓和 campaign 状态演进 | 不加载数据，不输出绩效 |
| `turtle_multi_asset/execution/` | 成交、成本、滑点、止损成交模型 | 不生成策略信号 |
| `turtle_multi_asset/portfolio/` | 账户权益、组合风险占用、现金模型 | 不持有 MT5 adapter |
| `turtle_multi_asset/data/` | MT5/CSV/外部数据适配，数据校验 | 不推断策略风险参数 |
| `turtle_multi_asset/config/` | rule profile、asset registry、universe、成本参数 | 不写回测循环 |
| `turtle_multi_asset/analytics/` | metrics、drawdown、交易统计、汇总表 | 不改变订单和成交 |

## D. 现状到目标的迁移映射表

本次已执行的是第一阶段零行为变化迁移。表中“是否必须第一阶段执行”为“是”的项已经完成。

| 现有文件/类/函数/状态 | 建议迁移到 | 迁移动机 | 是否必须第一阶段执行 |
|---|---|---|---|
| `strategy.py: LONG`、`SHORT` | `domain.py` | 方向常量属于领域模型，策略和回测都要共享。 | 是 |
| `strategy.py: AssetSpec` | `domain.py` | 合约、成本、权限、风险预算是跨策略、回测、数据 adapter 的核心模型。 | 是 |
| `strategy.py: TurtleRules` | `domain.py` | 规则配置被 indicators、engine、profiles 共享。 | 是 |
| `strategy.py: PositionUnit` | `domain.py` | 单笔入场单位是持仓和回测成交明细的领域对象。 | 是 |
| `strategy.py: Position` | `domain.py` | 持仓估值、PnL、1N 风险属于领域对象。 | 是 |
| `strategy.py: PortfolioState` | `domain.py` | 策略状态应独立于信号引擎，便于后续集中状态管理。 | 是 |
| `strategy.py: Order` | `domain.py` | 当前是 order intent，不是成交结果，应由策略和回测共享。 | 是 |
| `strategy.py: EntrySignal` | `domain.py` | 入场信号数据结构应从引擎实现中抽离。 | 是 |
| `strategy.py: compute_turtle_indicators` | `indicators.py` | 指标计算无状态，必须独立以便单测和复用。 | 是 |
| `strategy.py: _with_indicators` | `indicators.py` | 指标缓存/补算 helper 属于指标层。 | 是 |
| `strategy.py: _indicator_columns` | `indicators.py` | 指标列名由规则窗口决定，属于指标层内部 helper。 | 是 |
| `strategy.py: _indicator_rules_key` | `indicators.py` | 指标缓存 key 属于指标层。 | 是 |
| `strategy.py: _wilder_average` | `indicators.py` | Wilder 平滑是指标细节。 | 是 |
| `strategy.py: _require_columns` | `indicators.py` | OHLC 输入校验服务于指标计算。 | 是 |
| `strategy.py: _risk_sized_qty` | `sizing.py` | 仓位数量计算应从策略信号中分离。 | 是 |
| `strategy.py: _round_down` | `sizing.py` | 数量步长处理是 sizing helper。 | 是 |
| `strategy.py: MultiAssetTurtleStrategy` | `engine.py` | 第一阶段仍保留信号和预算分配在一个引擎中，避免过度拆分。 | 是 |
| `MultiAssetTurtleStrategy.generate_orders` | `engine.py` | 订单意图生成是 engine 主入口。 | 是 |
| `MultiAssetTurtleStrategy.risk_usage` | 后续 `risk/usage.py`，当前 `engine.py` | 当前用于预算分配，后续应成为风险状态读取接口。 | 否 |
| `MultiAssetTurtleStrategy.leverage_usage` | 后续 `risk/leverage.py`，当前 `engine.py` | 当前与预算分配紧耦合，后续拆出以明确杠杆口径。 | 否 |
| `_entry_signal`、`_breakout_signal` | 后续 `signals/entry.py`，当前 `engine.py` | 入场信号判断应与预算分配分离。 | 否 |
| `_add_order` | 后续 `signals/pyramid.py` 和 `sizing/`，当前 `engine.py` | 加仓触发、数量、订单构建混在一起。 | 否 |
| `_exit_order`、`_exit_signal` | 后续 `signals/exit.py`，当前 `engine.py` | 退出信号和 stop 判断应明确优先级。 | 否 |
| `_allocate_by_budget` | 后续 `risk/allocator.py`，当前 `engine.py` | 风险预算闸门和候选排序应独立测试。 | 否 |
| `_finite_float` | 后续 `utils/numeric.py`，当前 `engine.py` 且 `strategy.py` 兼容导出 | 多处可能复用，但当前只服务策略引擎。 | 否 |
| `backtest.py: BacktestResult` | 保留，后续 `backtest/result.py` | 结果容器可以独立于回测循环。 | 否 |
| `backtest.py: TurtleBacktester.run` | 后续 `engine/backtest_runner.py` | 当前主循环过大，混合执行、账户、信号调用。 | 否 |
| `backtest.py: _execute_orders` | 后续 `execution/fills.py` | 成交和现金更新应成为执行模型。 | 否 |
| `backtest.py: _process_intraday_stops`、`_execute_stop_orders` | 后续 `execution/stops.py` | 止损成交模型应独立回归测试。 | 否 |
| `backtest.py: _apply_entry_fill` | 后续 `positions/service.py` | 持仓状态更新不应藏在回测器。 | 否 |
| `backtest.py: _trade_cost` | 后续 `execution/costs.py` | 成本模型应可替换和独立测试。 | 否 |
| `backtest.py: _metrics` | 后续 `analytics/metrics.py` | 绩效统计不应与撮合循环耦合。 | 否 |
| `mt5_data.py: _infer_asset_fields`、`_asset_fields` | 后续 `config/instrument_registry.py` | 品种画像和风险预算不是数据 adapter 职责。 | 否 |
| `examples/run_local_turtle_backtest.py: load_asset_specs` | 后续 `config/specs.py` | 本地 CSV 入口不应承担 spec 推断。 | 否 |
| `examples/run_d1_multi_asset_equity_overlay.py: build_specs`、`rules_3x` | 后续 `config/universes.py`、`config/profiles.py` | 实验 universe 和风险参数应配置化。 | 否 |

## E. 状态集中管理建议

| 状态名 | 当前可能位置 | 建议集中位置 | 原因 | 错误风险 |
|---|---|---|---|---|
| 持仓状态 | `PortfolioState.positions`，由 `backtest.py` 更新 | `positions/position_book.py` 或 `portfolio/state.py` | 开仓、加仓、退出、估值都依赖同一份持仓。 | 高 |
| 上一次 fast trade 是否盈利 | `PortfolioState.last_fast_trade_won`，在回测 exit 时写入 | `positions/campaign_state.py` | 这是系统选择记忆，不应散落在回测成交细节中。 | 中 |
| 每个 symbol 的 campaign 状态 | 当前隐含在 `Position.system`、`last_add_price`、`last_fast_trade_won` | `positions/campaign_state.py` | fast/slow、加仓锚点、是否跳过 fast 应统一描述。 | 高 |
| 组合风险占用 | `MultiAssetTurtleStrategy.risk_usage` 动态计算 | `risk/risk_book.py` | 预算分配前后应有明确快照。 | 中 |
| 分组风险占用 | `risk_usage["clusters"]` | `risk/risk_book.py` | cluster 限制和 symbol 限制优先级需要可测试。 | 中 |
| 单方向风险占用 | `risk_usage["long"]`、`risk_usage["short"]` | `risk/risk_book.py` | 多空预算冲突应透明。 | 中 |
| 杠杆占用 | `leverage_usage` 动态计算 | `risk/leverage_book.py` 或 `portfolio/exposure.py` | 当前依赖当前 close，数据缺失时会跳过。 | 中 |
| 订单意图 | `Order` | `domain/order.py` | 策略输出的是意图，不是成交。 | 中 |
| 成交结果 | `orders` DataFrame 行、`trades` DataFrame 行 | `execution/fill.py`、`execution/trade_ledger.py` | intent 和 fill 混用会造成审计困难。 | 高 |
| 回测账户状态 | `TurtleBacktester.run` 局部变量 `cash`、`equity_points` | `portfolio/account.py` | cash model、carry、mark-to-market 需要明确边界。 | 高 |

## F. 规则层冲突 / 可疑实现清单

本节只做审计，不把规则问题混进本次结构性重构。

| 问题 | 位置 | 为什么是风险点 | 类型 | 建议怎么处理 |
|---|---|---|---|---|
| `unit_1n_risk_pct` 与 `stop_n=2.0` 口径不同 | `sizing._risk_sized_qty`、`engine._entry_order`、`TurtleRules.stop_n` | 仓位按 1N 预算，保护止损默认 2N，实际 stop loss 近似为 2 倍 unit 预算。字段名已经写 1N，但报告和风控口径容易误读。 | 规则问题 | 在文档和 metrics 中同时报告 1N 风险和 stop-N 风险，不在结构重构中修改。 |
| `max_units`、symbol 1N cap、cluster cap、direction cap、leverage cap 同时生效 | `engine._allocate_by_budget` | 当前优先级由 if 顺序隐含决定，候选按 score 排序后逐个尝试。不同闸门拦截原因没有记录。 | 结构加规则 | 后续风险 allocator 返回 reject reason，并为优先级建立测试。 |
| `trigger_mode="intraday"` 对 entry/add 有影响，但 exit channel 仍用 close | `engine._breakout_signal`、`engine._add_order`、`engine._exit_signal` | 用户可能以为 intraday trigger 同时控制通道退出，但当前只有 stop 是 intraday 处理。 | 规则问题 | 明确规则说明；如要改，单独做规则变更和回测对比。 |
| 入场 intraday 的 signal price 和实际 fill 关系不够直观 | `engine._entry_signal`、`backtest._execute_orders` | intraday 信号可用 high/low 判定，但回测仍在下一根 open 成交。 | 规则问题 | 保持现状；未来引入 execution model 参数。 |
| stop 退出优先于趋势退出，且 stop 在每日循环中先于新信号 | `backtest.run`、`_process_intraday_stops` | 这是合理设计，但优先级应文档化，否则重构 execution 时易改变行为。 | 结构问题 | 为 stop 与 trend exit 同 bar 场景补测试。 |
| end-of-data liquidation 在每个 symbol 自己最后一根 K 线上发生 | `backtest._end_of_data_exit_orders` | 多日历资产中，个别资产结束时会先清算，影响后续组合权益和预算。 | 规则问题 | 保留现状，增加多日历样本回归说明。 |
| `last_fast_trade_won` 只在 fast 仓位退出时更新，slow 不重置 | `backtest._execute_orders`、`_execute_stop_orders` | classic Turtle 的 skip-fast 记忆口径需要确认。slow 交易是否应改变 fast skip 状态当前没有显式说明。 | 规则问题 | 单列规则决策，不在结构重构中修改。 |
| MT5 数据 adapter 推断 asset class、cluster、成本和风险参数 | `mt5_data._infer_asset_fields` | 数据接入层混入策略配置，换 broker 或 symbol 名后可能隐式改变风险画像。 | 结构问题 | 拆到 instrument registry/config，MT5 adapter 只读元数据。 |
| examples 中硬编码 universe 和风险 profile | `examples/run_d1_multi_asset_equity_overlay.py` 等 | 实验脚本成为事实配置源，重复和版本漂移风险高。 | 结构问题 | 后续迁到 `config/universes.py` 或外部 YAML/JSON。 |

## G. 最小可落地重构顺序

| 阶段 | 改动目标 | 预计影响范围 | 回归风险 | 需要补的测试 |
|---|---|---|---|---|
| 第 1 阶段，已完成 | 抽出 `domain.py`、`indicators.py`、`sizing.py`、`engine.py`，`strategy.py` 保留兼容层 | 核心导入路径和 `backtest.py` 依赖 | 低，纯搬迁 | 已跑全量 tests 和前后回测文件哈希对比 |
| 第 2 阶段 | 把 `engine.py` 拆成 `signals/`、`risk/`、`orders/` | `MultiAssetTurtleStrategy` 内部 | 中 | 对 entry/add/exit、reject reason、预算优先级做单测 |
| 第 3 阶段 | 抽离 `backtest.py` 的 execution、position update、account、metrics | 回测主循环 | 高 | 订单、交易明细、equity curve 快照等价性测试 |
| 第 4 阶段 | 把 MT5 `_infer_asset_fields` 和 examples 的 `build_specs/rules_3x` 下沉到 config | 数据 adapter 和实验脚本 | 中 | spec registry 输入输出测试，旧脚本回测等价性测试 |
| 第 5 阶段 | 清理 examples 复用统计函数，补 analytics | examples 和报告输出 | 低 | summary/metrics 函数测试 |

## H. 实际产物和重构说明

本次实际改动文件:

| 文件 | 重构内容 | 行为影响 |
|---|---|---|
| `turtle_multi_asset/domain.py` | 新增领域模型和方向常量 | 零交易逻辑变化 |
| `turtle_multi_asset/indicators.py` | 新增指标计算函数和指标 helper | 零交易逻辑变化 |
| `turtle_multi_asset/sizing.py` | 新增 1N 风险仓位计算 helper | 零交易逻辑变化 |
| `turtle_multi_asset/engine.py` | 承载原 `MultiAssetTurtleStrategy` 的订单生成和预算分配 | 代码搬迁，逻辑保持原样 |
| `turtle_multi_asset/strategy.py` | 改为兼容 facade，继续导出旧 API 和旧私有 helper 名称 | 保持 `from turtle_multi_asset.strategy import _risk_sized_qty` 等旧导入可用 |
| `turtle_multi_asset/backtest.py` | 改为从 `domain.py`、`engine.py`、`indicators.py` 导入 | 导入路径变化，回测逻辑未改 |
| `turtle_multi_asset/mt5_data.py` | `AssetSpec` 改从 `domain.py` 导入 | 导入路径变化，逻辑未改 |
| `turtle_multi_asset/profiles.py` | `TurtleRules` 改从 `domain.py` 导入 | 导入路径变化，逻辑未改 |
| `turtle_multi_asset/__init__.py` | 顶层 API 改从新模块导入 | 对外导出名称不变 |

验证方式:

1. 重构前运行 `python -m pytest`: `22 passed`
2. 重构前运行基准回测:

```text
$env:PYTHONPATH='.'; python examples/run_local_turtle_backtest.py --symbols XAUUSDc BTCUSDc --timeframe H4 --data-dir data_2022_xau_btc --out-dir outputs/refactor_compare_before --equity 10000 --align-start --align-end --rule-profile h4-daily-equivalent
```

3. 重构后运行 `python -m pytest`: `22 passed`
4. 重构后运行同一基准回测到 `outputs/refactor_compare_after`
5. 对比 `metrics.json`、`equity_curve.csv`、`orders.csv`、`trades.csv`、`trade_details.csv` 的 SHA256，全部一致。

## I. 重构前后回测结果对比

样本:

| 项目 | 值 |
|---|---|
| 数据目录 | `data_2022_xau_btc` |
| symbols | `XAUUSDc`、`BTCUSDc` |
| timeframe | `H4` |
| 规则 profile | `h4-daily-equivalent` |
| 初始权益 | `10000` |
| 对齐方式 | `--align-start --align-end` |
| XAU 数据范围 | `2022-01-02 20:00:00+00:00` 到 `2026-04-20 00:00:00+00:00`，`6865` bars |
| BTC 数据范围 | `2022-01-02 20:00:00+00:00` 到 `2026-04-20 00:00:00+00:00`，`9410` bars |

指标对比:

| 指标 | 重构前 | 重构后 | 差值 |
|---|---:|---:|---:|
| final_equity | 16021.9548020957 | 16021.9548020957 | 0 |
| total_return | 0.602195 | 0.602195 | 0 |
| cagr | 0.116057 | 0.116057 | 0 |
| max_drawdown | -0.178232 | -0.178232 | 0 |
| volatility | 0.158405 | 0.158405 | 0 |
| sharpe_like | 0.772582 | 0.772582 | 0 |
| mar | 0.651158 | 0.651158 | 0 |
| trade_count | 72 | 72 | 0 |
| orders | 162 | 162 | 0 |

文件级对比:

| 文件 | 是否完全一致 | SHA256 |
|---|---|---|
| `metrics.json` | 是 | `379da21e37ab7bf6e9a01ba5fb271ecc089f6a3134cd24f1bfb4cf78f50f1337` |
| `equity_curve.csv` | 是 | `338b3bc0740133ff24bd01fa3e5612292f0f7b16a4c01c30ef0f909b81af5585` |
| `orders.csv` | 是 | `a2c7191f8359aba04a875cced39936484b3b01d22aca3124cb5885e7f99a23dd` |
| `trades.csv` | 是 | `a54387938fc703c7d2150fa4584412fe789c75bfe15185accde29e03e68cddf1` |
| `trade_details.csv` | 是 | `669f775581fcbe402f26cfbb3d53c5048dd1df185011824926ed243ad847fc40` |

结果分析:

本次重构没有改变订单数量、成交序列、交易明细、权益曲线或绩效指标。重构前后输出文件字节级完全一致，说明本次属于零行为变化的结构拆分。

交易结果本身显示，该样本期最终权益为 `16021.9548020957`，总收益约 `60.22%`，最大回撤约 `-17.82%`，交易数 `72`。按交易退出类型统计，`stop` 为 `56` 笔，`trend_exit` 为 `16` 笔；按品种净 PnL 统计，`BTCUSDc` 约 `1383.453857`，`XAUUSDc` 约 `4638.500945`。这些是策略行为结果，不是本次重构带来的变化。

结论:

第一阶段重构达成目标: `strategy.py` 从过胖核心文件变为兼容导出层，领域模型、指标、sizing 和策略引擎获得明确边界，同时保持回测结果完全一致。后续如果继续拆 `engine.py` 或 `backtest.py`，必须继续使用同样的文件级回测快照对比，避免把规则修正混入结构重构。

## J. D1 候选组合完整回测对比补充

上一节的 XAU/BTC H4 回测只适合作为轻量结构回归，不足以代表当前主研究口径。按现有历史审计输出，本次追加使用 `examples/run_d1_candidate9_audit.py` 做完整 D1 反过拟合审计对比。

对比口径:

| 项目 | 重构前基准 | 重构后输出 |
|---|---|---|
| 脚本 | `examples/run_d1_candidate9_audit.py` | `examples/run_d1_candidate9_audit.py` |
| 初始权益 | `10000` | `10000` |
| 重构前输出目录 | `outputs/d1_candidate9_audit/` | - |
| 重构后输出目录 | - | `outputs/refactor_compare_d1_candidate9_after/` |
| 主候选判断 | `minus_eth_short`，即踢掉 ETH short 后 8 品种 | 同左 |

8 品种候选:

```text
XAUUSD_DUKAS
XAGUSD_DUKAS
BTCUSDT_BINANCE
NVDA
AMD
MU
TSM
AVGO
```

方向约束:

| 品种 | 方向 |
|---|---|
| XAU / XAG | long only |
| BTC | long/short |
| NVDA / AMD / MU / TSM / AVGO | long only |
| ETH | 从主候选移除 |

重构前后数值一致性:

| 文件 | 形状 | 非数值列差异 | 最大数值绝对差 |
|---|---:|---:|---:|
| `minus_eth_short/equity_curve.csv` | `3169 x 2` | 0 | `9.31322574615e-10` |
| `minus_eth_short/orders.csv` | `599 x 14` | 0 | `1.16415321827e-10` |
| `minus_eth_short/trades.csv` | `198 x 28` | 0 | `3.05590219796e-10` |
| `minus_eth_short/trade_details.csv` | `401 x 22` | 0 | `3.05590219796e-10` |

说明: D1 完整审计输出不是所有文件字节级一致，差异来自浮点 CSV/JSON 序列化尾数；订单行数、交易行数、非数值字段、核心指标均一致，数值差异在 `1e-10` 量级，不构成策略行为变化。

### J1. 主组合对比

| 组合 | final_equity | CAGR | maxDD | Sharpe-like | MAR | trades |
|---|---:|---:|---:|---:|---:|---:|
| 原 9 品种 `base_9` | 1,497,084.87 | 78.15% | -50.14% | 1.383 | 1.559 | 217 |
| 踢掉 ETH short 后 8 品种 `minus_eth_short` | 1,625,439.31 | 79.85% | -46.98% | 1.417 | 1.700 | 198 |
| 之前 20 品种参考 `reference_previous20` | 386,067.67 | 52.38% | -51.05% | 1.104 | 1.026 | 408 |

结论: D1 主研究口径下，踢掉 ETH short 后的 8 品种组合仍优于原 9 品种。它提升 final equity、CAGR、Sharpe-like、MAR，同时降低最大回撤和交易数。ETH short 不是必要腿，保留它反而拖累组合质量。

### J2. 单资产剔除

| 实验 | final_equity | CAGR | maxDD | Sharpe-like | MAR | trades | 判断 |
|---|---:|---:|---:|---:|---:|---:|---|
| `minus_xag` | 702,680.52 | 63.27% | -42.04% | 1.274 | 1.505 | 204 | XAG 提高收益，但也提高集中度 |
| `minus_xau` | 586,981.90 | 59.92% | -52.60% | 1.162 | 1.139 | 211 | XAU 比表面贡献更重要 |
| `minus_btc` | 163,009.28 | 37.96% | -49.72% | 1.130 | 0.763 | 175 | BTC 不可替代 |
| `minus_eth_short` | 1,625,439.31 | 79.85% | -46.98% | 1.417 | 1.700 | 198 | ETH short 应该踢掉 |
| `minus_nvda` | 682,229.21 | 62.72% | -56.51% | 1.281 | 1.110 | 203 | NVDA 重要 |
| `minus_amd` | 660,021.69 | 62.10% | -50.55% | 1.255 | 1.229 | 209 | AMD 有效 |
| `minus_mu` | 1,063,502.99 | 71.27% | -49.76% | 1.335 | 1.432 | 207 | MU 有效，但不是唯一大腿 |
| `minus_tsm` | 923,000.55 | 68.49% | -44.47% | 1.305 | 1.540 | 208 | TSM 可保留，但不是不可替代 |
| `minus_avgo` | 1,197,496.39 | 73.62% | -52.56% | 1.358 | 1.401 | 205 | AVGO 有帮助 |

### J3. 风险簇剔除

| 实验 | final_equity | CAGR | maxDD | Sharpe-like | MAR | trades | 判断 |
|---|---:|---:|---:|---:|---:|---:|---|
| `minus_metals` | 597,097.35 | 60.24% | -40.07% | 1.237 | 1.503 | 180 | 金属提高收益，但集中风险明显 |
| `minus_crypto` | 146,164.31 | 30.77% | -50.26% | 1.037 | 0.612 | 182 | 加密簇不可替代 |
| `minus_semis` | 286,418.37 | 47.23% | -57.00% | 1.054 | 0.828 | 113 | 半导体簇不可替代 |
| `minus_eth_short` | 1,625,439.31 | 79.85% | -46.98% | 1.417 | 1.700 | 198 | ETH short 不需要 |

结论: 真正核心是 BTC、半导体、金属三块，ETH short 不是核心风险腿。

### J4. 分段和滚动窗口

| 实验 | final_equity | CAGR | maxDD | Sharpe-like | MAR | trades |
|---|---:|---:|---:|---:|---:|---:|
| `segment_1_2017_2019` | 57,460.72 | 109.06% | -40.65% | 1.609 | 2.683 | 70 |
| `segment_2_2020_2022` | 56,472.54 | 78.34% | -41.51% | 1.368 | 1.887 | 81 |
| `segment_3_2023_2026` | 40,603.00 | 53.03% | -48.01% | 1.162 | 1.104 | 106 |
| `roll_2021_2026` | 17,057.02 | 10.62% | -44.11% | 0.457 | 0.241 | 161 |

结论: 三个固定分段均盈利，但 `2023-2026` 明显弱于前两段；`roll_2021_2026` 是最重要警告，说明全样本强表现依赖早期趋势和复利路径，不能只看全样本权益曲线。

### J5. 成本压力测试

| 实验 | final_equity | CAGR | maxDD | Sharpe-like | MAR | trades |
|---|---:|---:|---:|---:|---:|---:|
| `base_9` | 1,497,084.87 | 78.15% | -50.14% | 1.383 | 1.559 | 217 |
| `cost_1p5x` | 1,401,882.33 | 76.81% | -51.47% | 1.368 | 1.492 | 217 |
| `cost_2x` | 1,311,339.83 | 75.45% | -52.76% | 1.352 | 1.430 | 217 |

结论: 成本压力测试通过。2 倍成本下仍显著强于 20 品种参考，但回撤略扩大，MAR 从 `1.559` 降到 `1.430`。

### J6. 参数扰动

| 实验 | final_equity | CAGR | maxDD | Sharpe-like | MAR | trades | 判断 |
|---|---:|---:|---:|---:|---:|---:|---|
| `entry_18_50` | 928,342.10 | 68.60% | -52.29% | 1.305 | 1.312 | 232 | 通过但变弱 |
| `entry_22_60` | 878,795.60 | 67.54% | -46.39% | 1.314 | 1.456 | 222 | 通过 |
| `exit_9_18` | 997,744.63 | 70.01% | -49.80% | 1.296 | 1.406 | 221 | 通过 |
| `exit_12_24` | 1,173,692.48 | 73.22% | -51.53% | 1.335 | 1.421 | 213 | 通过 |
| `stop_1p8n` | 334,788.43 | 49.90% | -46.48% | 1.106 | 1.074 | 232 | 明显警告 |
| `stop_2p2n` | 1,153,419.44 | 72.88% | -46.30% | 1.311 | 1.574 | 209 | 通过 |

结论: 入场和出场窗口不是单点尖峰，但止损收紧到 `1.8N` 会明显变差，说明策略对过紧止损敏感。后续不能把止损参数优化混入结构重构。

### J7. 完整回测补充结论

D1 完整反过拟合审计支持把候选从 9 品种修正为 8 品种强候选:

```text
XAU / XAG / BTC / NVDA / AMD / MU / TSM / AVGO
```

但它仍不是可以直接定版的低风险组合，主要原因是:

| 风险点 | 说明 |
|---|---|
| 收益集中度高 | `base_9` 的 top1 贡献约 `46.38%`，top3 贡献约 `78.23%`；`minus_eth_short` 的 top1 贡献约 `46.31%`，top3 贡献约 `75.46%`。 |
| 起点敏感 | `roll_2021_2026` 只有 `10.62%` CAGR，MAR 只有 `0.241`。 |
| 核心簇不可替代 | 去掉 BTC 或半导体后组合质量大幅下降。 |
| 金属 cap 敏感 | 金属贡献收益，但也带来仓位和趋势依赖。 |

下一步建议不再优先加品种，而是围绕 8 品种候选做仓位强度、金属 cap、BTC cap、start-date sensitivity 和滚动窗口稳健性测试。
