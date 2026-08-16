# 指标、信号与执行语义

| 项目 | 输入 | lag / 预热 | 信号时间 | 执行语义 |
|---|---|---|---|---|
| Donchian N | 完整 D1 high/low | `rolling(N).max/min().shift(1)`，需 N+1 根 | 当前完整 bar 收盘 | `close > high` / `close < low` 严格比较 |
| Wilder ATR N | TR | 首 N 根算术均值 seed，随后 Wilder 递推 | 当前完整 bar | 风险/过滤特征，不单独下单 |
| SMA10/20 | 完整 D1 close | MA 交叉 `lag=0`，趋势过滤显式 `lag=1` | 当前完整 bar 收盘 | 前值 `<=` 且当前 `>` 为金叉；反向为死叉 |
| MACD 12/26/9 | 完整 D1 close | EMA `adjust=False` | 当前完整 bar 收盘 | `dif`/`dea` 交叉；`histogram=dif-dea`，`macd_bar=2*histogram` |

## 策略版本

- `corrected-v2`：默认；共享指标、完整收盘确认、bar cursor 顺序回放、业务 key 不含价格。
- `legacy-v1`：只读归档标签；当前代码没有可执行的 `legacy-v1` 路径，不能作为 CLI 或 API 参数。

首次升级 cursor 只评估最新完整 bar，不自动重发全部历史。`--backfill-signals` 才会研究性回放全部 bar，且不发送正式通知。

正式每日顺序为：更新 → current/质量验证 → complete-only 加载 → freshness/资格 → 一次计算特征 → 顺序回放规则 → signal/cursor/outbox 同事务提交 → 当前 run 通知 → JSON/HTML 报告 → 关闭 DailyRun。
