# V8 策略快速入门指南

## 🚀 快速开始

### 1. 功能测试（5分钟）

首先验证V8策略的基本功能是否正常：

```bash
python tests/test_v8_strategy.py
```

预期输出：
```
开始V8策略功能测试...
================================================================================
测试简化入场信号检测
================================================================================
✓ 入场信号测试通过

================================================================================
测试简化出场决策
================================================================================
✓ 出场决策测试通过

================================================================================
测试简化回测引擎
================================================================================
✓ 回测引擎测试通过

================================================================================
V8策略功能测试完成
================================================================================
```

### 2. 准备回测数据（10-30分钟）

从Binance Vision数据缓存中准备V8回测所需的数据：

```bash
python data/prepare_v8_data.py
```

这个脚本会：
- 从现有的15分钟数据中提取数据
- 构建30m、1h、2h、4h、1d等高时间框架数据
- 保存到 `data_cache/v8_prepared/` 目录

### 3. 运行完整回测（30-60分钟）

运行V8策略的完整回测并与V5-V7对比：

```bash
python main/run_v8_complete.py
```

这个脚本会：
- 运行V8策略的滚动回测（0.25y到5.0y）
- 生成详细的回测结果
- 与V5-V7进行对比分析
- 保存结果到 `artifacts/alpha_decay_v8/` 目录

## 📊 查看结果

### 回测结果文件结构

```
artifacts/alpha_decay_v8/
├── summary.csv                    # 汇总结果
├── version_comparison.csv          # 版本对比
├── 0.25y/
│   ├── equity.csv                # 权益曲线
│   ├── fills.csv                 # 成交记录
│   └── institutional_metrics.json # 机构指标
├── 0.5y/
│   ├── equity.csv
│   ├── fills.csv
│   └── institutional_metrics.json
├── ... (其他周期)
└── 5.0y/
    ├── equity.csv
    ├── fills.csv
    └── institutional_metrics.json
```

### 快速查看汇总结果

```bash
# 查看V8汇总结果
cat artifacts/alpha_decay_v8/summary.csv

# 查看版本对比
cat artifacts/alpha_decay_v8/version_comparison.csv
```

### 使用Python查看结果

```python
import pandas as pd

# 读取V8结果
v8_summary = pd.read_csv('artifacts/alpha_decay_v8/summary.csv')
print("V8策略汇总结果：")
print(v8_summary.to_string(index=False))

# 读取版本对比
comparison = pd.read_csv('artifacts/alpha_decay_v8/version_comparison.csv')
print("\n版本对比：")
print(comparison.to_string(index=False))
```

## ⚙️ 参数调整

### 调整入场参数

编辑 `main/run_v8_complete.py` 中的参数：

```python
v8_results = run_v8_backtest(
    symbols=["BTCUSDT", "ETHUSDT"],  # 交易品种
    periods=["0.25y", "0.5y", "0.75y", "1.0y", "2.0y", "3.0y", "4.0y", "5.0y"],  # 回测周期
    min_confidence=0.6,              # 最小置信度阈值（0.5-0.8）
    risk_per_trade=0.01,            # 每笔交易风险比例（0.005-0.02）
    output_dir="artifacts/alpha_decay_v8",
    prepare_data=False              # 是否重新准备数据
)
```

### 参数说明

**min_confidence（最小置信度阈值）**
- 范围：0.5 - 0.8
- 影响：控制入场信号的严格程度
- 建议：
  - 0.5：宽松，更多交易机会
  - 0.6：平衡（推荐）
  - 0.7：严格，高质量信号
  - 0.8：非常严格，少量高质量交易

**risk_per_trade（每笔交易风险比例）**
- 范围：0.005 - 0.02
- 影响：控制每笔交易的风险敞口
- 建议：
  - 0.005（0.5%）：保守
  - 0.01（1%）：平衡（推荐）
  - 0.015（1.5%）：积极
  - 0.02（2%）：激进

## 🔍 深入分析

### 分析特定周期的表现

```python
import pandas as pd
import matplotlib.pyplot as plt

# 读取1年周期的权益曲线
equity = pd.read_csv('artifacts/alpha_decay_v8/1.0y/equity.csv')
equity['close_ts'] = pd.to_datetime(equity['close_ts'])

# 绘制权益曲线
plt.figure(figsize=(12, 6))
plt.plot(equity['close_ts'], equity['equity'])
plt.title('V8 Strategy - 1 Year Equity Curve')
plt.xlabel('Time')
plt.ylabel('Equity')
plt.grid(True)
plt.show()
```

### 分析成交记录

```python
import pandas as pd

# 读取成交记录
fills = pd.read_csv('artifacts/alpha_decay_v8/1.0y/fills.csv')
fills['timestamp'] = pd.to_datetime(fills['timestamp'])

# 按原因分组统计
reason_counts = fills['reason'].value_counts()
print("出场原因统计：")
print(reason_counts)

# 计算每笔交易的盈亏
trades = fills[fills['action'] == 'close'].copy()
trades['pnl'] = trades.apply(
    lambda x: (x['price'] - x['notional']/x['quantity']) * x['quantity'] 
    if x['side'] == 'long' 
    else (x['notional']/x['quantity'] - x['price']) * x['quantity'],
    axis=1
)
print(f"\n总盈亏: {trades['pnl'].sum():.2f}")
print(f"平均盈亏: {trades['pnl'].mean():.2f}")
print(f"胜率: {(trades['pnl'] > 0).sum() / len(trades):.2%}")
```

## 🆚 与V5-V7对比

### 快速对比关键指标

```python
import pandas as pd

# 读取各版本结果
versions = ['V5', 'V6', 'V7', 'V8']
results = {}

for version in versions:
    path = f'artifacts/alpha_decay_{version.lower()}/summary.csv'
    try:
        df = pd.read_csv(path)
        results[version] = df
    except FileNotFoundError:
        print(f"警告：{version} 结果文件未找到")

# 对比1年和5年表现
for period in ['1.0y', '5.0y']:
    print(f"\n{period} 周期对比：")
    print("-" * 80)
    
    for version, df in results.items():
        row = df[df['period'] == period]
        if not row.empty:
            data = row.iloc[0]
            print(f"{version}: 收益={data['total_return']:.2%}, "
                  f"回撤={data['max_drawdown']:.2%}, "
                  f"PF={data['profit_factor']:.2f}, "
                  f"胜率={data['win_rate']:.2%}, "
                  f"交易数={data['trade_count']}")
```

## 🐛 常见问题

### 1. 模块导入错误

**问题**：`ModuleNotFoundError: No module named 'signals'`

**解决**：确保在项目根目录运行脚本，或设置正确的Python路径：

```bash
# 方法1：在项目根目录运行
cd e:\Project\inv-cry
python tests/test_v8_strategy.py

# 方法2：设置Python路径
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
python tests/test_v8_strategy.py
```

### 2. 数据文件未找到

**问题**：`FileNotFoundError: data_cache/binance_vision/futures_um/monthly/15m/...`

**解决**：
- 确保Binance Vision数据已下载到正确目录
- 检查数据文件是否存在
- 重新运行数据准备脚本

### 3. 回测结果为空

**问题**：回测运行但没有生成任何交易

**解决**：
- 降低 `min_confidence` 参数（如从0.6降到0.5）
- 检查数据是否完整
- 查看日志了解具体原因

### 4. 内存不足

**问题**：处理大量数据时出现内存错误

**解决**：
- 减少回测周期数量
- 分批处理数据
- 增加系统内存或使用更强大的机器

## 📚 进阶使用

### 自定义交易品种

修改 `main/run_v8_complete.py` 中的品种列表：

```python
v8_results = run_v8_backtest(
    symbols=["BTCUSDT", "ETHUSDT", "BNBUSDT"],  # 添加更多品种
    # ... 其他参数
)
```

### 自定义回测周期

```python
v8_results = run_v8_backtest(
    periods=["0.5y", "1.0y", "2.0y"],  # 只回测特定周期
    # ... 其他参数
)
```

### 参数网格搜索

```python
import pandas as pd

# 测试不同的min_confidence值
confidence_values = [0.5, 0.6, 0.7, 0.8]
results = []

for conf in confidence_values:
    print(f"\n测试 min_confidence={conf}")
    result = run_v8_backtest(
        min_confidence=conf,
        output_dir=f"artifacts/alpha_decay_v8_conf{conf}"
    )
    results.append({
        'min_confidence': conf,
        'total_return': result[result['period'] == '1.0y']['total_return'].values[0],
        'max_drawdown': result[result['period'] == '1.0y']['max_drawdown'].values[0],
        'profit_factor': result[result['period'] == '1.0y']['profit_factor'].values[0],
    })

# 比较结果
results_df = pd.DataFrame(results)
print("\n参数优化结果：")
print(results_df.to_string(index=False))
```

## 📞 获取帮助

如果遇到问题：

1. 查看详细文档：`V8_STRATEGY_REFACTOR.md`
2. 查看实施总结：`V8_IMPLEMENTATION_SUMMARY.md`
3. 检查日志文件：`logs/`
4. 运行功能测试：`python tests/test_v8_strategy.py`

## 🎯 下一步

1. ✅ 运行功能测试，验证基本功能
2. ✅ 准备回测数据
3. ✅ 运行完整回测
4. 📊 分析回测结果
5. ⚙️ 根据结果调整参数
6. 🔄 重新回测验证
7. 🚀 在模拟环境测试实盘表现

祝您使用愉快！🎉