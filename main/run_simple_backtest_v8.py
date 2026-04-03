"""
V8 简化策略回测脚本

与原有策略的核心区别：
1. 极简入场：基于EMA回撤形态 + 动量确认
2. 简化出场：动态跟踪止损 + 固定止盈目标（2R减仓50%，5R全部平仓）
3. 移除复杂的多重确认和状态机
4. 固定仓位管理：每笔交易风险1%
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict

import pandas as pd
import numpy as np

from config.loader import load_settings
from data.prepare_v8_data import load_prepared_dataset, filter_dataset_by_period
from backtest.simple_engine import SimpleBacktestEngine


def prepare_dataset_for_simple_backtest(
    symbols: list[str],
    periods: list[str],
    data_dir: str = "data_cache/v8_prepared"
) -> Dict[str, Dict[str, pd.DataFrame]]:
    """
    准备简化回测所需的数据集
    
    注意：这里简化了数据准备逻辑，实际使用时需要根据您的数据源调整
    """
    return load_prepared_dataset(symbols=symbols, data_dir=data_dir)


def run_simple_backtest_v8(
    symbols: list[str] = ["BTCUSDT", "ETHUSDT"],
    periods: list[str] = ["0.25y", "0.5y", "0.75y", "1.0y", "2.0y", "3.0y", "4.0y", "5.0y"],
    min_confidence: float = 0.6,
    risk_per_trade: float = 0.01,
    output_dir: str = "artifacts/alpha_decay_v8"
):
    """
    运行V8简化策略的滚动回测
    """
    print("=" * 80)
    print("V8 简化策略回测开始")
    print("=" * 80)
    
    # 创建输出目录
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    # 加载配置
    settings = load_settings()
    
    # 准备数据集
    print("\n正在准备数据集...")
    dataset = prepare_dataset_for_simple_backtest(symbols, periods)
    
    # 检查数据完整性
    for symbol in symbols:
        for tf in ["15m", "1h", "4h", "1d"]:
            if dataset[symbol][tf].empty:
                print(f"警告：{symbol} {tf} 数据为空，跳过该品种")
                continue
    
    # 运行滚动回测
    results_summary = []
    
    for period in periods:
        print(f"\n{'=' * 80}")
        print(f"正在回测周期：{period}")
        print(f"{'=' * 80}")
        
        # 根据周期过滤数据，确保不同周期回测使用不同历史窗口
        filtered_dataset = filter_dataset_by_period(dataset, period)
        
        # 创建回测引擎
        engine = SimpleBacktestEngine(
            settings=settings,
            dataset=filtered_dataset,
            initial_equity=10000.0,
            min_confidence=min_confidence,
            risk_per_trade=risk_per_trade,
            max_positions=1
        )
        
        # 运行回测
        result = engine.run()
        
        # 输出结果
        print(f"\n{period} 回测结果：")
        print(f"  信号生成数：{result.signals_generated}")
        print(f"  实际开仓数：{result.positions_taken}")
        print(f"  总交易次数：{result.total_trades}")
        print(f"  胜率：{result.win_rate:.2%}")
        print(f"  总收益：{result.total_return:.2%}")
        print(f"  最大回撤：{result.max_drawdown:.2%}")
        print(f"  盈亏比：{result.profit_factor:.2f}")
        
        # 保存结果
        period_dir = Path(output_dir) / period
        period_dir.mkdir(parents=True, exist_ok=True)
        
        # 保存权益曲线
        result.equity_curve.to_csv(period_dir / "equity.csv")
        
        # 保存成交记录
        if result.fills:
            fills_df = pd.DataFrame([
                {
                    "timestamp": fill.timestamp,
                    "symbol": fill.symbol,
                    "side": fill.side,
                    "action": fill.action,
                    "price": fill.price,
                    "quantity": fill.quantity,
                    "notional": fill.notional,
                    "fee": fill.fee,
                    "reason": fill.reason,
                }
                for fill in result.fills
            ])
            fills_df.to_csv(period_dir / "fills.csv", index=False)
        
        # 记录汇总数据
        results_summary.append({
            "period": period,
            "total_return": result.total_return,
            "max_drawdown": result.max_drawdown,
            "sharpe_rf0": 0.0,  # 简化版本暂不计算夏普比率
            "profit_factor": result.profit_factor,
            "win_rate": result.win_rate,
            "trade_count": result.total_trades,
            "signals_generated": result.signals_generated,
            "positions_taken": result.positions_taken,
        })
    
    # 保存汇总结果
    summary_df = pd.DataFrame(results_summary)
    summary_df.to_csv(Path(output_dir) / "summary.csv", index=False)
    
    # 输出最终汇总
    print(f"\n{'=' * 80}")
    print("V8 简化策略回测完成 - 汇总结果")
    print(f"{'=' * 80}")
    print(summary_df.to_string(index=False))
    
    print(f"\n结果已保存到：{output_dir}")
    
    return summary_df


def compare_with_previous_versions(
    v8_results: pd.DataFrame,
    v5_path: str = "artifacts/alpha_decay_v5/summary.csv",
    v6_path: str = "artifacts/alpha_decay_v6/summary.csv",
    v7_path: str = "artifacts/alpha_decay_v7/summary.csv"
):
    """
    与之前版本进行对比
    """
    print(f"\n{'=' * 80}")
    print("版本对比分析")
    print(f"{'=' * 80}")
    
    # 读取之前版本的结果
    previous_results = {}
    for version, path in [("V5", v5_path), ("V6", v6_path), ("V7", v7_path)]:
        try:
            df = pd.read_csv(path)
            previous_results[version] = df
        except FileNotFoundError:
            print(f"警告：{version} 结果文件未找到：{path}")
    
    # 对比关键指标
    key_periods = ["1.0y", "5.0y"]
    
    for period in key_periods:
        print(f"\n{period} 周期对比：")
        print("-" * 80)
        
        # V8结果
        v8_row = v8_results[v8_results["period"] == period]
        if not v8_row.empty:
            v8_data = v8_row.iloc[0]
            print(f"V8: 收益={v8_data['total_return']:.2%}, 回撤={v8_data['max_drawdown']:.2%}, "
                  f"PF={v8_data['profit_factor']:.2f}, 胜率={v8_data['win_rate']:.2%}, "
                  f"交易数={v8_data['trade_count']}")
        
        # 之前版本结果
        for version, df in previous_results.items():
            row = df[df["period"] == period]
            if not row.empty:
                data = row.iloc[0]
                print(f"{version}: 收益={data['total_return']:.2%}, 回撤={data['max_drawdown']:.2%}, "
                      f"PF={data['profit_factor']:.2f}, 胜率={data['win_rate']:.2%}, "
                      f"交易数={data['trade_count']}")
    
    # 分析V8的优势和劣势
    print(f"\n{'=' * 80}")
    print("V8策略分析")
    print(f"{'=' * 80}")
    
    print("\nV8策略的核心改进：")
    print("1. 极简入场逻辑：移除了复杂的MACD共振、多重确认等过滤条件")
    print("2. 简化出场机制：统一的动态跟踪止损 + 固定止盈目标")
    print("3. 固定风险管理：每笔交易风险1%，避免过度复杂的仓位管理")
    print("4. 移除状态机：不再有复杂的State 1/2、TP1/TP2等状态")
    
    print("\n预期效果：")
    print("- 短期表现：应该比V6有显著改善，接近V5的水平")
    print("- 长期表现：应该比V5更稳健，接近V6的水平")
    print("- 交易频率：介于V5和V6之间，避免过度过滤")
    print("- 风险控制：通过固定风险比例和动态止损，保持良好的风险收益比")


if __name__ == "__main__":
    # 运行V8简化策略回测
    v8_results = run_simple_backtest_v8(
        symbols=["BTCUSDT", "ETHUSDT"],
        periods=["0.25y", "0.5y", "0.75y", "1.0y", "2.0y", "3.0y", "4.0y", "5.0y"],
        min_confidence=0.6,
        risk_per_trade=0.01,
        output_dir="artifacts/alpha_decay_v8"
    )
    
    # 与之前版本对比
    compare_with_previous_versions(v8_results)