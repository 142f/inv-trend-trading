# External XAU/BTC Long History 3x Backtest Report

生成日期：2026-04-20

## 数据源

本次不用 MT5，改用免费外部数据源：

| symbol | 数据源 | 接口 | 起点 | 终点 | bars |
|---|---|---|---:|---:|---:|
| XAUUSD_DUKAS | Dukascopy XAU/USD bid | `dukascopy-python` / Dukascopy chart historical data | 2005-01-02 20:00 UTC | 2026-04-20 00:00 UTC | 34721 |
| BTCUSDT_BINANCE | Binance spot BTCUSDT | Binance `/api/v3/klines`, interval `4h` | 2017-08-17 04:00 UTC | 2026-04-20 00:00 UTC | 18992 |

参考文档：

- Dukascopy historical data: https://www.dukascopy.com/wiki/en/development/strategy-api/historical-data/overview-historical-data/
- Binance kline endpoint: https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints

生成目录：

```text
data_external_xau_btc/
  raw/dukascopy/XAUUSD_DUKAS/H4.csv
  raw/binance/BTCUSDT_BINANCE/H4.csv
  processed/external/H4/XAUUSD_DUKAS.csv
  processed/external/H4/BTCUSDT_BINANCE.csv
  metadata/external/download_log.csv
  metadata/external/data_quality_report.csv
  metadata/external/symbol_specs.csv
```

下载命令：

```powershell
.\.venv\Scripts\python.exe -m examples.download_external_xau_btc_data --data-dir data_external_xau_btc --xau-start 2005-01-01 --btc-start 2017-08-17 --end 2026-04-20
```

## 数据质量

| symbol | market_type | bars | duplicate | bad_ohlc | large_gap | normal_session_gap | abnormal_gap | median_gap_hours | max_gap_hours |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| XAUUSD_DUKAS | session | 34721 | 0 | 0 | 1130 | 1115 | 15 | 4.0 | 76.0 |
| BTCUSDT_BINANCE | 24x7 | 18992 | 0 | 0 | 8 | 0 | 8 | 4.0 | 32.0 |

说明：

- Dukascopy XAU 的异常 gap 明显少于此前 MT5 XAU 长样本：`15` vs `625`。
- Binance BTCUSDT 是交易所成交 K 线，连续性比 MT5 BTCUSDc 更可核验。
- 本次 XAU 使用 bid OHLC；BTC 使用 Binance 成交 OHLC。由于两者不是同一经纪商可交易报价，结果用于策略研究，不等同于某券商真实成交。

## 回测配置

三组窗口都使用同一套 3x 风控：

| 约束 | 数值 |
|---|---:|
| 组合总杠杆上限 | 3.0x |
| 单方向杠杆上限 | 2.0x |
| XAU / BTC 类别杠杆 | 1.5x / 1.5x |
| 总 1N 风险上限 | 12% |
| 单方向 1N 风险上限 | 8% |
| XAU / BTC 类别 1N 风险 | 4% / 4% |
| XAU cost/slippage | 1 bps / 3 bps |
| BTC cost/slippage | 3 bps / 8 bps |

窗口：

| 名称 | 入场窗口 | 出场窗口 | N |
|---|---:|---:|---:|
| 20/55 H4 | 20 / 55 | 10 / 20 | 20 |
| 60/165 H4 | 60 / 165 | 30 / 60 | 60 |
| 120/330 H4 | 120 / 330 | 60 / 120 | 120 |

样本：

| 样本 | 口径 |
|---|---|
| full | XAU 从 2005-01-02 开始，BTC 从 2017-08-17 加入 |
| common | 两者都从 BTC 起点 2017-08-17 开始 |

## 总体结果

### Full 样本：2005-01-02 -> 2026-04-20

| 窗口 | final_equity | total_return | CAGR | max_drawdown | volatility | Sharpe-like | MAR | trades | add | max_total_lev |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 20/55 H4 | 73695.11 | 636.95% | 9.83% | -42.65% | 17.30% | 0.629 | 0.231 | 847 | 462 | 2.099x |
| 60/165 H4 | 47737.11 | 377.37% | 7.62% | -44.71% | 17.01% | 0.517 | 0.170 | 407 | 206 | 2.644x |
| 120/330 H4 | 91951.81 | 819.52% | 10.98% | -28.34% | 19.59% | 0.630 | 0.388 | 244 | 127 | 2.167x |

### Common 样本：2017-08-17 -> 2026-04-20

| 窗口 | final_equity | total_return | CAGR | max_drawdown | volatility | Sharpe-like | MAR | trades | add | max_total_lev |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 20/55 H4 | 57508.71 | 475.09% | 22.35% | -31.30% | 21.68% | 1.039 | 0.714 | 499 | 273 | 2.834x |
| 60/165 H4 | 68466.63 | 584.67% | 24.84% | -30.19% | 22.14% | 1.113 | 0.823 | 228 | 121 | 2.645x |
| 120/330 H4 | 79511.96 | 695.12% | 27.01% | -25.06% | 27.27% | 1.013 | 1.078 | 140 | 71 | 2.167x |

结论：

- 换成长历史外部数据后，`120/330 H4` 仍然是三组里最终权益最高、回撤最浅的版本。
- `60/165 H4` 在 common 样本中比 `20/55 H4` 更好，但在 full 样本里仍被 120/330 明显压过。
- full 样本 CAGR 低很多，是因为 2005-2016 只有 XAU 可交易，BTC 大趋势还没进入样本。

## Full 120/330 H4 详细结果

这是长样本里表现最好的一组。

### 分品种多空

| symbol | side | trades | net_pnl | cost | win_rate | avg_pnl | max_win | max_loss | avg_holding_bars |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT_BINANCE | long | 51 | 50275.28 | 1767.67 | 21.57% | 985.79 | 32845.68 | -818.05 | 102.90 |
| BTCUSDT_BINANCE | short | 30 | 10939.68 | 904.95 | 23.33% | 364.66 | 6967.67 | -708.51 | 77.73 |
| XAUUSD_DUKAS | long | 93 | 31596.80 | 1839.63 | 26.88% | 339.75 | 11791.04 | -1241.24 | 96.98 |
| XAUUSD_DUKAS | short | 70 | -10859.95 | 1244.02 | 12.86% | -155.14 | 647.17 | -776.81 | 41.94 |

### 退出类型

| exit_type | trades | net_pnl | cost | win_rate | avg_pnl | max_win | max_loss | avg_holding_bars |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| stop | 202 | -20244.28 | 4536.12 | 6.93% | -100.22 | 11791.04 | -1241.24 | 32.90 |
| trend_exit | 42 | 102196.09 | 1220.15 | 90.48% | 2433.24 | 32845.68 | -90.20 | 306.88 |

### 年度收益

| year | return | end_equity |
|---:|---:|---:|
| 2005 | 0.96% | 10095.75 |
| 2006 | 11.30% | 11236.20 |
| 2007 | 1.81% | 11439.40 |
| 2008 | -0.75% | 11353.79 |
| 2009 | 6.99% | 12147.18 |
| 2010 | 4.68% | 12715.95 |
| 2011 | 1.07% | 12852.31 |
| 2012 | 1.88% | 13094.49 |
| 2013 | -7.81% | 12072.11 |
| 2014 | 0.61% | 12146.00 |
| 2015 | -5.42% | 11488.14 |
| 2016 | 10.09% | 12647.72 |
| 2017 | 9.41% | 13837.34 |
| 2018 | 0.54% | 13911.85 |
| 2019 | 34.07% | 18652.05 |
| 2020 | 76.61% | 32941.31 |
| 2021 | 50.61% | 49612.97 |
| 2022 | -1.25% | 48994.12 |
| 2023 | 14.22% | 55962.01 |
| 2024 | 20.69% | 67538.84 |
| 2025 | 30.49% | 88133.12 |
| 2026 | 4.33% | 91951.81 |

### 最大盈利交易

| symbol | side | entry_time | exit_time | units | pnl | exit_reason |
|---|---|---|---|---:|---:|---|
| BTCUSDT_BINANCE | long | 2020-10-21 04:00 UTC | 2021-04-23 04:00 UTC | 2 | 32845.68 | long_exit_120d_low |
| XAUUSD_DUKAS | long | 2025-08-29 16:00 UTC | 2025-10-22 00:00 UTC | 2 | 11791.04 | intraday_stop |
| XAUUSD_DUKAS | long | 2025-01-22 04:00 UTC | 2025-05-01 00:00 UTC | 2 | 10201.75 | intraday_stop |
| BTCUSDT_BINANCE | long | 2023-10-23 04:00 UTC | 2024-01-22 20:00 UTC | 2 | 8584.90 | long_exit_120d_low |
| BTCUSDT_BINANCE | long | 2024-11-06 04:00 UTC | 2024-12-30 16:00 UTC | 2 | 6987.52 | long_exit_120d_low |

## Common 120/330 H4 详细结果

common 样本更适合和此前 MT5 的 2018 起点回测比较。

### 分品种多空

| symbol | side | trades | net_pnl | cost | win_rate | avg_pnl | max_win | max_loss | avg_holding_bars |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT_BINANCE | long | 51 | 43466.68 | 1527.20 | 21.57% | 852.29 | 28402.92 | -707.55 | 102.90 |
| BTCUSDT_BINANCE | short | 30 | 9455.90 | 781.76 | 23.33% | 315.20 | 6014.03 | -611.40 | 77.73 |
| XAUUSD_DUKAS | long | 36 | 23317.69 | 1133.19 | 33.33% | 647.71 | 10199.00 | -1073.02 | 110.72 |
| XAUUSD_DUKAS | short | 23 | -6728.31 | 660.10 | 4.35% | -292.54 | 560.20 | -671.86 | 51.04 |

### 退出类型

| exit_type | trades | net_pnl | cost | win_rate | avg_pnl | max_win | max_loss | avg_holding_bars |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| stop | 112 | -13084.23 | 3172.92 | 4.46% | -116.82 | 10199.00 | -1073.02 | 31.08 |
| trend_exit | 28 | 82596.19 | 929.32 | 92.86% | 2949.86 | 28402.92 | -77.98 | 330.68 |

## 和 MT5 结果对比

此前 MT5 最早共同样本是 `2018-06-28 -> 2026-04-20`，120/330 H4 3x 结果为：

| 数据源 | 样本起点 | final_equity | total_return | CAGR | max_drawdown | trades |
|---|---:|---:|---:|---:|---:|---:|
| MT5 XAUUSDc/BTCUSDc | 2018-06-28 | 71524.72 | 615.25% | 28.64% | -29.23% | 119 |
| External common XAU/BTC | 2017-08-17 | 79511.96 | 695.12% | 27.01% | -25.06% | 140 |
| External full XAU/BTC | 2005-01-02 | 91951.81 | 819.52% | 10.98% | -28.34% | 244 |

不能只看 final_equity：

- External full 样本时间长很多，CAGR 降到 `10.98%` 是正常的，因为 2005-2016 只有 XAU，没有 BTC 的趋势贡献。
- External common 与 MT5 更可比；它多了 2017-08 到 2018-06 的 BTC 初期样本，最终权益和回撤都优于 MT5 口径。
- XAU short 仍然是结构性拖累，换 Dukascopy 数据后仍为负。

## 输出目录

| 样本 | 窗口 | 输出目录 |
|---|---|---|
| full | 20/55 H4 | `outputs/external_xau_btc_x3_20_55_h4_full` |
| full | 60/165 H4 | `outputs/external_xau_btc_x3_60_165_h4_full` |
| full | 120/330 H4 | `outputs/external_xau_btc_x3_120_330_h4_full` |
| common | 20/55 H4 | `outputs/external_xau_btc_x3_20_55_h4_common` |
| common | 60/165 H4 | `outputs/external_xau_btc_x3_60_165_h4_common` |
| common | 120/330 H4 | `outputs/external_xau_btc_x3_120_330_h4_common` |
| summary | all | `outputs/external_xau_btc_x3_summary.csv` |

## 主要判断

1. 外部数据显著扩展了样本：XAU 从 2005 起，BTC 从 2017 起。
2. 数据质量比 MT5 长样本更好，特别是 XAU abnormal gap 从 `625` 降到 `15`。
3. 在三组窗口里，`120/330 H4` 仍然最稳：收益最高、回撤更浅、趋势退出质量最好。
4. `20/55 H4` 交易太多，仍然被大量 stop 和成本拖累。
5. `60/165 H4` 是中间版，但不是最优：common 样本优于 20/55，full 样本明显低于 120/330。
6. 结果仍然高度依赖少数 BTC/XAU 大趋势，尤其是 BTC 2020-2021 long。
