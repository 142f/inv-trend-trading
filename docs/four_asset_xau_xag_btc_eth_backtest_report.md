# Four-Asset XAU/XAG/BTC/ETH 3x Backtest Report

生成日期：2026-04-21

## 数据

数据目录：`data_external_xau_btc_xag_eth`

| symbol | source | start | end | bars | abnormal_gap |
|---|---|---:|---:|---:|---:|
| XAUUSD_DUKAS | Dukascopy XAU/USD bid | 2005-01-02 20:00 UTC | 2026-04-20 00:00 UTC | 34721 | 15 |
| XAGUSD_DUKAS | Dukascopy XAG/USD bid | 2015-01-01 20:00 UTC | 2026-04-20 00:00 UTC | 18058 | 8 |
| BTCUSDT_BINANCE | Binance spot BTCUSDT | 2017-08-17 04:00 UTC | 2026-04-20 00:00 UTC | 18992 | 8 |
| ETHUSDT_BINANCE | Binance spot ETHUSDT | 2017-08-17 04:00 UTC | 2026-04-20 00:00 UTC | 18992 | 8 |

下载命令：

```powershell
.\.venv\Scripts\python.exe -m examples.download_external_xau_btc_data --data-dir data_external_xau_btc_xag_eth --include xau xag btc eth --xau-start 2005-01-01 --xag-start 2015-01-01 --btc-start 2017-08-17 --eth-start 2017-08-17 --end 2026-04-20
```

## 回测配置

主基线：

| 项目 | 配置 |
|---|---|
| strategy | Turtle |
| timeframe | H4 |
| entry | 120 / 330 H4 |
| exit | 60 / 120 H4 |
| stop | 2N |
| leverage | 3x category allocation |
| metals cluster cap | 1.5x |
| crypto cluster cap | 1.5x |

单品种设置：

| symbol | max_units | unit_1N risk | max_symbol_1N | max_symbol_leverage | short policy |
|---|---:|---:|---:|---:|---|
| XAUUSD_DUKAS | 3 | 0.4% | 4.0% | 1.5x | tested |
| XAGUSD_DUKAS | 2 | 0.3% | 3.0% | 1.0x | tested |
| BTCUSDT_BINANCE | 2 | 0.3% | 4.0% | 1.5x | enabled |
| ETHUSDT_BINANCE | 2 | 0.3% | 3.0% | 1.0x | tested |

样本：

| sample | 说明 |
|---|---|
| common_btc / common_all | 2017-08-17 起，四个品种都有数据 |
| full | XAU 从 2005 起，XAG 从 2015 加入，BTC/ETH 从 2017 加入 |

## 总体结果

### Common 样本：2017-08-17 -> 2026-04-20

| asset set | policy | final_equity | total_return | CAGR | max_drawdown | volatility | Sharpe-like | MAR | trades |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2 assets | no_xau_short | 92044.60 | 820.45% | 29.17% | -24.42% | 26.79% | 1.089 | 1.195 | 117 |
| 4 assets | baseline | 138478.10 | 1284.78% | 35.41% | -45.63% | 37.75% | 0.992 | 0.776 | 299 |
| 4 assets | no_xau_short | 170788.98 | 1607.89% | 38.72% | -40.03% | 37.57% | 1.060 | 0.967 | 274 |
| 4 assets | no_xau_xag_short | 172299.08 | 1622.99% | 38.86% | -40.20% | 37.30% | 1.067 | 0.967 | 253 |
| 4 assets | no_metals_short_no_eth_short | 158439.52 | 1484.40% | 37.52% | -38.22% | 36.46% | 1.057 | 0.982 | 215 |

### Full 样本：2005-01-02 -> 2026-04-20

| asset set | policy | final_equity | total_return | CAGR | max_drawdown | volatility | Sharpe-like | MAR | trades |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2 assets | no_xau_short | 141281.70 | 1312.82% | 13.24% | -27.17% | 18.93% | 0.752 | 0.487 | 173 |
| 4 assets | baseline | 159013.14 | 1490.13% | 13.87% | -45.63% | 25.94% | 0.631 | 0.304 | 421 |
| 4 assets | no_xau_short | 234113.25 | 2241.13% | 15.96% | -40.03% | 25.54% | 0.708 | 0.399 | 356 |
| 4 assets | no_xau_xag_short | 304313.55 | 2943.14% | 17.40% | -36.63% | 25.33% | 0.760 | 0.475 | 309 |
| 4 assets | no_metals_short_no_eth_short | 280352.82 | 2703.53% | 16.95% | -36.91% | 24.82% | 0.755 | 0.459 | 271 |

## 主要结论

### 1. 增加 XAG 和 ETH 明显提高收益，但显著加深回撤

Common 样本：

| 对比 | final_equity | max_drawdown | MAR |
|---|---:|---:|---:|
| 2 assets, no XAU short | 92044.60 | -24.42% | 1.195 |
| 4 assets, no XAU/XAG short | 172299.08 | -40.20% | 0.967 |

四资产最终权益接近翻倍，但回撤从 `-24.42%` 加深到约 `-40%`。因此四资产不是“无脑更好”，而是更激进的组合。

### 2. XAG short 也应禁用

Common 样本中：

| policy | XAG pnl | final_equity |
|---|---:|---:|
| no_xau_short | -4501.92 | 170788.98 |
| no_xau_xag_short | 9273.39 | 172299.08 |

XAG short 和 XAU short 类似，都是拖累项。禁用 XAG short 后，XAG 从净亏损转为正贡献。

### 3. 是否禁用 ETH short 取决于风险偏好

Common 样本：

| policy | final_equity | max_drawdown | MAR |
|---|---:|---:|---:|
| no_xau_xag_short | 172299.08 | -40.20% | 0.967 |
| no_metals_short_no_eth_short | 158439.52 | -38.22% | 0.982 |

禁用 ETH short 会降低最终收益，但 MAR 略高、回撤略浅。ETH short 没有像金银 short 那样明显无效，但它增加了波动。

## 推荐方案

如果你追求主基线的收益/风险平衡，我建议：

```text
XAU long only
XAG long only
BTC long/short
ETH long/short
120/330 H4
3x category allocation
```

也就是：`four_asset + no_xau_xag_short`。

结果：

| sample | final_equity | CAGR | max_drawdown | Sharpe-like | MAR | trades |
|---|---:|---:|---:|---:|---:|---:|
| common | 172299.08 | 38.86% | -40.20% | 1.067 | 0.967 | 253 |
| full | 304313.55 | 17.40% | -36.63% | 0.760 | 0.475 | 309 |

如果你更重视回撤和组合稳定性，保留两资产基线仍更稳：

```text
XAU long only
BTC long/short
```

Common 结果：

| sample | final_equity | CAGR | max_drawdown | Sharpe-like | MAR |
|---|---:|---:|---:|---:|---:|
| common | 92044.60 | 29.17% | -24.42% | 1.089 | 1.195 |

## Four-Asset 推荐方案细分

口径：common, no_xau_xag_short

| symbol | side | trades | net_pnl | win_rate | max_win | max_loss | avg_holding_bars |
|---|---|---:|---:|---:|---:|---:|---:|
| BTCUSDT_BINANCE | long | 53 | 62955.58 | 18.87% | 40781.82 | -1657.20 | 94.89 |
| BTCUSDT_BINANCE | short | 30 | 20331.61 | 23.33% | 14748.22 | -1029.24 | 77.73 |
| ETHUSDT_BINANCE | long | 59 | 32880.66 | 16.95% | 21309.19 | -1113.96 | 62.32 |
| ETHUSDT_BINANCE | short | 36 | 10957.54 | 16.67% | 12714.57 | -1152.31 | 70.86 |
| XAGUSD_DUKAS | long | 40 | 9273.39 | 12.50% | 12583.81 | -1180.79 | 50.18 |
| XAUUSD_DUKAS | long | 35 | 25900.31 | 25.71% | 14631.23 | -1309.39 | 102.97 |

退出类型：

| exit_type | trades | net_pnl | cost | win_rate | avg_pnl | max_win |
|---|---:|---:|---:|---:|---:|---:|
| stop | 211 | -60463.00 | 8373.40 | 3.79% | -286.55 | 14631.23 |
| trend_exit | 42 | 222762.08 | 2251.32 | 92.86% | 5303.86 | 40781.82 |

最大盈利：

| symbol | side | entry | exit | units | pnl |
|---|---|---|---|---:|---:|
| BTCUSDT_BINANCE | long | 2020-10-21 04:00 UTC | 2021-04-23 04:00 UTC | 2 | 40781.82 |
| ETHUSDT_BINANCE | long | 2020-11-06 04:00 UTC | 2021-02-23 12:00 UTC | 2 | 21309.19 |
| ETHUSDT_BINANCE | long | 2025-07-11 00:00 UTC | 2025-09-22 08:00 UTC | 2 | 15656.59 |
| BTCUSDT_BINANCE | short | 2026-01-29 20:00 UTC | 2026-03-04 12:00 UTC | 2 | 14748.22 |
| XAUUSD_DUKAS | long | 2025-08-29 16:00 UTC | 2025-10-28 04:00 UTC | 2 | 14631.23 |

## 输出目录

```text
data_external_xau_btc_xag_eth/
outputs/external_four_asset_x3/summary.csv
outputs/external_four_asset_x3/four_asset_common_btc_no_xau_xag_short/
outputs/external_four_asset_x3/four_asset_full_no_xau_xag_short/
outputs/external_four_asset_x3/two_asset_ref_common_btc_no_xau_short/
```

## 最终判断

- 加 XAG/ETH 后，收益明显提高。
- 但回撤也从两资产的约 `-24%` 加深到四资产的约 `-40%`。
- 金银 short 都建议禁用。
- ETH short 可以保留；如果你更保守，可以禁用 ETH short 换取略低回撤和略高 MAR。
- 当前四资产推荐版本是 `no_xau_xag_short`，但它应该作为“进攻型基线”，不是替代两资产稳健基线。
