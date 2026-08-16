# XAU Short Policy Experiment

生成日期：2026-04-21

## 实验口径

数据源：`data_external_xau_btc`

主基线：

| 项目 | 配置 |
|---|---|
| symbols | XAUUSD_DUKAS, BTCUSDT_BINANCE |
| strategy | Turtle 120/330 H4 |
| exit | 60/120 H4 + 2N stop |
| leverage | 3x category allocation |
| main sample | common: 2017-08-17 -> 2026-04-20 |
| robustness sample | full: 2005-01-02 -> 2026-04-20 |

测试的 XAU short 政策：

| policy | 含义 |
|---|---|
| baseline | 原始策略 |
| disable_xau_short | 禁用 XAU short，只保留 XAU long |
| half_xau_short | XAU short 仓位减半 |
| slow_only_xau_short | XAU short 只允许 slow system |
| xau_short_strength_ge_0_5n | XAU short 突破强度至少 0.5N |
| xau_short_strength_ge_1_0n | XAU short 突破强度至少 1.0N |
| xau_short_below_sma330 | 只在 XAU close < SMA330 时允许 short |
| xau_short_slow_below_sma330 | slow-only + close < SMA330 |

## Common 样本结果

| policy | final_equity | total_return | CAGR | max_drawdown | Sharpe-like | MAR | trades | XAU short trades | XAU short pnl |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 79511.96 | 695.12% | 27.01% | -25.06% | 1.013 | 1.078 | 140 | 23 | -6728.31 |
| disable_xau_short | 92044.60 | 820.45% | 29.17% | -24.42% | 1.089 | 1.195 | 117 | 0 | 0.00 |
| half_xau_short | 85704.44 | 757.04% | 28.12% | -24.12% | 1.050 | 1.166 | 140 | 23 | -3793.67 |
| slow_only_xau_short | 80727.32 | 707.27% | 27.24% | -25.09% | 1.014 | 1.086 | 139 | 22 | -6763.12 |
| strength >= 0.5N | 83801.95 | 738.02% | 27.78% | -24.61% | 1.037 | 1.129 | 134 | 17 | -4873.14 |
| strength >= 1.0N | 88070.40 | 780.70% | 28.52% | -24.61% | 1.062 | 1.159 | 130 | 13 | -2750.13 |
| below SMA330 | 79511.96 | 695.12% | 27.01% | -25.06% | 1.013 | 1.078 | 140 | 23 | -6728.31 |
| slow + below SMA330 | 80727.32 | 707.27% | 27.24% | -25.09% | 1.014 | 1.086 | 139 | 22 | -6763.12 |

## Full 样本结果

| policy | final_equity | total_return | CAGR | max_drawdown | Sharpe-like | MAR | trades | XAU short trades | XAU short pnl |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| baseline | 91951.81 | 819.52% | 10.98% | -28.34% | 0.630 | 0.388 | 244 | 70 | -10859.95 |
| disable_xau_short | 141281.70 | 1312.82% | 13.24% | -27.17% | 0.752 | 0.487 | 173 | 0 | 0.00 |
| half_xau_short | 119406.47 | 1094.06% | 12.35% | -26.73% | 0.695 | 0.462 | 248 | 75 | -6255.80 |
| slow_only_xau_short | 95680.32 | 856.80% | 11.19% | -28.34% | 0.639 | 0.395 | 241 | 68 | -11181.78 |
| strength >= 0.5N | 111784.26 | 1017.84% | 12.01% | -27.80% | 0.678 | 0.432 | 225 | 52 | -8217.24 |
| strength >= 1.0N | 124985.11 | 1149.85% | 12.59% | -26.32% | 0.710 | 0.478 | 209 | 36 | -4869.83 |
| below SMA330 | 91884.32 | 818.84% | 10.98% | -28.34% | 0.630 | 0.387 | 243 | 69 | -10802.26 |
| slow + below SMA330 | 95680.32 | 856.80% | 11.19% | -28.34% | 0.639 | 0.395 | 241 | 68 | -11181.78 |

## 结论

### 1. 最推荐：直接禁用 XAU short

这是最强且最干净的结果：

| 样本 | baseline final | no XAU short final | 改善 |
|---|---:|---:|---:|
| common | 79511.96 | 92044.60 | +15.76% |
| full | 91951.81 | 141281.70 | +53.65% |

同时：

- common MAR 从 `1.078` 提升到 `1.195`。
- full MAR 从 `0.388` 提升到 `0.487`。
- 回撤略微改善。
- 交易数减少，结构更简单。

### 2. 如果不想完全禁用：用 strength >= 1.0N

`xau_short_strength_ge_1_0n` 是保留 XAU short 的最佳折中：

- common final `88070.40`，明显优于 baseline。
- full final `124985.11`，明显优于 baseline。
- XAU short 亏损从 common `-6728.31` 降到 `-2750.13`。
- XAU short 亏损从 full `-10859.95` 降到 `-4869.83`。

但它仍然没有让 XAU short 变成正收益，只是降低伤害。

### 3. 简单保守折中：XAU short 半仓

`half_xau_short` 表现也稳定：

- common final `85704.44`。
- full final `119406.47`。
- full 最大回撤 `-26.73%`，是测试里较好的。

如果你想保留一部分黄金下跌趋势敞口，但不想引入太多规则，半仓是低复杂度方案。

### 4. 不推荐：slow-only 和 SMA330 过滤

`slow_only_xau_short` 几乎没有解决问题。

`below_sma330` 在 common 样本完全没有变化，说明已有 XAU short 大多已经发生在均线下方；这个过滤条件没有提供额外信息。

## 推荐决策

当前主线建议：

1. 主基线：`120/330 H4 + disable_xau_short`。
2. 备选基线：`120/330 H4 + XAU short strength >= 1.0N`。
3. 保守折中：`120/330 H4 + half_xau_short`。

如果目标是实盘可执行性和组合稳定性，优先采用禁用 XAU short。

如果目标是研究“黄金是否存在可用空头趋势腿”，保留 `strength >= 1.0N` 继续观察，但不要把它作为主基线。

输出目录：

```text
outputs/xau_short_policy/
  summary.csv
  baseline_common/
  disable_xau_short_common/
  half_xau_short_common/
  xau_short_strength_ge_1_0n_common/
  ...
```

