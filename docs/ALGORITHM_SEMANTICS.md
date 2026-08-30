# 指标、信号与执行语义

| 项目 | 输入 | lag / 预热 | 信号时间 | 执行语义 |
|---|---|---|---|---|
| Donchian N | 完整 D1 high/low | `rolling(N).max/min().shift(1)`，需 N+1 根 | 当前完整 bar 收盘 | `close > high` / `close < low` 严格比较 |
| Wilder ATR N | TR | 首 N 根算术均值 seed，随后 Wilder 递推 | 当前完整 bar | 风险/过滤特征，不单独下单 |
| SMA10/20 | 完整 D1 close | MA 交叉 `lag=0`，趋势过滤显式 `lag=1` | 当前完整 bar 收盘 | 前值 `<=` 且当前 `>` 为金叉；反向为死叉 |
| MACD 12/26/9 | 完整 D1 close | EMA `adjust=False` | 当前完整 bar 收盘 | `dif`/`dea` 交叉；`histogram=dif-dea`，`macd_bar=2*histogram` |

海龟报告同时提供不参与决策的盘中观察层：`high > channel_high_N` 或
`low < channel_low_N` 表示盘中越轨；只有收盘价仍在通道外才属于正式突破。盘中越轨、
收盘未确认的记录不会进入 `turtle_breakouts`、资格校验或通知。

## 每日多维趋势检查

- 海龟、SMA 排列、EMA 趋势与多周期 MACD 是平行策略族；组合评级不改变海龟下单或回测规则。
- SMA 多头/空头排列分别为 `5>10>20>55>120` 与完全反向；EMA 仅独立比较 `EMA144/EMA169`。
- MACD 除 D1 外还使用固定 D1 会话锚点聚合的 D2/D5/D7 K 线。每根高周期 K 线必须包含完整的 2/5/7 个 D1 会话；未完成桶不计算、不触发，信号时间为聚合 K 收盘日。
- MACD 明细中的动量方向表示 DIF 相对 DEA；组合评级中的多周期 MACD 方向表示 DIF 与 DEA 的零轴区域共识，两者不是同一个维度。
- DMI/ADX 使用 Wilder 14 期；`ADX >= 25` 且 `+DI/-DI` 同向时为趋势质量加分。ATR14/close 的 120 期分位在 10%–90% 为加分，区间外降级；相对成交量为当前量除以前 20 根均量，达到 1.5 为加分。
- A 级需要海龟、SMA、EMA、MACD 中至少三族同向且总分至少 6；B 级要求两族且至少 4 分。冲突不评级。全部底层事件持久化，只有进入或切换至 A 级时创建的共振事件进入通知队列。
- 报告异常保持逐日证据：跳空大于 2×ATR、日内振幅大于 3×ATR、120 日 ATR 排名低于 2% 或高于 98%。连续同类异常只在 HTML 中聚合为阶段，仍不进入正式通知。

## 策略版本

- `corrected-v2`：默认；共享指标、完整收盘确认、bar cursor 顺序回放、业务 key 不含价格。
- `legacy-v1`：只读归档标签；当前代码没有可执行的 `legacy-v1` 路径，不能作为 CLI 或 API 参数。

首次升级 cursor 只评估最新完整 bar，不自动重发全部历史。`--backfill-signals` 才会研究性回放全部 bar，且不发送正式通知。

正式每日顺序为：更新 → current/质量验证 → complete-only 加载 → freshness/资格 → 一次计算特征 → 顺序回放规则 → signal/cursor/outbox 同事务提交 → 当前 run 通知 → JSON/HTML 报告 → 关闭 DailyRun。
