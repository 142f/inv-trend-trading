"""
V8策略快速回测脚本 - 用于验证修复效果
"""

from __future__ import annotations

import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd
from config.loader import load_settings
from data.prepare_v8_data import load_prepared_dataset, filter_dataset_by_period
from backtest.simple_engine import SimpleBacktestEngine


def quick_test_v8():
    """快速测试V8策略"""
    print("=" * 80)
    print("V8 策略快速测试")
    print("=" * 80)
    
    # 加载配置
    settings = load_settings()
    
    # 加载已准备的数据
    print("\n加载数据...")
    dataset = load_prepared_dataset(
        symbols=["BTCUSDT", "ETHUSDT"],
        data_dir="data_cache/v8_prepared"
    )
    
    # 只测试0.25y周期
    print("过滤数据（0.25y周期）...")
    filtered_dataset = filter_dataset_by_period(dataset, "0.25y")
    
    # 创建回测引擎
    print("创建回测引擎...")
    engine = SimpleBacktestEngine(
        settings=settings,
        dataset=filtered_dataset,
        initial_equity=10000.0,
        min_confidence=0.6,
        risk_per_trade=0.01,
        max_positions=1
    )
    
    # 运行回测
    print("运行回测...")
    result = engine.run()
    
    # 输出结果
    print(f"\n{'=' * 80}")
    print("V8 策略快速测试结果")
    print(f"{'=' * 80}")
    print(f"信号生成数：{result.signals_generated}")
    print(f"实际开仓数：{result.positions_taken}")
    print(f"总交易次数：{result.total_trades}")
    print(f"胜率：{result.win_rate:.2%}")
    print(f"总收益：{result.total_return:.2%}")
    print(f"最大回撤：{result.max_drawdown:.2%}")
    print(f"盈亏比：{result.profit_factor:.2f}")
    
    # 显示权益曲线的前后几个点
    print(f"\n权益曲线样本：")
    print(result.equity_curve.head(10))
    print("...")
    print(result.equity_curve.tail(10))
    
    # 显示成交记录样本
    if result.fills:
        print(f"\n成交记录样本（前10条）：")
        for i, fill in enumerate(result.fills[:10]):
            print(f"{i+1}. {fill.timestamp} {fill.symbol} {fill.side} {fill.action} "
                  f"价格:{fill.price:.2f} 数量:{fill.quantity:.6f} 原因:{fill.reason}")
    
    return result


if __name__ == "__main__":
    try:
        result = quick_test_v8()
        print("\n✅ V8策略快速测试完成！")
    except Exception as e:
        print(f"\n❌ 测试失败：{e}")
        import traceback
        traceback.print_exc()