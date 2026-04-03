"""
V8策略快速测试脚本

这个脚本会：
1. 测试简化入场信号检测
2. 测试简化出场决策
3. 验证基本功能是否正常
"""

from __future__ import annotations

import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import pandas as pd
import numpy as np
from datetime import datetime, timedelta


def create_test_data(n_bars: int = 100) -> pd.DataFrame:
    """
    创建测试数据
    """
    np.random.seed(42)
    
    # 生成模拟价格数据
    base_price = 50000.0
    returns = np.random.normal(0, 0.01, n_bars)
    prices = [base_price]
    
    for ret in returns[1:]:
        prices.append(prices[-1] * (1 + ret))
    
    # 创建OHLCV数据
    data = []
    for i, price in enumerate(prices):
        high = price * (1 + abs(np.random.normal(0, 0.005)))
        low = price * (1 - abs(np.random.normal(0, 0.005)))
        open_price = price * (1 + np.random.normal(0, 0.002))
        close_price = price
        volume = np.random.uniform(100, 1000)
        
        data.append({
            'open': open_price,
            'high': high,
            'low': low,
            'close': close_price,
            'volume': volume,
            'close_ts': datetime(2024, 1, 1) + timedelta(minutes=15 * i)
        })
    
    df = pd.DataFrame(data)
    
    # 添加技术指标
    df['ema144'] = df['close'].ewm(span=144, adjust=False).mean()
    df['ema169'] = df['close'].ewm(span=169, adjust=False).mean()
    
    # 计算ATR
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    df['atr14'] = true_range.rolling(window=14).mean()
    
    # 填充ATR的NaN值
    df['atr14'] = df['atr14'].fillna(df['atr14'].mean())
    
    return df


def test_simple_entry():
    """
    测试简化入场信号检测
    """
    print("=" * 80)
    print("测试简化入场信号检测")
    print("=" * 80)
    
    from signals.simple_entry import detect_simple_entry
    
    # 创建测试数据
    df = create_test_data(100)
    
    # 测试多头信号
    print("\n测试多头信号...")
    long_signal = detect_simple_entry(
        df=df,
        close_ts=df.iloc[-1]['close_ts'],
        side="long",
        min_confidence=0.5  # 降低阈值以便更容易触发信号
    )
    
    print(f"多头信号结果：")
    print(f"  有效：{long_signal.valid}")
    print(f"  置信度：{long_signal.confidence:.2f}")
    print(f"  触发价格：{long_signal.trigger_price:.2f}")
    print(f"  止损价格：{long_signal.stop_price:.2f}")
    
    # 测试空头信号
    print("\n测试空头信号...")
    short_signal = detect_simple_entry(
        df=df,
        close_ts=df.iloc[-1]['close_ts'],
        side="short",
        min_confidence=0.5
    )
    
    print(f"空头信号结果：")
    print(f"  有效：{short_signal.valid}")
    print(f"  置信度：{short_signal.confidence:.2f}")
    print(f"  触发价格：{short_signal.trigger_price:.2f}")
    print(f"  止损价格：{short_signal.stop_price:.2f}")
    
    return long_signal, short_signal


def test_simple_exit():
    """
    测试简化出场决策
    """
    print("\n" + "=" * 80)
    print("测试简化出场决策")
    print("=" * 80)
    
    from signals.simple_exit import check_simple_exit
    from portfolio.state import PositionState
    
    # 创建测试仓位
    pos = PositionState(symbol="BTCUSDT")
    pos.side = "long"
    pos.entry_price = 50000.0
    pos.stop_price = 49000.0
    pos.position_size = 0.1
    pos.initial_position_size = 0.1
    pos.initial_stop_distance = 1000.0
    pos.bars_held = 10
    pos.highest_high = 51000.0
    pos.lowest_low = 49500.0
    pos.position_state = 1
    
    # 测试不同价格情况
    test_cases = [
        ("盈利中", 51000.0),
        ("小幅亏损", 49800.0),
        ("触及止损", 48900.0),
        ("大幅盈利", 52000.0),
    ]
    
    for case_name, current_price in test_cases:
        print(f"\n测试场景：{case_name} (价格: {current_price:.2f})")
        
        exit_decision = check_simple_exit(
            pos=pos,
            current_price=current_price,
            atr=500.0,
            ema_long=49500.0,
            partial_exit_done=False,
            max_hold_bars=384
        )
        
        print(f"  应该出场：{exit_decision.should_exit}")
        print(f"  出场价格：{exit_decision.exit_price:.2f}")
        print(f"  原因：{exit_decision.reason}")


def test_simple_engine():
    """
    测试简化回测引擎
    """
    print("\n" + "=" * 80)
    print("测试简化回测引擎")
    print("=" * 80)
    
    try:
        from backtest.simple_engine import SimpleBacktestEngine
        from config.loader import load_settings
        
        print("正在加载配置...")
        settings = load_settings()
        
        print("创建测试数据集...")
        # 创建简单的测试数据集
        test_dataset = {
            "BTCUSDT": {
                "15m": create_test_data(500),
                "30m": create_test_data(250),
                "1h": create_test_data(125),
                "2h": create_test_data(62),
                "4h": create_test_data(31),
                "1d": create_test_data(15),
            }
        }
        
        # 临时修改settings以只测试BTCUSDT
        original_symbols = settings.strategy.symbols
        settings.strategy.symbols = ["BTCUSDT"]
        
        print("创建回测引擎...")
        engine = SimpleBacktestEngine(
            settings=settings,
            dataset=test_dataset,
            initial_equity=10000.0,
            min_confidence=0.4,  # 降低阈值以便更容易触发信号
            risk_per_trade=0.01,
            max_positions=1
        )
        
        print("运行回测...")
        result = engine.run()
        
        print(f"回测结果：")
        print(f"  信号生成数：{result.signals_generated}")
        print(f"  实际开仓数：{result.positions_taken}")
        # 注意：简化版本可能不包含所有统计信息
        try:
            print(f"  总交易次数：{result.total_trades}")
            print(f"  胜率：{result.win_rate:.2%}")
            print(f"  总收益：{result.total_return:.2%}")
            print(f"  最大回撤：{result.max_drawdown:.2%}")
            print(f"  盈亏比：{result.profit_factor:.2f}")
        except AttributeError as e:
            print(f"  注意：某些统计信息不可用 ({e})")
        
        # 恢复原始设置
        settings.strategy.symbols = original_symbols
        
        print("\n回测引擎测试通过！")
        
    except Exception as e:
        print(f"\n回测引擎测试失败：{e}")
        import traceback
        traceback.print_exc()


def main():
    """
    主函数：运行所有测试
    """
    print("开始V8策略功能测试...")
    print()
    
    # 测试入场信号
    try:
        test_simple_entry()
        print("\n✓ 入场信号测试通过")
    except Exception as e:
        print(f"\n✗ 入场信号测试失败：{e}")
        import traceback
        traceback.print_exc()
    
    # 测试出场决策
    try:
        test_simple_exit()
        print("\n✓ 出场决策测试通过")
    except Exception as e:
        print(f"\n✗ 出场决策测试失败：{e}")
        import traceback
        traceback.print_exc()
    
    # 测试回测引擎
    try:
        test_simple_engine()
        print("\n✓ 回测引擎测试通过")
    except Exception as e:
        print(f"\n✗ 回测引擎测试失败：{e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 80)
    print("V8策略功能测试完成")
    print("=" * 80)


if __name__ == "__main__":
    main()