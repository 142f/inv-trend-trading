# 多资产海龟趋势系统可行性方案

## 总判断

这套系统可以落地，但不应把经典海龟法则原封不动套到所有资产。可行版本应采用：

1. 经典内核：价格突破、N/ATR 波动率归一化仓位、2N 类保护止损、反向突破退出、盈利奔跑。
2. 现代扩展：按资产类别分层、按风险簇限制暴露、把交易成本/滑点/funding/借券成本纳入。
3. 工程约束：第一版采用日线收盘确认、下一可成交窗口执行，盘中 stop-entry 只作为后续实证分支。

## 品种适配

当前代码用 `AssetSpec` 明确每个标的的资产类别、风险簇、可否做空、交易单位、成本和风险预算。建议初始分层：

| 层级 | 标的示例 | 初始结论 |
| --- | --- | --- |
| 宽基 ETF | SPY, QQQ | 最适合作为权益趋势母资产，但要控制与单股的重复 beta |
| 单股票 | NVDA, AMD, MSFT, AAPL 等 | 可做趋势，但必须单独处理财报跳空、借券和集中暴露 |
| 贵金属 | XAU, XAG | XAU 更适配经典框架；XAG 应降低单位风险并限制加仓 |
| 加密 | BTC, ETH | 适合趋势，但要限制杠杆，并显式建模 funding 与周末流动性 |

## 已实现策略代码

新增目录：

- `turtle_multi_asset/strategy.py`：信号、N 计算、仓位 sizing、加仓、退出、组合风险预算。
- `turtle_multi_asset/backtest.py`：日线收盘确认、次日开盘成交的简化事件回测器。
- `examples/run_turtle_demo.py`：合成数据演示。
- `tests/test_turtle_multi_asset.py`：最小行为测试。

## 参数定位

代码中的默认值是候选基线，不是结论。

固定原则：

- 必须使用客观价格规则。
- 必须使用 N/ATR 波动率归一化仓位。
- 必须有保护止损和反向突破退出。
- 必须限制单标的、单风险簇、单方向和组合总热度。

候选参数：

- `fast_entry=20`、`slow_entry=55`
- `fast_exit=10`、`slow_exit=20`
- `n_period=20`
- `stop_n=2.0`
- `pyramid_step_n=0.5`
- `max_units`
- `unit_1n_risk_pct`
- `cluster_1n_risk_pct`

必须回测确认：

- 单股票是否需要慢于 20/55 的窗口。
- XAG 是否必须低于 XAU 的单位风险。
- BTC/ETH 是否减少加仓层数。
- close-confirmed 与 intraday breakout 的收益/成本差异。
- 财报冻结窗口是否真的改善样本外表现。
- 相关簇风险预算的阈值。

## 实盘最容易失真的点

1. 用当前热门股票池回测，产生幸存者偏差和主题偏差。
2. 把 SPY/QQQ 与大型科技股当成独立风险源。
3. 忽略单股财报跳空、停牌、LULD 和借券失败。
4. 忽略 BTC/ETH funding、周末流动性和高杠杆路径依赖。
5. 把 XAU/XAG 的 OTC 点差模型误用成期货模型。
6. 连续止损后主观跳过下一次突破信号。

## 建议落地路径

保守版：

- 先跑 SPY、QQQ、XAU、XAG、BTC、ETH。
- 单股票只做 shadow portfolio。
- 日线收盘确认，次日执行。
- 降低 `unit_1n_risk_pct` 和 `max_units`。

标准版：

- 全品种进入候选池。
- 单股票启用事件冻结、借券检查和单簇风险上限。
- BTC/ETH 单独设置更低加仓层数和 funding 成本。

激进版：

- 研究盘中突破触发。
- 增加加仓层数。
- 允许更多双向衍生品交易。
- 仅在成本压力测试和样本外验证通过后使用。

## 运行方式

```bash
python -m examples.run_turtle_demo
pytest tests/test_turtle_multi_asset.py
```

## 使用 MetaTrader 5 真实数据测试

本项目已新增 MT5 数据适配器：

- `turtle_multi_asset/mt5_data.py`
- `examples/run_mt5_turtle_backtest.py`

前置条件：

1. Windows 上已安装 MetaTrader 5。
2. MT5 终端已启动并登录交易账户或模拟账户。
3. 目标品种已在 Market Watch 中可见。
4. 传入的 symbol 名称必须和券商 MT5 里的名称完全一致，例如有些券商使用 `XAUUSD`，有些使用 `XAUUSD.r`、`GOLD`、`BTCUSD`、`BTCUSDm`。

示例：

```bash
python -m examples.run_mt5_turtle_backtest --symbols XAUUSD XAGUSD BTCUSD ETHUSD --timeframe D1 --count 1200
```

如果不确定券商的真实品种名，先列出匹配项：

```bash
python -m examples.run_mt5_turtle_backtest --list-symbols "*XAU*"
python -m examples.run_mt5_turtle_backtest --list-symbols "*BTC*"
```

指定历史区间：

```bash
python -m examples.run_mt5_turtle_backtest --symbols XAUUSD XAGUSD --timeframe D1 --start 2020-01-01 --end 2026-04-20
```

如果需要指定 MT5 terminal 路径：

```bash
python -m examples.run_mt5_turtle_backtest --terminal-path "C:\Program Files\MetaTrader 5\terminal64.exe" --symbols XAUUSD BTCUSD
```

输出目录默认为：

```text
outputs/mt5_turtle/
```

包含：

- `equity_curve.csv`
- `orders.csv`
- `trades.csv`
- `metrics.json`

注意：MT5 返回的是券商历史数据，不同券商的点差、交易时段、合约规格、加密品种报价和历史深度都可能不同。回测结果只能说明“该券商数据与该成本假设下”的表现，不能直接外推到其他券商或交易所。

替换为真实数据时，每个 DataFrame 至少需要：

```text
open, high, low, close
```

可选列：

```text
event_freeze, funding_rate, borrow_rate
```
