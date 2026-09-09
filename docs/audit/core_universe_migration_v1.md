# 四资产迁移现状审计 v1

审计日期：2026-09-08。先审计后修改；未删除文件。当前工作树初始无修改。

## 数据证据

权威库：`data/metadata/研究目录_v3.sqlite3`，只读核查其表、版本、物理 Parquet 和行情内容。

|资产|已有来源及身份|实际覆盖（日线开盘标签）|版本|质量问题|
|---|---|---|---|---|
|BTC|Binance / BTCUSDT.BINANCE.SPOT，USDT last|2017-08-17—2026-08-26，3297 条|bc4ff3bc207cb79982cf249a|UTC，无重复、倒序或日线缺口；CURATED 不等于历史交易规则已认证|
|ETH|Binance / ETHUSDT.BINANCE.SPOT，USDT last|2017-08-17—2026-08-26，3297 条|6826ac18be97f01ef4f4b7fd|同上|
|XAU|配置为 Dukascopy bid CFD；库中 XAUUSD_DUKAS/source=external|2005-01-02—2026-04-20，6878 条|market_datasets_v3: 7f232ee80f02e0df8d86699cbf7e26a0c71a6e7ebaf43f22547d7cf49b90f962|RESEARCH_ONLY；无 curated instrument/version；存在 240 个周六标签，不能套用工作日日历|
|XAG|配置为 Dukascopy bid CFD；库中 XAGUSD_DUKAS/source=external|2015-01-01—2026-04-20，3513 条|同上|RESEARCH_ONLY；582 个周日标签，收盘可用时间、实际会话和成交量单位未认证|

Crypto 物理对象 SHA256：BTC `beb48401484bb20ec2f3af40d3f93d64192c6caeb296ead29a016f8244c9ad8a`；ETH `b0c767e9e1d0988d38a154eb396273a102fa3af783a6dcbc4cbb095a0f67a8b3`。

Crypto timestamp 为 UTC 开盘、bar_end 为下一 UTC 零点。金属表日期带 UTC，但这仅证明标签格式，不能证明实际会话和公开可得时点。金属相邻标签间隔有 1、2、3 天，缺失区间必须与独立会话表核对，当前不能认定全部是休市。两个金属均无重复日期；volume 非零不能证明可成交手数。

原始资料是后期采集/导入的历史快照；有不可变对象与版本血缘，但没有完整历史 source vintage 证据。存在未来修订/补齐风险。2025+ 不得称 untouched OOS；历史实验统一标注 replayed_oos。

## 执行与账户

1. `core/事件账户_v1.py` 的 CashBroker 强制单 symbol，每账户默认 100000，下一 bar 开盘执行、滞后 volume 限量，手续费/滑点默认各 5 bps，lot_size 默认 1；这些属于研究参数，不是 BTC/ETH 历史规格。
2. `application/多方法研究_v1.py` 只接受 EQUITIES，对每 symbol/family/scenario 创建独立 broker，最后汇总权益；不能作为本次共享账户实现。
3. 另有 `application/perpetual_audit/分钟执行.py` 和 `core/永续风控.py` 的永续保证金、资金费事件和时点规格校验。其产品身份不同，不能套用到 Binance 现货或 Dukascopy CFD。
4. `core/执行约束.py` 明确 certified=False。永续认证读取器要求 funding 独立结算日程及 mark 数据；没有四资产统一、已认证 ContractSpec。
5. XAU/XAG 缺 bid/ask 成交证据、合约乘数、tick、数量步长、最小量、历史保证金和隔夜费用。禁止猜测正式参数；正式入口必须拒绝 UNVERIFIED。
6. BTC/ETH USDT 与金属 USD 不能隐式等价。必须有历史汇率，或显式仅研究的 USDT/USD=1 假设。

## 架构与存储复用

- `core/阶段协议_v1.py` 有显式 Envelope/Quality/RunContext/hash、7 个 Stage，账户 payload 是单资产，没有组合分配、组合目标或独立 Execution Stage。
- 通用可复用：不可变内容寻址、SQLite 事务、Quality/UTC/hash 思路、在线特征的历史窗口、先训练再锁参协议、评估和报告模式。
- 股票专用：`data/时点行情_v1.py` 的 EQUITIES、纽约 09:30/16:00 近似会话、252 年化、CashBroker 单资产和 lot=1、各 symbol 独立资金、旧聚合 Benchmark。保留兼容，仅标工程验证。
- 已有 `adapters/multi_asset/risk`、budget_policy 等风控实现，但不是四资产共用事件账本。
- 复用 `dataset_versions/heads`、`data_objects/aliases`、`documents`、`quality_assessments/intervals`、`lineage_links`、`market_datasets_v3/market_bars_v3`、`result_runs/parameters/metrics/tables/rows`、`wf_windows_v3`、`retention_pins`。StageRepository 有独立版本表定义，但当前权威库尚无 stage_studies_v1，不应假称已存在运行结果。
- SPY 当前只有 legacy 数据；Treasury proxy 没有已认证输入，扩展验证不得替换 Core。

## 迁移决策

先建立版本化四资产配置及 UNVERIFIED 正式规格，提供明确隔离的 RESEARCH_ASSUMPTION 模式；先验收数据/执行门禁，再开放收益计算。研究模式必须披露金属日期近似、bid 执行假设、非真实 volume 限额、汇率和成本假设；不能形成正式可交易结论。

冻结 Core=BTC/ETH/XAU/XAG；旧科技股模块保留为工程验证。当前正式验收阻断项是历史合同、金属会话/来源及 FX 血缘，不能通过调参或测试得分解除。
