# XAU/BTC MT5 最早 H4 数据 3x 回测报告

生成日期：2026-04-20

## 更正说明

上一版 `outputs/xau_btc_mt5_earliest_h4_common` 是用 `examples.run_local_turtle_backtest` 的默认 `h4-daily-equivalent` profile 跑出来的。这个 profile 不是 3x 方案：

| 约束 | 默认 profile | 3x 方案 |
|---|---:|---:|
| 组合总杠杆上限 | 1.5x | 3.0x |
| 单方向杠杆上限 | 1.2x | 2.0x |
| precious_metals 类别杠杆 | 1.0x | 1.5x |
| crypto 类别杠杆 | 0.5x | 1.5x |
| 总 1N 风险上限 | 8% | 12% |
| 单方向 1N 风险上限 | 6% | 8% |
| XAU/BTC 类别 1N 风险 | 2.5% / 1.5% | 4% / 4% |

所以结果变差不是代码或数据变了，而是回测口径错了。按之前的 3x 类别杠杆方案重跑后，结果重新回到 `71524.72`。

## 数据

数据目录：`data_2010_xau_btc`

| symbol | timeframe | MT5 实际起点 | 终点 | bars |
|---|---|---:|---:|---:|
| BTCUSDc | H4 | 2018-02-09 08:00 UTC | 2026-04-20 00:00 UTC | 17925 |
| XAUUSDc | H4 | 2018-06-28 00:00 UTC | 2026-04-20 00:00 UTC | 8607 |

共同样本回测区间：`2018-06-28 00:00 UTC -> 2026-04-20 00:00 UTC`。

## 3x 回测配置

输出目录：`outputs/xau_btc_mt5_earliest_h4_3x`

| 项目 | 配置 |
|---|---|
| strategy | multi-asset turtle |
| timeframe | H4 |
| rule profile | H4 daily equivalent: 120/330 entry, 60/120 exit, N=120 |
| initial_equity | 10000 |
| symbols | XAUUSDc, BTCUSDc |
| direction | long + short |
| 组合总杠杆 | 3.0x |
| 单方向杠杆 | 2.0x |
| XAU / BTC 类别杠杆 | 1.5x / 1.5x |
| 总 1N 风险 | 12% |
| 单方向 1N 风险 | 8% |
| XAU / BTC 类别 1N 风险 | 4% / 4% |
| 单品种杠杆 | 1.5x |
| 单品种 1N 风险 | 4% |

## 3x 结果

| 指标 | 数值 |
|---|---:|
| 初始权益 | 10000.00 |
| 最终权益 | 71524.72 |
| 总收益 | 615.25% |
| CAGR | 28.64% |
| 最大回撤 | -29.23% |
| 年化波动 | 26.82% |
| Sharpe-like | 1.073 |
| MAR | 0.980 |
| trades | 119 |

订单结构：

| action | 数量 |
|---|---:|
| open | 119 |
| add | 65 |
| exit | 119 |

## 对比：默认 profile 不是 3x

| 口径 | 输出目录 | final_equity | total_return | CAGR | max_drawdown | trades |
|---|---|---:|---:|---:|---:|---:|
| 默认 `h4-daily-equivalent` | `outputs/xau_btc_mt5_earliest_h4_common` | 35512.38 | 255.12% | 17.61% | -27.85% | 102 |
| 3x 类别杠杆方案 | `outputs/xau_btc_mt5_earliest_h4_3x` | 71524.72 | 615.25% | 28.64% | -29.23% | 119 |

## 结论

- 代码没有改，数据也没有变。
- 刚才结果变差的原因是我使用了默认保守 profile，而不是你之前要求的 3x 类别杠杆方案。
- 正确 3x 口径已经重跑并复现旧结果：`10000 -> 71524.72`。
- 旧的 `outputs/codex_3x_2010` 和新的 `outputs/xau_btc_mt5_earliest_h4_3x` 指标一致。
