# 趋势量化架构审计与迁移状态

## 目标依赖方向

```text
historical_data ──> inv_trend_application ──> inv_trend_observability
                            │
                            v
                    inv_trend_core

turtle_detector / turtle_multi_asset = 兼容入口与领域适配器
```

`inv_trend_core` 不访问 Provider、Repository、SQLite、Parquet、CLI 或报告；它只包含确定性数学原语和稳定信号身份。`historical_data` 继续拥有行情获取、三层数据、质量门控、Calendar、Manifest 与 Repository。现有包暂时作为兼容层，按功能逐步迁移，不破坏历史数据与入口。

## 本阶段已修复

- Donchian、Wilder ATR、SMA、EMA、MACD 下沉为共享实现；Donchian 使用前序 bar，MACD 同时明确 `histogram` 与 `macd_bar`。
- 旧 `high_N/low_N` 字段保留为适配别名，规范字段为 `channel_high_N/channel_low_N`。
- `SignalEvent` 与不含价格的稳定业务 key 移入 Core，默认策略版本为 `corrected-v2`。
- CandidateDetector 从两次完整准备降为一次；单品种 detector 回测从逐 bar 重算前缀改为一次准备后顺序推进。
- 每日扫描增加可重复 SQLite migration、策略版本 cursor、事务性 signal/outbox/cursor 提交与停机 bar 回放。
- 通知统计限制为当前 run；旧 pending 单独计为 recovered；异常会结束为 `COMPLETED_WITH_DELIVERY_ERRORS`。
- 美股新鲜度按纽约 16:15、DST 与 NYSE 交易日判断；24x7/24x5 使用完整 UTC 日边界。
- 回测终值非正时 CAGR 记为 `-100%`，避免复数/NaN。
- 每日 HTML 为纯 Python、内嵌 CSS/SVG、UTF-8、自包含文件。
- 修复旧 `turtle-alert` 报告中的乱码字符串。

## 尚存重复与风险

- `turtle_multi_asset` 的大型组合回测 runner 仍同时负责撮合、风险、账本和估值；尚未拆成独立执行阶段。
- 旧策略仍通过兼容别名消费指标；待基线差异报告稳定后再移除旧字段。
- SQLite 适合单机计划任务；多机写入或远程服务出现前不引入 MySQL。
- HTML 是观察层，不是计算权威；JSON、Parquet 与 SQLite 仍是审计依据。

## 迁移原则

每次迁移都增加因果性与冻结基准测试；若改变可执行策略语义，必须先记录首个事件差异并建立新的、可追溯的基线，禁止直接覆盖历史回测结果。
