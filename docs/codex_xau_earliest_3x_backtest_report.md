# XAUUSDc 最早可用 MT5 数据 3x 单品种回测报告

生成日期：2026-04-20

## 1. 数据处理

请求命令：

```powershell
python -m examples.download_mt5_data --symbols XAUUSDc --timeframe H4 --start 2000-01-01 --end 2026-04-20 --data-dir data_xau_earliest
```

说明：

- 先尝试 `1970-01-01`，MT5 返回 `Invalid params`。
- 改用 `2000-01-01` 后成功。
- MT5 实际返回的最早 XAUUSDc H4 数据为 `2018-06-28 00:00:00+00:00`。

生成目录：

```text
data_xau_earliest/
  raw/mt5/XAUUSDc/H4.csv
  processed/mt5/H4/XAUUSDc.csv
  metadata/mt5/download_log.csv
  metadata/mt5/symbol_specs.csv
  metadata/mt5/data_quality_report.csv
```

## 2. 数据质量

| symbol | timeframe | market_type | bars | start | end |
|---|---|---|---:|---|---|
| XAUUSDc | H4 | session | 8607 | 2018-06-28 00:00 UTC | 2026-04-20 00:00 UTC |

质量字段：

| duplicate | bad_ohlc | large_gap | normal_session_gap | abnormal_gap | expected_gap_minutes | median_gap_hours | max_gap_hours |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0 | 1196 | 571 | 625 | 240 | 4 | 96 |

spread：

| median_spread | max_spread | median_spread_bps | p95_spread_bps | max_spread_bps |
|---:|---:|---:|---:|---:|
| 0.0 | 468.0 | 0.00 | 0.82 | 1.78 |

数据质量结论：

- OHLC 本身无重复、无 bad row。
- 但 XAU 是 session market，长样本里有大量 gap。
- `625` 个 abnormal gap 需要谨慎看待；这意味着 XAU 单品种长样本结果不能只看收益。

## 3. 回测配置

输出目录：

```text
outputs/codex_3x_xau_earliest
```

回测口径：

| 项目 | 配置 |
|---|---|
| symbol | XAUUSDc |
| timeframe | H4 |
| rule profile | h4-daily-equivalent |
| initial_equity | 10000 |
| allow short | true |
| total leverage cap | 3.0x |
| direction leverage cap | 3.0x |
| XAU / precious_metals cap | 3.0x |
| total 1N risk cap | 12% |
| direction 1N risk cap | 12% |
| XAU 1N risk cap | 12% |

说明：

- 本次是单品种 XAU 测试，因此没有把 precious metals 限制在之前多资产方案的 1.5x。
- XAU 单品种允许最高 3x。

## 4. 总体回测结果

| 指标 | 数值 |
|---|---:|
| 初始权益 | 10000.00 |
| 最终权益 | 23925.64 |
| 最低权益 | 7226.75 |
| 最高权益 | 29625.57 |
| total_return | 139.26% |
| CAGR | 11.82% |
| max_drawdown | -34.57% |
| volatility | 21.16% |
| sharpe_like | 0.634 |
| MAR | 0.342 |
| orders | 148 |
| trades | 48 |

订单结构：

| action | 数量 |
|---|---:|
| open | 48 |
| add | 52 |
| exit | 48 |

## 5. 分多空

| side | trades | net_pnl | gross_pnl | cost | avg_pnl | win_rate | avg_holding_bars | max_win | max_loss |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| long | 38 | 14941.53 | 15393.27 | 451.73 | 393.20 | 15.79% | 69.71 | 12967.42 | -161.82 |
| short | 10 | -1015.89 | -897.22 | 118.67 | -101.59 | 10.00% | 28.80 | 16.79 | -179.67 |

结论：

- XAU 单品种收益几乎全部来自 long。
- short 仍然是拖累项。
- short 只有 1 笔微盈利，整体净亏 `-1015.89`。

## 6. 退出类型

| exit_type | trades | net_pnl | win_rate | avg_pnl |
|---|---:|---:|---:|---:|
| stop | 42 | -3726.73 | 4.76% | -88.73 |
| trend_exit | 6 | 17652.37 | 83.33% | 2942.06 |

结论：

- 典型趋势系统结构：大量止损小亏，少数趋势退出贡献全部收益。
- 只有 6 笔 trend_exit，却贡献 `17652.37`。

## 7. 系统表现

| system | trades | net_pnl | win_rate | avg_pnl |
|---|---:|---:|---:|---:|
| fast | 1 | 164.29 | 100.00% | 164.29 |
| slow | 47 | 13761.35 | 12.77% | 292.79 |

slow system 是主要收益来源。

## 8. 最大盈利与亏损

最大盈利交易：

| side | entry_time | exit_time | units | pnl | exit_reason |
|---|---|---|---:|---:|---|
| long | 2025-08-29 16:00 UTC | 2026-03-18 12:00 UTC | 3 | 12967.42 | long_exit_120d_low |
| long | 2025-02-03 16:00 UTC | 2025-05-14 16:00 UTC | 3 | 2861.58 | long_exit_120d_low |
| long | 2024-03-04 16:00 UTC | 2024-06-07 16:00 UTC | 3 | 1646.21 | long_exit_120d_low |

最大亏损交易：

| side | entry_time | exit_time | units | pnl | exit_reason |
|---|---|---|---:|---:|---|
| short | 2022-09-23 12:00 UTC | 2022-09-28 12:00 UTC | 3 | -179.67 | intraday_stop |
| short | 2026-03-23 04:00 UTC | 2026-03-23 08:00 UTC | 2 | -171.13 | intraday_stop |
| long | 2021-11-10 16:00 UTC | 2021-11-12 08:00 UTC | 3 | -161.82 | intraday_stop |

## 9. 实际杠杆

| 指标 | 最大值 |
|---|---:|
| total_lev | 3.030x |
| long_lev | 3.030x |
| short_lev | 2.745x |

说明：

- 风控接受订单时按当时权益、价格和预算约束。
- 持仓后价格和权益波动会让实际杠杆漂移。
- 当前回测器没有强制降杠杆逻辑，因此实际曲线上可短暂超过 3.0x，例如最高约 `3.03x`。

## 10. 结论

XAU 单品种 3x 回测结果：

- 收益为正：`10000 -> 23925.64`。
- 总收益 `139.26%`，CAGR `11.82%`。
- 但最大回撤达到 `-34.57%`，风险明显偏高。
- 绝大部分收益来自 XAU long。
- XAU short 继续弱，净亏 `-1015.89`。
- 结果高度依赖 2024-2026 黄金大趋势，尤其 2025-2026 long 单笔贡献 `12967.42`。
- 由于数据存在 `625` 个 abnormal gap，该长样本更适合作为压力观察，不能只按收益判断可实盘复现。

如果只测试“XAU 单品种是否能吃到大趋势”，答案是可以。

如果测试“XAU 单品种 3x 是否稳健”，答案是不够稳健：回撤深、short 拖累、收益集中、数据 gap 风险较高。
