# 趋势监控运行与恢复

当前终态更新（2026-10-06）：隔离观察 outputs/持续公开更新24小时_20261005_02 已实际结束，原生exit1、FAILED，累计86406秒；BTC/ETH×D1/H4共656成功、8失败、2中断，各目标164成功，未达到250要求。Windows休眠对应约10小时14分钟扫描间隔，600秒门禁native2拒绝；恢复版本中BTC/ETH各2根H4未被成功扫描，共4项标的bar。原会话及五个已记录工作/子进程均已结束，未完成任务0，不再声明当前仍live。停机后独立Repository Catalog核验native0：660数据记录、4个current有效，1336隔离行情文件及current/运行记录前后不变；1457个原行情文件保护验证通过。隔离目录更新产生的新对象属于正常更新范围，不能把它与原行情保护混淆。本机10个唯一投递、重复0、可投递队列0，不代表用户实际收到提醒。终态证据绑定见 outputs/当前长观察终态验收证据基线_20261006.json；目录一致性通过不改变观察失败。主源码和99原测试仍与先前1388通过基线一致，本轮未重复全量回归。正式全资产行情、实际用户通知和长期连续运行仍未完成，整体目标未完成；下文旧检查点保留为历史证据。

贵金属公开目录复查（2026-10-06 13:02北京时间）：通过现有DukascopyBarsProvider进行一次timeout=10秒、retries=1的目录请求，显式use_environment_credentials=False且api_key为None；返回HTTP429，回执status=FAILED。检查脚本原生exit0表示已记录请求结果，不代表供应商接通或正式数据通过。未发布数据、未写原行情，不据此认定必须使用密钥或放宽来源/质量/日历/H4门禁。证据：outputs/黄金白银公开目录复核_20261006/复核回执.json。

当前运行状态更正（2026-10-06）：本机Windows系统日志记录北京时间01:57进入低功耗、12:08恢复，与约10小时14分钟扫描启动间隔相符；系统报告Button or Lid，唤醒来源未知。原观察会话62340仍可实际轮询，监督器自动更换扫描子进程并将2项遗留任务标为INTERRUPTED，随后四目标扫描恢复。检查点累计480成功、8失败、2中断，未完成0；62份决策、45份事件封装可验证，本机接收器10个唯一事件、重复0。扫描间隔600秒门禁以原生退出码2拒绝，不能宣称连续运行或24小时验收通过。恢复后的BTC/ETH×D1/H4四份固定快照完整复现一致，重复成功扫描复用决策；执行门禁均关闭。证据见 outputs/长间隔主机电源事件_20261006.json、outputs/长间隔扫描门禁原生退出回执_20261006.json、outputs/长间隔恢复四目标精确复现_20261006/精确复现回执.json 和 outputs/长间隔恢复与重复扫描去重回执_20261006.json。没有更改系统电源设置或核心策略；此前完整1388项通过属于历史基线。正式股票/ETF/贵金属数据、真实用户通知和长期运行验收仍未完成，整体目标未完成。下文保留的较早检查点均为历史证据。

运行现状更正（2026-10-05 19:55北京时间检查点）：下文按日期保留的“仍在运行”描述是历史检查点，不代表当前存活。旧观察句柄62303、71138及68098均已结束或丢失；旧62303/68098的进程消失及原行情保护证据见 `outputs/双观察进程消失取证回执_20261005.json`，消失根因未证实，不将历史RUNNING字段作为存活证明。当前隔离公开更新观察位于 `outputs/持续公开更新24小时_20261005_02`，本轮实际轮询句柄62340，并由Windows原生检查确认两个持有子进程存活；这只证明检查时存活。

当前只读审计见 `outputs/公开更新中断恢复现状审计_20261005.json`：状态样本运行14184秒，BTC/ETH×D1/H4累计184成功、8失败，各目标46成功、2失败，未完成任务0；36份决策、32份事件封装可验证；4次通知投递对应本机接收器4个唯一事件、重复0。8次失败来自两轮公开Binance TLS连接异常，后续恢复；外部网络根因未证实，故障与恢复去重证据见 `outputs/中断恢复供应商故障后三轮扫描去重回执_20261005.json`。1457个原行情文件保持不变。H4两目标各推进1根已收盘bar，D1尚未推进。相邻已记录任务最大启动间隔303.603671秒，600秒审计门禁通过；这不是连续进程存活证明，也不是正式产品SLA。

主264源码及99份测试源码仍与 `outputs/组合修复正式交付基线_20261004.json` 一致；1388通过、7项既有警告为此前实际完成的完整回归，本轮未重新运行。P0-core的Final仅限已验证本机Windows/后端合同。24小时与各目标250成功轮、全部周期新收盘推进、股票/ETF/贵金属正式数据、实际用户通知渠道和整体监控交付仍未完成；活跃更新目录的最终Catalog审计须待持有工作进程停止后进行。本轮没有改变策略、质量门禁或运行器，也没有新开观察代替当前进程。

扫描间隔验收出口（2026-10-05）：需要自动阻断超限或样本不足时，在只读审计命令中同时指定 `--maximum-start-gap-seconds 600 --require-cadence`。必须显式指定阈值；工具先保存完整回执，再在间隔超限或尚未验证时返回原生退出码2，间隔合格返回0。默认报告模式保持兼容，仍需检查回执。此选项只验收已持久化任务的相邻开始间隔，不证明连续在线、首尾覆盖、数据新鲜度、用户收件或整体系统完成；运行中的观察也可以通过这一项检查。现有观察进程与持续验收运行器不变。

持续观察的扫描间隔复核：运行 `python scripts/长观察复核.py --directory <观察目录> --output <新的回执.json> --maximum-start-gap-seconds 600`。针对每300秒扫描的观察，本例600秒为显式容差；其他周期应根据运行要求指定阈值。工具通过只读SQLite事务检查相邻任务开始时间，`scan_cadence.excessive_gaps`保留超限前后任务与时间；`observed_cadence_within_limit=false`表示扫描间隔不合格，null表示未指定阈值或样本不足。工具exit0仅表示审计成功，不能代替检查回执内容。该检查不证明进程在线、首尾覆盖、成功数据更新或用户收到通知；这些需要各自证据。主机休眠可能中断所有目标，恢复扫描和自动重启也不能补回休眠期间的在线时长。当前活跃观察保持原运行器，不因观察超时重启；部署环境须满足持续供电、联网和保持运行的要求。

真实v3双周期历史回放复核（2026-10-04）：本轮无必要结构性修改，主264源码和99份测试源码仍与8fe0b1.../1388项完整通过基线一致。在已结束的v3公开行情短验收数据上，使用既有HistoricalReplay和共享Backtest运行BTC/ETH×D1/H4四个固定版本，实际终止exit0；日线各400根、H4各543根，共1886根逐bar回放，四组各自均覆盖IDLE/SETUP/CANDIDATE/CONFIRMED_ENTRY/POSITION/EXIT。同bar、同捕获模拟组合与资金输入的1882根策略意图比较差异0；每组末根因Backtest没有对应入场评估而未比较，共4根，不伪造其输入。四个独立回放库通知outbox/attempt均0，38行情文件、Catalog current及原实时事件/任务计数不变。这是策略意图与模拟账户上下文一致性证明，不是成交/PnL、实际持仓或整个监控验收。长观察62303/71138仍在运行，24小时、新收盘bar、其他资产正式数据和用户通知仍待完成。证据outputs/v3真实历史回放交付基线_20261004.json。

持续更新审计复核（2026-10-04）：旧长观察复核工具在已成功完成的v3更新短验收上复现exit1，根因为把隔离行情正常变化当作只读违规；本轮只扩展审计工具，不改运行器或核心源码8fe0b1...，264源码及99份原测试源码与1388项完整通过基线一致。更新模式现在核验原行情哈希、运行库根绑定/隔离、所有快照模型与哈希、成功任务的质量/新鲜度和未知账户执行门禁、通知回执，保留默认只读检查。14项合同通过，覆盖原4项及新10项允许隔离更新、拒绝原行情变动/假成功/根冲突；实际运行中的4目标首检查点8成功、166目标检查点3轮498任务和330唯一本机投递，未完成任务均0。另外通过现有scanner.reproduce精确重算已结束v3短验收的四份真实市场快照，完整payload逐字段相等，38行情文件、Catalog current及原事件/任务计数不变，独立复现库无事件或通知。隔离数据的最终Catalog仍由运行器在持有工作进程停止后核验，不把活跃更新中的跨连接观察冒充原子Catalog审计。长观察62303/71138仍live，未完成24小时或新收盘bar验收，实际用户通知和其他资产正式数据仍待完成。证据outputs/持续更新审计交付基线_20261004.json。

持续公开更新复核（2026-10-04）：主源码8fe0b1...完整1388项回归通过；本轮仅扩展验收工具，未变更核心策略/数据门禁。首次公开更新短观察使用裸根，实际选择legacy DuckDB，11成功/1次Catalog文件占用失败，阻断并恢复；不能作为稳定性通过。核实此前裸根真实行情短验收同样属于legacy后端范围，旧记录称其v3的表述应以此更正为准，不用其代替v3持续更新证据。工具已显式初始化v3 SQLite；一次初始化网络超时现场保留，并补齐初始化FAILED回执、原数据保护和不启动监督进程的合同。最新12项工具合同通过（独立于1388主回归）；实际v3短验收exit0，BTC/ETH×D1/H4各3轮，共12成功/0失败，Catalog与未知账户门禁核验、进程退出和原行情保护通过。短观察未跨新收盘bar，也没有市场事件通知，因此不证明长期更新或用户收到提醒。全83资产×2周期只读24小时观察62303，以及BTC/ETH真实更新24小时观察71138均已启动并确认live；后者要求每目标250成功轮数及至少1次新收盘bar推进。两者尚未完成24小时验收，其他资产正式数据和实际用户通知目的地仍待完成。证据outputs/持续公开更新验证基线_20261004.json。

主工作区完整回归复核（2026-10-04）：62824已实际结束exit0，1388 passed、7项既有警告、575.58秒，0失败/错误/跳过。主源码8fe0b1...与已完整通过的三文件候选逐字节一致，原95份测试源码保持不变、新增4份测试源码。证据outputs/组合修复正式交付基线_20261004.json绑定264源码/pyproject、99份测试源码、完整报告及依赖。P0-core在已验证的本机Windows/后端合同范围维持Final，不代表全资产数据认证或整个监控系统Final。持续验收工具保留默认只读模式，新增明确选择BTC/ETH的隔离公开更新模式，保护原数据并要求成功更新轮数和可配置收盘bar推进数；9项隔离合同通过，真实短验收待终态。

合入后真实输入复核（2026-10-04）：新隔离目录outputs/组合修复真实更新验收_20261004_01的公开Binance真实更新验收已终止exit0；BTC/ETH×D1/H4三轮12项SUCCESS，四份快照精确复现，同版本决策去重、重新打开运行库及Catalog/SQLite核验通过，未完成0、重复事件0。unknown账户execution=false，无订单或外部通知；本次仍是短验收，不能证明长期稳定。主完整回归62824仍待终态。证据outputs/组合修复本轮基线_20261004.json。

组合修复集成复核（2026-10-04）：组合候选完整回归已实际结束，1388 passed、7项既有警告、552.55秒，退出码0，外部源码/测试模块均0。三处补丁已逐文件校验后合入主源码8fe0b1a7f2735a68ecb41c47971a34c8e431e945235a26c37c512016c488a2e6：Dukascopy环境凭据显式开关、factory传递公开凭据策略、Windows停止Win5边界要求受限等待并取得真实句柄退出证明。真实拒绝访问或等待超时仍抛原异常，无质量门禁降级。原95份测试源码字节不变，新增4份共19个案例；集成前递归清单计数检查曾中止，未发生写入，修正清单校验后成功。主完整集成回归62824已启动，尚未取得终态，不能用候选通过替代主回归。集成回执outputs/组合修复集成_20261004/集成回执.json。旧只读观察89379已实际终止exit1，源码守卫触发FAILED，累计4151.17秒、166目标各14轮、2324任务（2296失败/28成功）、330唯一本机投递；1457行情文件不变、未完成任务0、持有进程已确认退出，停止回执outputs/组合修复旧观察停止回执_20261004.json。旧观察不是24小时通过，也不是provider持续更新验证。主回归通过后再开展新源码持续验证；股票/ETF与贵金属正式行情、实际用户通知目的地及多天运行仍未完成，整体未Final。

2026-10-04停止合同复核：公开凭据候选完整1382项仍1失败，新增现场为v3原生恢复测试停止时Win5。不要将错误5直接解释为进程已退出；组合候选使用已持有的句柄做最多5秒等待，只有退出证据成立才完成，真正存活/超时保留错误。不接管存档PID、不停止未知进程。44项监督合同及真实句柄交错通过，完整26863运行中，未集成。原持续观察89379仍live。贵金属正式输入仍待验证；官方历史归档的AWS凭据/请求方计费路径没有启动。

2026-10-04候选运行复核：公开凭据测试启动器已补Windows spawn主入口保护，固定候选源码与测试包目录并核验模块来源；原通知进程死亡恢复和凭据/监控入口共32项通过。旧完整1379结果为1378通过/1启动器失败，不能作为完整通过；新完整回归64629仍live。主源码未集成，仍不要把credential_providers=False当成已修复的Dukascopy环境key隔离保证。独立候选四项真实行情只读扫描/复现通过，不更新原或前轮验收数据。

2026-10-04公开更新边界待修：当前主源码的credential_providers=False尚不能阻止Dukascopy读取环境key。禁止把这一开关当作已验证的无凭据隔离保证；本轮只以虚拟值、拦截网络复现，不读取实际key。修复候选55项通过，全量仍在会话67830，尚未集成。实际公开目录无key探测HTTP429，保留outputs/贵金属公开接口探测_20261004_01/公开接口探测回执.json；不自动重试密钥或注册账户。原只读观察89379未受候选修改影响。

2026-10-04实际运行补充：隔离验收outputs/真实更新运行链验收_20261004_03已验证refresh=true的12项公开行情更新/扫描及runtime重新打开后的去重。工具outputs/真实更新运行链验收_20261004.py要求显式--output和不存在的新目录，不覆盖证据；下载公开BTC/ETH数据，不读取密钥，不运行用户通知或下单。它是短时验收工具，不是生产部署命令。原data仍保留正式门禁；24小时会话89379本轮确认live，但refresh=false不证明provider持续更新。

当前基线（2026-10-04）：主工作区完整1369项通过、7项既有警告，source253e61...，P0在本机已验证范围恢复Final，见outputs/生命周期集成交付基线_20261004.json。新观察目录outputs/持续监控24小时_20261004_02，会话89379，86400秒/300秒扫描/60秒采样、每目标至少250次；仍RUNNING。新观察仍读原data且refresh=false，不用其证明provider更新或四项新隔离行情已写入原库；若再发现实证缺陷，应记录并优先修复，不能只为了冻结时长保留有缺陷源码。

真实加密资产链路证据位于outputs/加密行情实际接入验收_20261004：四项实际接入、实际增量更新/扫描、四份保存决策精确复现和400根真实BTC日线回放，哈希汇总outputs/实际加密链路交付基线_20261004.json。实时扫描账户明确未知，阻断CONFIRMED_ENTRY；历史回放使用明确模拟账户，不是真实成交。原data未更新，授权的正式供应商/账户/通知目的地和持续更新仍须分别接通验收。

2026-10-04主源码已集成修复，完整回归执行中。原outputs/持续监控24小时_20261004_01因确认的数据身份漏洞/心跳误报修复而结束，最终FAILED（source changed），不是观察超时或24小时通过；outputs/原观察停止回执_20261004.json核验本次进程已退出、任务无残留及行情不变。后续持续验证必须使用新目录和新源码基线。

预期组件显式退役入口：`trend-monitor --config <明确列出保留目标的配置> retire-expectation --component scanner --scope OLD:D1 --expected-epoch <当前epoch> --actor <操作者> --reason <退役原因>`。epoch从`RuntimeLedger.expectations(include_retired=True)`获取，审计通过`expectation_history(component,scope)`读取。配置目标为空表示全部ACTIVE，因此该配置不能用于退役scanner；scheduler不能由此命令退役。命令不初始化Repository/Strategy，不删除历史或停止进程；活着/未知心跳持有者或未完成扫描先拒绝，不能用退役掩盖故障。移除配置后仍须显式退役，重新启用由现有runtime登记并开始新的等待心跳周期。重复同一epoch/actor/reason返回原回执，变化请求拒绝；审计/生命周期丢失不自动重建。

## 股票正式来源接入前置契约（2026-10-04审查）

尚未选择用户的正式供应商，也未使用密钥或发送市场请求。现有Yahoo/Alpha Vantage的research_only限制继续生效。任何新供应商须通过已有Repository发布流程；接口支持某种周期本身不能证明符合本项目交易时段和正式用途要求。

- 身份：实际返回的symbol、instrument_id、来源代码及周期不得与请求冲突；来源feed、交易所覆盖、许可、币种、价格类型、调整方法必须明确。旧CSV接入存在显式身份被覆盖的问题，已隔离复现并在候选修复，主源码尚未合入；不能继续无条件引用此前P0 Final作为当前完整结论。
- 时间：使用带时区时间，明确供应商标签是开盘还是收盘，固定regular时段。H4须与现有纽约09:30起点及实际收盘/提前收盘契约一致；不能直接把任意供应商4Hour结果改标签当作合格H4。聚合保留组成周期和窗口证据，缺少组成K线不得填造。
- 完成性：最后一根是否收盘必须按实际交易日历判断；行情延迟、收盘后的修订和最大允许时效均需明确。晚到修订经过原重叠更新及人工Review规则，不静默覆盖已引用历史版本。
- 调整：区分原价和拆股/分红调整价，防止重复调整；保存所用规则及请求参数。机构行动系列是否已核验单独记录，不以API支持adjustment替代认证。
- 请求：明确起止、分页完整性、空结果、限速、超时、重试及授权失败；保留原始响应和每页证据。不得因分页未完成或403而换成不兼容feed继续正式发布。
- 验收：合成合同只证明实现边界；还须授权真实请求、最新版行情、Quality/Manifest/current校验、正式Repository读取和D1/H4策略扫描。每种资产/周期分别给出证据，不用BTC通过替代股票通过。

供应商文档核对仅用于设计：Alpaca历史bars接口提供1Day及1—23Hour、feed、adjustment和分页参数；SIP与IEX覆盖不同，近期访问还受账号权限约束。因此未来如选用该服务，必须显式绑定feed/权限并验证时段，不能自动选择或自动订购。[历史bars官方接口](https://docs.alpaca.markets/us/reference/stockbars)、[官方Market Data FAQ](https://docs.alpaca.markets/us/v1.1/docs/market-data-faq)。没有据此宣称服务已接通。

2026-10-04准备度补充：重放旧Yahoo原始快照不会解除research_only来源限制。全新隔离v3根完成76项历史接入和完整Catalog检查，正式读取全部拒绝。验收制品在outputs/股票原始快照重新认证_20261004_01；不可覆盖生产data、删除research_only或修改旧质量报告使监控发信号。最新数据更新及正式来源资格须另行验证。原持续观察源码仍冻结，已知心跳并发误报候选尚未合入。

活动观察可使用只读检查工具生成新的、不可覆盖的检查点：

```powershell
.\.venv\Scripts\python.exe scripts/长观察复核.py --directory outputs/持续监控24小时_20261004_01 --output outputs/长观察检查点_新编号.json
```

它不会重启、停止或接管PID，也不会修改行情/事件库。SQLite mode=ro/query_only和读事务用于一致性检查，Snapshot及Envelope校验复用现有模型；报告中的活动任务不等于停机残留。行情CSV/Parquet/current指针按初始清单复核，新增同类文件也会拒绝。接收审计末尾无换行半行视为正在写入，完整错误仍失败；数据库、行情和接收审计来自各自观测时刻，最终终态仍应等待runner自然结束。失败不生成通过回执。

最新完整回归（2026-10-04）：1330项通过、7项既有警告、565.04秒，源码f096a81489da15ec6abe1d7a2ef28e67e38ce354fcaa33afb40d44f238ae927e，见outputs/通知监督事务交付基线_20261004.json；启动及监督范围同事务修正已复核。下面的旧“追加回归待结果”保留为历史记录。

## 独立持续验收

```powershell
.\.venv\Scripts\python.exe scripts/持续监控验收.py --output outputs/持续监控验收_新编号 --duration-seconds 86400 --interval-seconds 300 --sample-seconds 60 --minimum-cycles 2
```

output必须不存在，保留历史证据；参数正数且minimum-cycles为正整数。默认观察24小时，真实Repository仅只读行情，不构造provider，账户保持unknown。工具启动本机接收器及扫描/通知监督，地址仅在进程内新建环境变量，不读取用户生产端点。源码变动会停止验收并标记FAILED，不能把多源码版本混成一份长期报告。当前检查范围为数据根CSV/Parquet和current JSON，报告明确文件数量，不等同于旧其他选择器的文件清单。

持续验收状态.json包含时长、运行数量、阻断目标、事件、队列、接收审计、最少完整扫描次数及停机后的未完成任务；本机接收审计.jsonl记录每个事件键、是否重复及payload哈希。停止时先关闭扫描，再有界等待政策允许的通知排空，最后停止通知，确认自己持有的子进程已死亡并核对行情哈希。报告COMPLETED只表示本次时长及最低次数完成，不表示整个项目Final，更不表示真实用户收到通知。只读观察不能证明持续供应商更新；该项仍需目标环境验收。

当前追加事务修正（2026-10-04）：通知监督上一版1329项完整回归已通过；最终Review另复现启动/范围两次自动提交的中间态，现改为同一显式事务。当前源码f096a81489da15ec6abe1d7a2ef28e67e38ce354fcaa33afb40d44f238ae927e，追加回归待结果。旧1329回执保留且注明此待办，不能冒充当前源码验收。

当前通知监督迭代（2026-10-04）：新增supervise-notify，复用ProcessSupervisor及Windows原子创建；39项首轮、5项真实HTTP恢复、76项边界、34项空闲/命令回归通过。当前全项目回归执行中，源码96c151440567db3c85cedc9faf1b09e302627bfa378809dbfb2425826c4cec31；以下旧轮次“尚无通知监督”的说明为历史状态。

## 监督通知消费器

先将用户选定Webhook地址放入显式环境变量，再启动：

```powershell
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml supervise-notify --webhook-env MONITOR_WEBHOOK_URL
```

它独立于supervise扫描进程，共用监控事件库；每个不透明渠道身份有自己的监督锁、进程历史和重启预算，旧无scope记录保持扫描runtime解释。supervisor_scopes增量表绑定新的启动记录，不改变旧supervisor_processes字段。监督父进程不打开行情Repository，不发送HTTP；地址只通过环境传给既有notify --serve入口，参数中仅有环境变量名。通知子进程接受独立停止标记，认领下一项前检查停止，正常完成正在执行的投递及回写；轮询等待每0.2秒检查停止，并按心跳期限三分之一更新真实空闲脉冲。空闲心跳不在HTTP/原生调用阻塞时运行。

监督仅接受本实例Job成员发出的notification及匹配渠道心跳；启动期限和heartbeat_timeout_seconds用于发现首次未就绪或后续卡死。使用既有graceful_stop_seconds、restart_delay_seconds、max_restarts及restart_window_seconds；停止后租约由既有队列过期恢复，保留原事件幂等键。扫描硬期限不应用于通知任务。NOTIFICATION_HEALTH记录通知监督失败与恢复，保持同渠道抑制避免通知故障循环。

真实本机HTTP验收覆盖正常投递与协作停止、接收回执后原生无限等待、停止与重启、LEASE_EXPIRED到DELIVERED；两次请求保持同一事件键，仍需接收方去重。用户实际外部目的地、多天持续运行、系统启动服务及预期目标退役管理尚未完成。

当前Windows原子创建更新（2026-10-04）：supervise使用Windows 10 / Server 2016及以上的JOB_LIST创建属性，使进程启动时已归属本实例Job；不支持属性或创建失败时停止启动，不退回非原子创建。父进程kernel退出边界及创建错误清理合同已通过，全项目1315项通过、7项既有弃用警告，见outputs/原子归属交付基线_20261004.json。supervise仍只监督扫描runtime，notify需独立启动；未安装操作系统启动服务或完成多天运行验收。以下旧轮次报告保留为历史证据。

运行能力包括CLI、扫描调度、独立事件库、任务审计、心跳、独立通知消费器及只读FastAPI面板。扫描监督已完成本机验证，当前通知监督的验证范围见顶部记录；用户实际通知端点尚未验收。不能把outbox的PENDING任务当作已发送通知。

## 配置与启动

在项目根目录运行。监控配置示例为 `config/监控运行示例.yaml`；其中路径相对于YAML所在目录解析。策略参数继续使用既有 `src/inv_trend/config/strategy.yaml`，监控YAML不接受另一套rules。

```powershell
.\.venv\Scripts\python.exe -m pip install -e . --no-deps --no-build-isolation --no-index
.\.venv\Scripts\trend-monitor.exe --help
```

也可以使用 `python -m inv_trend.cli.监控运行`。示例中的空symbols表示Repository中所有ACTIVE资产，包含ETF持仓快照扩展的股票；timeframes接受1D/4H并规范为D1/H4。RESERVED或未登记资产会拒绝启动，不自动猜测来源。

默认账户为unknown，执行门禁不通过。显式 `--paper-equity 100000` 使用模拟参考账户，只计算策略意图，不创建真实持仓、不调用交易接口。长期运行需要持仓事实时，应通过Runtime的account_source注入带观察身份、时间和持仓引用的ReferenceAccount；第一版CLI没有自动连接券商账户。

## 只读行情扫描

```powershell
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml scan
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml scan --symbols BTC ETH --timeframes 1D 4H
```

scan强制关闭行情更新，不构造网络provider，也不读取provider密钥。它会写监控事件库、任务记录及锁文件。正式行情只通过HistoricalDataService.load_bars读取；质量失败、Manifest失败、缺失周期和过期数据保留明确健康证据并阻断市场事件。

运行成功与行情可用性是不同事实：SUCCESS表示该次扫描正常完成并通过质量门禁；已验证但过期的数据仍记录STALE_DATA，不产生正式确认。FAILED表示本次更新或扫描未通过质量/运行检查。行情是否可用应同时查看快照的quality_gate、freshness_gate和DATA_HEALTH。

## 更新扫描与常驻运行

```powershell
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml once --symbols BTC ETH --timeframes 1D
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml serve
```

once和serve遵循YAML的refresh开关。refresh=true时使用Repository.update，然后正式load、策略计算及事件提交。当前默认构造Binance、Dukascopy和Yahoo公共provider；监控入口显式禁用读取Alpha Vantage凭据，已有market-data入口的可选provider行为保持兼容。未接通的来源、股票H4和未认证的研究数据会记录失败，禁止退回任意本地文件产生正式信号。

interval_seconds控制重新扫描间隔；workers限制并发；poll_seconds控制调度巡检；scan_timeout_seconds是整次任务的软期限。单个instrument_id+timeframe的文件锁位于数据根的 `.monitoring_locks`，多个监控进程共享该锁；周期不同可独立运行。事件库内的unfinished任务唯一索引提供第二道保护。

超时巡检会取消该任务的决策提交资格，记录SCAN_TIMEOUT；任务返回后，在事务开始及COMMIT之前再次检查期限。已经提交的有效决策不会因随后记账耗时被改称超时。线程无法强制终止一个冻结的第三方本地调用；正常HTTP使用有界timeout/retries，异常长时间阻塞时需要外部进程监督。不要把软期限描述成对任意底层调用的硬中断。

Ctrl+C触发关闭、取消排队任务并等待工作线程收尾。没有发送订单的路径。

## 文件与数据库

运行目录默认 `outputs/趋势监控`：

| 文件/表 | 职责 |
| --- | --- |
| 监控事件.sqlite3 | live决策、状态、事件、outbox、运行任务及心跳 |
| 历史回放.sqlite3 | 独立replay决定与状态；禁止通知入队 |
| decisions | 完整Decision Snapshot，包含输入、账户、前置状态、实际行情前缀和代码/配置绑定 |
| monitoring_state | 每个资产/周期的当前状态及revision |
| events / outbox | 每个独立事件及其待发送任务，和决定同事务提交 |
| runtime_tasks | RUNNING、SUCCESS、FAILED、TIMED_OUT、INTERRUPTED任务及错误证据 |
| runtime_heartbeats | scanner、scheduler和lock的最近观测时间 |

事件库有所有者标记、schema和live/replay用途，不能使用行情Catalog或其他已有数据库代替。运行记录另外绑定绝对data_root，重启指向不同数据根会拒绝。SQLite使用WAL、FULL同步和外键；备份时应停止写入或使用SQLite backup API，不能只复制仍在写入的主文件而遗漏WAL。

## 独立心跳检查

```powershell
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml watchdog
```

watchdog独立打开监控库，不调用Market Data、策略或provider，也不会为被检查的scheduler伪造心跳。它检查主scheduler及已登记scanner的心跳；从未启动过的空事件库会报告HEARTBEAT_MISSING。外部任务计划或服务管理器应定期运行它，才能在整个监控进程停住以后仍发现失联。

主scheduler允许的心跳年龄为heartbeat_timeout_seconds；scanner允许interval+scan_timeout+heartbeat_timeout，避免正常调度等待被误报。未来时间戳同样不视为健康。失联/恢复以健康状态变化生成事件，重复检查不重复通知。

## 重启与故障恢复

启动时只回收操作系统已明确确认死亡的PID所持未完成任务，标记INTERRUPTED；存活或状态无法验证的持有者不会被抢占。Windows内核文件锁在进程死亡后释放，不依据文件年龄删除锁文件。

同一输入重复扫描返回原决定，新的运行尝试仍有自己的job_id。旧bar不能回退当前状态，状态revision防止并发覆盖。决定、状态、全部独立事件和outbox同事务提交：提交前进程终止，重启后全部没有；提交后终止，重试返回已有记录。

修复行情异常应使用已有market-data doctor/verify/repair-current和审核流程，不编辑监控快照、不更改质量报告以绕过门禁。事件库损坏时保留证据、停止该实例，不能自动接纳新用途或静默清空状态。

## 固定版本历史回放

```powershell
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml replay --symbol BTC --timeframe 1D --dataset-version <已验证版本> --compare-backtest --paper-equity 100000
```

回放只加载一次指定版本，逐bar向共享策略提供因果前缀，在bar结束及发布宽限后判断；不读取current，不写live库，不发送通知。对照捕获回测器当根bar的模拟持仓和权益，比较相同输入下的策略意图。报告compared_bars、uncompared_bars和mismatches；它不证明真实成交、收益或供应商当时可得性一致。

已保存市场决定可由MonitorScanner.reproduce重算。原版本、配置、代码、账户、前置状态、时钟及价格前缀哈希必须匹配；失败健康记录没有市场输入，不能伪称可复现的策略判断。

## 退出码及当前边界

| 退出码 | 含义 |
| --- | --- |
| 0 | 命令正常完成；不等于所有数据新鲜或具备执行资格 |
| 1 | 启动、配置或未能正常记录的运行异常 |
| 2 | 扫描含FAILED/SKIPPED/TIMED_OUT任务，或watchdog失联，或回测意图对照不一致 |
| 130 | 用户中断 |

stdout输出JSON结果；stderr输出已脱敏错误。错误中的HTTP URL路径、query、userinfo及常见凭据键（包括引号包围的JSON/Python字典文本）会遮盖。DATA_MISSING表示没有正式发布版本，QUALITY_FAILURE表示已知认证/质量失败，有正式current而制品丢失属于MANIFEST_FAILURE。

## 独立通知进程

把选定Webhook URL放入自己指定的环境变量，然后单独启动消费器：

```powershell
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml notify --webhook-env MONITOR_WEBHOOK_URL
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml notify --webhook-env MONITOR_WEBHOOK_URL --serve
```

第一条最多消费20项（--batch-size可设置1到1000）；第二条独立循环，按poll_seconds轮询。notify与supervise-notify显式读取指定环境变量，不构建行情Repository或provider。URL不得放进配置或日志；HTTPS端点必需，HTTP仅允许本机测试。通知政策在同一运行YAML的notifications中配置，SETUP/IDLE默认关闭。只认领尚未绑定渠道或已绑定本渠道的任务；改变端点不自动搬迁旧渠道任务。

PENDING尚未认领；IN_FLIGHT租约有效；RETRY等待next_attempt；DELIVERED已获匹配2xx回执；FAILED永久失败或重试预算耗尽；SUPPRESSED按政策留档而不发送。notification_attempts保存每次开始/结束时间、渠道、状态、HTTP回执或脱敏错误；LEASE_EXPIRED说明该次租约未能完成本地确认。默认最多5次，租约30秒大于HTTP超时10秒；重试5秒起指数退避，上限300秒。永久失败不自动清除，也不擅自重复发送。

端点2xx只证明接收请求；远端必须按event_id/Idempotency-Key去重，才能避免“对端接收成功但本机记录前进程退出”引起的重复投递。扫描状态不受独立通知失败影响。通知失败/恢复有NOTIFICATION_HEALTH事件，保留审计但不通过同一渠道再次发送，避免循环。通知消费器在首次工作前登记预期组件，空队列仍更新真实心跳；watchdog检查已登记组件，即使它从未发出首次心跳。

用户真实目的地尚未提供；当前只有隔离的本地HTTP验收，未向外部地址发送。完整进程监督验收仍待完成。

## 首次心跳与连续失败

Runtime在首次扫描前登记scheduler及全部实际解析后的scanner目标；notify登记不透明渠道身份，不保存Webhook URL。登记与心跳分开，登记不是健康证明。登记后未收到首次心跳，期限内为HEARTBEAT_STARTING，超期为HEARTBEAT_MISSING；真实心跳恢复为HEARTBEAT_RECOVERED。重启不会重置首次登记时间，避免反复重启掩盖失联。scheduler和notification期限为heartbeat_timeout_seconds，scanner期限为interval_seconds+scan_timeout_seconds+heartbeat_timeout_seconds。

watchdog不打开行情Repository，合并已登记组件、配置中显式目标及旧库已有scanner/notification心跳。显式目标没有登记和心跳时直接报告缺失。symbols为空且从未初始化Runtime时，watchdog无法在Repository不可用的情况下推断完整资产池；应先完成Runtime目标登记，或在配置中指定目标。已登记目标不会因另一个进程使用较小资产池而被自动删除；停用目标的显式登记退役管理仍待完善。

consecutive_failure_threshold默认3。连续已完成的FAILED/TIMED_OUT/INTERRUPTED任务达到阈值后发出CONSECUTIVE_SCAN_FAILURES；成功任务重置计数并发出SCAN_FAILURES_RECOVERED。计数读取持久任务记录，跨重启保存；同一故障周期只通知一次。健康快照中的count_at_transition、last_seen表示状态变化时的证据，当前心跳和任务详情应从运行记录查看。心跳状态未改变时不为每次轮询增加快照。

当前scan_timeout_seconds是协作软期限，阻止超时后的市场提交，但不能强制中断阻塞在原生库里的线程。此类进程卡死需要独立watchdog及进程监督；close等待工作线程结束，不能把软期限宣称为已实现硬终止或无人值守恢复。

## 独立运行时进程监督

```powershell
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml supervise
```

supervise父进程只打开独立监控事件库，保存配置副本并启动相同解释器下的既有serve入口。运行目录中进程监督.lock阻止两个监督实例重叠；每个子进程有独立停止标记和持久启动/结束记录，监督子进程.log保留子进程输出。启动失败、scheduler心跳缺失或未完成扫描超过硬期限时，先请求退出，再使用自己持有的进程句柄结束；Windows Job只包含本实例归属的子进程和后代。不会读取PID文件接管、终止未知或外部进程。

supervision设置启动期限120秒、扫描硬期限180秒、正常退出等待5秒、重启间隔5秒、300秒窗口最多5次重启（含首次启动共6次）。硬期限必须大于scan_timeout_seconds；预算跨监督进程重启读取持久启动记录，耗尽后记录RESTART_BUDGET_EXHAUSTED并退出2，避免无限启动。数据/策略故障快速结束任务且scheduler健康时不会仅靠重启掩盖失败，沿用连续失败告警。

Windows虚拟环境启动器与真实Python工作PID可不同，监督使用Job成员关系验证心跳和任务身份；不能以数值PID相等作为唯一判断。扫描硬期限检查全部未完成任务，不受运行历史分页限制。原生阻塞结束后，新Runtime只恢复已知死亡进程任务为INTERRUPTED，继续复用既有状态/决定/事件去重。

扫描监督合同已验证三后端原生阻塞、12次恢复后扫描及父进程异常退出清理；原子JOB_LIST修正另由1315项全量回归验证，见outputs/原子归属交付基线_20261004.json。supervise只管理扫描runtime，通知可另用supervise-notify监督，当前验证范围见顶部。未安装系统启动服务，未验证数天无人值守运行；不能把本命令误称为完整无人值守部署。

## Catalog故障定位

行情目录自己的SQLite/DuckDB/v3连接打开、查询或提交失败，会转换为CatalogAccessError并保留原始异常链；监控扫描与只读看板显示CATALOG_FAILURE，正式信号阻断。这个转换只发生在目录持有的数据库连接中，供应商内部缓存异常仍按provider故障处理，EventStore错误仍由监控运行边界处理。唯一性/不可变约束异常保持原始IntegrityError或ConstraintException语义。

历史请求起点查询失败不再返回“没有记录”。目录无法查询时，停止更新并检查原始数据库错误；不能据此推断冷启动范围或继续下载。修复使用既有doctor/verify/repair-current及审批路径，不编辑监控快照或跳过质量门禁。实际SQL失败、连接无法打开、事务回滚、看板只读和外部异常职责已在隔离三后端合同中验证；最终全项目1291项通过、7项弃用警告，见outputs/目录故障交付基线_20261004.json。

## 只读看板

安装可选依赖并启动独立本机页面：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[monitoring]"
.\.venv\Scripts\trend-monitor.exe --config config/监控运行示例.yaml dashboard --port 8000
```

打开http://127.0.0.1:8000/；目标详情如/targets/BTC/D1，JSON接口/api/status及/api/targets/BTC/D1。仅监听本机，未提供远程部署/多用户认证。启动不会创建行情provider，页面没有更新、交易或修改按钮；首次启动EventStore可升级已识别的旧监控库，HTTP请求本身只读。页面手动刷新，运行扫描与通知进程须独立启动。

主页表格时间为北京时间；完整ISO时间、版本、价格/指标/条件及历史在详情中。UNKNOWN/横线表示没有验证过的对应证据；原决定的价格不能视作实时价。Current Signal=BLOCKED时查看详情阻断原因，Monitoring State保留原状态并不代表现在可执行。质量/Manifest详情由Repository验证，不读取任意用户提供的文件路径。Repository启动失败时仍显示已有目标及系统健康，当前信号全部阻断。

本机已验证Python3.12.6、FastAPI0.142.2、Starlette1.7.0、Uvicorn0.54.0、Pydantic2.13.5。TestClient使用现有httpx有一项Starlette弃用警告，不影响HTTP合同；未通过删除测试或压制警告伪造通过。浏览器验收先遇到插件初始化路径错误，独立Chrome又遇到沙箱IPC权限限制；最终使用独立测试浏览器及Playwright精确视口完成，不访问用户登录浏览器配置。


## 隔离公开行情持续更新验收

默认模式始终只读既有行情；`--refresh-isolated`仅接受显式BTC/ETH，将新行情写入全新输出目录内的v3 SQLite根，拒绝复用数据根或写入原行情目录。它使用现有监督器、Repository、策略和通知队列，账户保持unknown；通知接收器仅在本机。

```powershell
$env:PYTHONPATH="src"
python scripts/持续监控验收.py --output outputs/新版本公开更新观察 --refresh-isolated --symbols BTC ETH --duration-seconds 86400 --interval-seconds 300 --sample-seconds 60 --minimum-cycles 250 --minimum-bar-advances 1
```

每次使用新输出目录。保存`持续验收状态.json`、初始受保护行情哈希、运行数据库、监督日志和本机接收审计。初始化失败产生FAILED回执并抛原异常；运行完成须验证每个目标的成功轮数、质量/新鲜度与未知账户执行门禁、Catalog、通知排空、进程退出和原行情未变。完成计时不等于整体监控验收，失败后不得把保留现场覆盖为成功；其他资产数据须单独完成来源和正式质量认证。


### 停机后的隔离v3目录核验（2026-10-06）

运行 `python scripts/隔离观察终态目录核验.py --directory <已结束的隔离观察目录> --output <新回执.json>`。工具要求观察报告为COMPLETED或FAILED、全部任务有终态、监督启动记录有完成时间，并通过既有Windows进程检查确认所有已记录工作/子进程PID均已结束；存活、未知或PID无法验证时exit2，且不构造Repository或调用Catalog验证。PID被其他进程复用也会保守拒绝，不能强行把未知拥有者当作已退出。

通过前置门禁后，仅支持目录内明确隔离的v3-sqlite更新根，要求当前核心源码匹配观察源码，再复用HistoricalDataService.verify_catalog；providers为空，不更新行情、不修复current。核验成功任务对应current覆盖、Manifest/文件哈希、pointer与Catalog一致性，以及前后行情文件、current、任务/事件计数和报告不变。exit0及PASSED_FINAL_CATALOG_ONLY只证明此次终态目录检查，不能使FAILED观察变成连续运行或用户通知通过；exit1表示核验失败。该工具不声称排除了未登记写入者或提供跨库原子事务。

16项合同通过；实际活跃长观察exit2拒绝，已停止的既有短观察exit0、28数据记录/4个current有效、62个行情文件不变。证据绑定于 outputs/终态目录核验交付基线_20261006.json。当前长观察尚未执行终态Catalog核验。


### 休眠间隔的收盘扫描覆盖复核（2026-10-06）

恢复时固定版本中的K线，经既有Repository正式版本读取与快照内容哈希验证，与成功扫描记录逐项对比：BTC、ETH各有UTC2026-10-05 16:00和20:00开盘的两根H4 bar未出现在成功扫描快照中，共4项标的bar；恢复扫描的00:00 H4 bar首次扫描距收盘约9分钟。两项D1新收盘bar首次成功扫描距收盘约4小时9分钟。证据：[固定版本收盘覆盖审计](../outputs/休眠间隔固定版本收盘覆盖审计_20261006_02/覆盖审计回执.json)。

此审计原生exit2、状态OBSERVED_SCAN_COVERAGE_INCOMPLETE；指标中的closed_bar_advances只计算已观察到的不同bar，不能证明它们连续或覆盖全部中间收盘。审计使用恢复版本，不证明同一版本在每个历史收盘点已可取得，不重建当时策略判断、不声称漏发了4个交易信号，也没有补发通知或补回在线时长。长扫描间隔及覆盖缺口继续保留为未通过证据。
