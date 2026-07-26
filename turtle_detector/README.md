# 独立海龟突破检测模块

本模块只识别趋势节点并输出风险信息，不直接提交订单，也不把单次突破表述为
确定性买卖建议。

## 1. 需求与边界

- BTC、ETH 默认对应 Binance 现货 `BTCUSDT_BINANCE_SPOT` 和
  `ETHUSDT_BINANCE_SPOT`。
- XAU、XAG 明确对应现有 DukasCopy `XAUUSD_DUKAS`、`XAGUSD_DUKAS`
  现货/CFD 序列。
- 美股默认使用 Nasdaq 来源、常规交易时段、后复权 OHLC。
- 期货、ETF、永续合约必须注册为不同 `instrument`，不能拼接到默认序列。
- 输入必须是有时区、单调、无重复的时间序列；模块不前向填充价格。
- 检测器依赖抽象数据提供者、状态仓库和通知器，不依赖现有回测或下单引擎。

## 2. 海龟规则定义

所有通道均为 `rolling(period).max/min().shift(1)`：

- System 1：20 周期突破，10 周期反向通道退出；
- System 2：55 周期突破，20 周期反向通道退出；
- 默认收盘确认，可切换为盘中 high/low 触发后等待下一根收盘确认；
- Wilder ATR/N 默认 20 周期；
- 初始止损默认 2N；
- 顺势每 0.5N 加仓，默认最多 3 次；
- System 1 支持前一笔盈利后过滤下一次突破，原始事件仍会被记录。

## 3. 多市场适配

`AssetConfig.identity` 由 instrument、data source、price type、adjustment
组成。输入若声明了其中任一字段，必须与配置完全一致。

| 市场 | 默认周期 | K 线/时区政策 |
|---|---|---|
| 加密现货 | H4、D1 | UTC、7×24，周末有效 |
| DukasCopy 贵金属 CFD | H4、D1 | UTC、FX 24×5 |
| 美股科技股 | D1、W1 | America/New_York、常规交易时段、后复权 |

连续期货需要在外部数据层完成换月和价格调整，并以独立 instrument 接入。
永续合约必须保留 funding 字段并使用独立配置。盘前盘后数据不能与常规时段数据
混合。

## 4. 信号状态设计

状态机：

```text
FLAT
  -> PENDING_CONFIRMATION  (盘中突破)
  -> ENTERED               (收盘突破或确认)
ENTERED
  -> ENTERED               (RETEST_CONFIRMED / PYRAMID_ADD)
  -> EXITED                (Donchian / ATR / 最大持仓退出)
  -> INVALIDATED           (FALSE_BREAKOUT / TREND_INVALIDATED)
```

状态按 `(symbol, timeframe)` 独立保存，包含方向、系统、入场、突破位、ATR、
止损、下一加仓位、加仓次数、持仓 bars、System 1 上次盈亏及最后处理时间。
仓库只在新状态/新信号键出现时通知，重复运行同一根 K 线不会重复告警。

## 5. 模块架构

```text
turtle_detector/
├── config/       严格 YAML 配置与默认资产池
├── data/         Provider 协议、身份/时区/OHLC 校验、拆股调整、日历
├── indicators/   因果 Donchian 与 Wilder ATR
├── signals/      不覆盖原始事件的可选质量过滤器
├── risk/         N 风险单位、初始止损、下一加仓位
├── engine/       扫描器与纯状态转换
├── storage/      内存及 JSON 状态/去重仓库
├── alerts/       Console、JSONL 及可组合通知接口
├── backtest/     单资产、时间顺序验证器与指标
└── cli.py        可由 cron/Task Scheduler 周期调用
```

依赖方向为 `data -> indicators -> engine -> storage/alerts`。数据获取、指标、
判断和通知没有写在同一个文件中。

## 6. 数据接口

输入 DataFrame：

```text
timestamp/index, open, high, low, close, volume
```

标准化后补充：

```text
symbol, instrument, market, timeframe, timezone,
data_source, price_type, adjustment
```

输出 `TurtleSignal` 至少包含：

```json
{
  "symbol": "XAU",
  "instrument": "XAUUSD_DUKAS",
  "market": "precious_metal",
  "timeframe": "D1",
  "signal_type": "SYSTEM1_BREAKOUT",
  "raw_signal_type": "SYSTEM1_BREAKOUT",
  "direction": "long",
  "signal_time": "2026-07-24T00:00:00+00:00",
  "trigger_price": 3412.5,
  "channel_high": 3398.2,
  "channel_low": 3220.1,
  "atr": 47.3,
  "atr_pct": 0.01386,
  "stop_price": 3317.9,
  "next_add_price": 3436.15,
  "distance_to_breakout_atr": 0.3023,
  "volatility_percentile": 0.64,
  "suggested_risk_unit": 0.0086,
  "trend_status": "uptrend",
  "confirmation_status": "close_confirmed",
  "data_source": "dukascopy",
  "tradeable": true,
  "filtered_reasons": [],
  "confidence_note": "原始海龟事件通过可选过滤器；仍不代表确定性盈利。"
}
```

## 7. 核心算法

```text
normalize_and_validate(one instrument, one timeframe)
channel[t] = extrema(bars[t-period:t])       # 不含 t
N[t]       = Wilder(TR[0:t])

if position:
    ATR stop -> channel exit -> false breakout
    -> favorable pyramid -> retest -> structure invalidation
else:
    detect 55 breakout
    else detect 20 breakout
    preserve raw event
    apply optional filters
    classify approaching / overextended / tradeable

persist state
if transition key is new:
    persist signal
    notify adapters
```

多周期扫描逐个调用单周期检测，最后只增加 `timeframe_alignment` 描述，不会
合并或重采样两个周期的价格。

## 8. 风控与加仓

- `stop = entry ± stop_atr * N`；
- `next_add = last_fill ± pyramid_step_atr * N`；
- 只在有利方向到达下一档时加仓；
- unified 模式会把全仓止损向有利方向移动；layered 模式保留分层语义；
- 输出的是风险比例建议，不是交易所 lot 数；
- 实际数量可调用
  `suggested_risk_quantity(equity, risk_fraction, atr, point_value)`；
- 组合总风险、成交时重新校验和保证金属于现有风险/执行模块职责。

## 9. 回测方案

`DetectorBacktester` 逐根扩展历史前缀，不使用随机拆分。它按单资产、单周期
输出 signals、trades、equity curve 和：

- 总收益、CAGR、最大回撤、Sharpe；
- 胜率、盈亏比、平均持仓 bars；
- 信号数量、交易数量、假突破率。

组合比较应在每个资产报告之后进行，不能用组合结果遮蔽失效资产。正式研究还应
使用 walk-forward 窗口、成本/滑点/funding 压力测试和参数邻域稳定性。

## 10. 告警机制

```powershell
python -m turtle_detector.cli `
  --bars processed_data/my_btc_d1.csv `
  --symbol BTC `
  --timeframe D1
```

CLI 使用 JSON 状态仓库、控制台和 JSONL 告警，可由 Windows Task Scheduler、
cron 或数据到达事件调用。邮件、企业微信、Telegram 和数据库只需实现
`Notifier.notify(signal)` 或 `SignalRepository` 协议；模块本身不保存凭证。

## 11. 测试体系

`tests/test_turtle_detector.py` 覆盖：

- shifted Donchian、防当前 K 线进入通道；
- Wilder ATR；
- System 1/2、10/20 退出；
- System 1 盈利过滤；
- 顺势金字塔与不利方向禁止加仓；
- 假突破、盘中到收盘确认；
- 多周期隔离和冲突；
- 时区、instrument 身份、拆股调整；
- 状态/告警去重；
- 缺失、重复、无效 OHLC；
- 添加未来数据不改变历史指标或信号。

## 12. 实施计划

### P0（已实现）

- 严格接口、默认资产配置、因果指标；
- 核心信号/状态/去重；
- JSON/控制台告警；
- 时间顺序验证器和单元测试。

### P1

- Binance、DukasCopy、Nasdaq 生产 Provider；
- SQLite/PostgreSQL repository；
- exchange calendar 与美股盘前盘后明确切分；
- 多周期趋势上下文、相对强弱和资金费率过滤；
- 与现有 `TurtleBacktester` 的 signal adapter。

### P2

- Email/企业微信/Telegram adapter；
- dashboard 与运行健康度监控；
- walk-forward/参数敏感性批处理；
- 实时流式 provider、延迟/断线/stale-data 监控。

## 最容易误判或引入未来函数的风险

1. 未 `shift(1)` 的通道；
2. 先切评估窗口、后计算 ATR/55 周期通道；
3. UTC 日线、交易所日线和纽约收盘线混用；
4. 现货、永续、期货、ETF/CFD 拼接；
5. 美股未复权导致拆股被识别为突破或止损；
6. 盘中 high/low 触发却假设按突破价成交；
7. 同一根 K 线同时触发加仓和止损却没有优先级；
8. 用未来周线收盘确认当前日线；
9. 忽略停牌、财报跳空、换月和 stale data；
10. 只报告组合表现或只在全样本上选参数。

