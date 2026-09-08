# inv-trend-trading：标准阶段与因果研究版本

当前发布：`0.4.0rc1`。新增七阶段研究主链路、三类方法/36组网格、逐根时间回放及原v3 SQLite内的实验谱系。**研究链路验收与全项目/实盘验收分开；后两者尚未通过。**

## 运行

Python 3.10+；发行验证环境为Python 3.13。完整依赖及测试工具见pyproject.toml；Windows时区由tzdata提供。

```bash
python -m pip install -e ".[test]"
research-stages --data data study --study-id 本地滚动验证_v4
research-stages --data data verify --study-id 本地滚动验证_v4
research-stages --data data report --study-id 本地滚动验证_v4 --output 本地研究评审_v4.html
```

附带库内已有 `三方法滚动定版_v3` 的本轮结果，验证已有结果只需运行verify。study_id和报告导出路径均不可覆盖；再次研究使用新版本ID。没有网络下载步骤隐藏在新研究驱动中；它读取随包/原项目既有固定版本D1行情。源码补丁不含数据库，应用补丁后沿用自己的原data，不要覆盖生产库。

不能安装editable包时，在项目根设置 `PYTHONPATH=src` 后，用 `python -m inv_trend.cli.分阶段研究_v1` 替代research-stages。Windows PowerShell设置：`$env:PYTHONPATH="src"`。

## 文档与职责

- [阶段输入输出规范](docs/阶段输入输出规范_v1.md)：全部字段、接口、版本、状态、事务、时间推进、独立执行与限制。
- [重构验收报告](docs/重构验收报告_v1.md)：真实测试、参数实验、收益/风险、评分、失败清单和未完成事项。
- [修改说明](docs/修改说明_v1.md)：逐文件修改、原因、兼容性及删除清单。
- [数据库定义](docs/阶段数据库定义_v1.sql)：原库内14张增量表的实际DDL。
- [研究方法依据](docs/研究方法依据_v1.md)：原始研究来源及与本轮实现的差异。

长期权威为 `data/metadata/研究目录_v3.sqlite3`；行情对象在既有raw/processed区；信号、实验、回测与评审进入数据库。HTML及验收证据文件是只读导出，不是Stage间的交换协议。

## 测试与治理

```bash
python -m pytest -q tests/test_标准阶段_v1.py tests/test_时点存储_v1.py tests/test_architecture_boundaries.py
python -m pytest -q
python scripts/目录治理_v1.py --root . --manifest 本地清理清单_v1.json
# 审阅清单、备份并停止测试/构建写入后才执行：
python scripts/目录治理_v1.py --root . --manifest 本地清理清单_v1.json --apply
python scripts/性能核验_v1.py --data data --output 本地性能核验_v1.json
```

清理默认不删除；apply核验每个已审阅路径和哈希。仅清理缓存、build副本和egg-info，不删除行情、数据库、仍被引用的processed_data、兼容转发模块或.env。发行包不带.env；在本机保留现有私有配置，不应从他人环境覆盖。

现有Turtle/daily/market-data入口仍保留。原日报制品契约、旧滚动回测接口和Parquet依赖相关测试有已复现失败；不能因为新增研究链路通过，就把旧路径重新命名为已认证。详细全量对照在验收报告。

## 投研边界

默认八个已有股票/ETF，2020—2024十个半年窗口；三方法每种12组，训练3子窗、5根隔离；手续费5bps、滑点5bps、下一开盘、上一日成交量限制，成本翻倍/延迟2根压力场景。现金账户仅做多，负向信号转为空仓；跨窗不断仓、不重置资金。

全部结果均为RESEARCH_ONLY。标的历史选择偏差、公司行动/日历版本、分红税费及未来纸上交易未认证；留出集未在本轮使用也不能证明项目此前从未访问它。不是实盘信号服务或收益承诺。
