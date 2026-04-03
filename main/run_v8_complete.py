"""
V8简化策略回测脚本 - 完整可运行版本

这个脚本会：
1. 准备V8回测所需的数据
2. 运行V8策略的滚动回测
3. 与V5-V7进行对比分析
"""

from __future__ import annotations

import sys
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from typing import Dict, List
import pandas as pd
import numpy as np

from config.loader import load_settings
from data.prepare_v8_data import prepare_v8_dataset, load_prepared_dataset, filter_dataset_by_period
from backtest.simple_engine import SimpleBacktestEngine


def run_v8_backtest(
    symbols: List[str] = ["BTCUSDT", "ETHUSDT"],
    periods: List[str] = ["0.25y", "0.5y", "0.75y", "1.0y", "2.0y", "3.0y", "4.0y", "5.0y"],
    min_confidence: float = 0.6,
    risk_per_trade: float = 0.01,
    output_dir: str = "artifacts/alpha_decay_v8",
    prepare_data: bool = True
) -> pd.DataFrame:
    """
    运行V8简化策略的完整回测
    
    Args:
        symbols: 交易品种列表
        periods: 回测周期列表
        min_confidence: 最小置信度阈值
        risk_per_trade: 每笔交易风险比例
        output_dir: 输出目录
        prepare_data: 是否准备新数据
    
    Returns:
        回测结果汇总DataFrame
    """
    print("=" * 80)
    print("V8 简化策略回测")
    print("=" * 80)
    
    # 创建输出目录
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    # 加载配置
    settings = load_settings()
    
    # 准备数据
    if prepare_data:
        print("\n第1步：准备数据...")
        dataset = prepare_v8_dataset(
            symbols=symbols,
            data_cache_dir="data_cache/binance_vision/futures_um/monthly/15m",
            output_dir="data_cache/v8_prepared"
        )
    else:
        print("\n第1步：加载数据...")
        dataset = load_prepared_dataset(
            symbols=symbols,
            data_dir="data_cache/v8_prepared"
        )
    
    # 检查数据完整性
    print("\n第2步：检查数据完整性...")
    for symbol in symbols:
        if symbol not in dataset:
            print(f"  警告：{symbol} 数据缺失")
            continue
        
        for tf in ["15m", "1h", "4h", "1d"]:
            if dataset[symbol][tf].empty:
                print(f"  警告：{symbol} {tf} 数据为空")
    
    # 运行滚动回测
    print("\n第3步：运行滚动回测...")
    results_summary = []
    
    for period in periods:
        print(f"\n{'=' * 80}")
        print(f"回测周期：{period}")
        print(f"{'=' * 80}")
        
        # 过滤数据
        filtered_dataset = filter_dataset_by_period(dataset, period)
        
        # 检查过滤后的数据
        has_valid_data = False
        for symbol in symbols:
            for tf in ["15m", "1h", "4h", "1d"]:
                if not filtered_dataset[symbol][tf].empty:
                    has_valid_data = True
                    break
            if has_valid_data:
                break
        
        if not has_valid_data:
            print(f"  跳过：无有效数据")
            continue
        
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
        try:
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
            else:
                # 创建空的fills.csv
                pd.DataFrame(columns=[
                    "timestamp", "symbol", "side", "action", 
                    "price", "quantity", "notional", "fee", "reason"
                ]).to_csv(period_dir / "fills.csv", index=False)
            
            # 保存机构指标（简化版本）
            institutional_metrics = {
                "total_trades": result.total_trades,
                "winning_trades": engine.winning_trades,
                "losing_trades": engine.losing_trades,
                "gross_profit": engine.gross_profit,
                "gross_loss": engine.gross_loss,
                "profit_factor": result.profit_factor,
                "win_rate": result.win_rate,
                "avg_win": engine.gross_profit / engine.winning_trades if engine.winning_trades > 0 else 0,
                "avg_loss": engine.gross_loss / engine.losing_trades if engine.losing_trades > 0 else 0,
                "signals_generated": result.signals_generated,
                "positions_taken": result.positions_taken,
            }
            
            import json
            with open(period_dir / "institutional_metrics.json", 'w') as f:
                json.dump(institutional_metrics, f, indent=2, default=str)
            
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
            
        except Exception as e:
            print(f"  回测失败：{e}")
            import traceback
            traceback.print_exc()
            continue
    
    # 保存汇总结果
    if results_summary:
        summary_df = pd.DataFrame(results_summary)
        summary_df.to_csv(Path(output_dir) / "summary.csv", index=False)
        
        # 输出最终汇总
        print(f"\n{'=' * 80}")
        print("V8 简化策略回测完成 - 汇总结果")
        print(f"{'=' * 80}")
        print(summary_df.to_string(index=False))
        
        print(f"\n结果已保存到：{output_dir}")
        
        return summary_df
    else:
        print("\n警告：没有生成任何回测结果")
        return pd.DataFrame()


def compare_with_previous_versions(
    v8_results: pd.DataFrame,
    output_dir: str = "artifacts/alpha_decay_v8"
):
    """
    与之前版本进行对比分析
    """
    print(f"\n{'=' * 80}")
    print("版本对比分析")
    print(f"{'=' * 80}")
    
    # 读取之前版本的结果
    previous_results = {}
    version_paths = {
        "V5": "artifacts/alpha_decay_v5/summary.csv",
        "V6": "artifacts/alpha_decay_v6/summary.csv",
        "V7": "artifacts/alpha_decay_v7/summary.csv"
    }
    
    for version, path in version_paths.items():
        try:
            df = pd.read_csv(path)
            previous_results[version] = df
        except FileNotFoundError:
            print(f"警告：{version} 结果文件未找到：{path}")
        except Exception as e:
            print(f"警告：读取{version} 结果失败：{e}")
    
    if not previous_results:
        print("没有找到之前版本的结果，跳过对比")
        return
    
    # 对比关键指标
    key_periods = ["1.0y", "5.0y"]
    
    comparison_data = []
    
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
            
            comparison_data.append({
                "version": "V8",
                "period": period,
                "total_return": v8_data['total_return'],
                "max_drawdown": v8_data['max_drawdown'],
                "profit_factor": v8_data['profit_factor'],
                "win_rate": v8_data['win_rate'],
                "trade_count": v8_data['trade_count']
            })
        
        # 之前版本结果
        for version, df in previous_results.items():
            row = df[df["period"] == period]
            if not row.empty:
                data = row.iloc[0]
                print(f"{version}: 收益={data['total_return']:.2%}, 回撤={data['max_drawdown']:.2%}, "
                      f"PF={data['profit_factor']:.2f}, 胜率={data['win_rate']:.2%}, "
                      f"交易数={data['trade_count']}")
                
                comparison_data.append({
                    "version": version,
                    "period": period,
                    "total_return": data['total_return'],
                    "max_drawdown": data['max_drawdown'],
                    "profit_factor": data['profit_factor'],
                    "win_rate": data['win_rate'],
                    "trade_count": data['trade_count']
                })
    
    # 保存对比结果
    if comparison_data:
        comparison_df = pd.DataFrame(comparison_data)
        comparison_df.to_csv(Path(output_dir) / "version_comparison.csv", index=False)
        print(f"\n对比结果已保存到：{Path(output_dir) / 'version_comparison.csv'}")
    
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


def main():
    """
    主函数：运行完整的V8回测流程
    """
    print("开始V8简化策略回测...")
    
    # 运行V8回测
    v8_results = run_v8_backtest(
        symbols=["BTCUSDT", "ETHUSDT"],
        periods=["0.25y", "0.5y", "0.75y", "1.0y", "2.0y", "3.0y", "4.0y", "5.0y"],
        min_confidence=0.6,
        risk_per_trade=0.01,
        output_dir="artifacts/alpha_decay_v8",
        prepare_data=False  # 使用已准备的数据
    )
    
    # 与之前版本对比
    if not v8_results.empty:
        compare_with_previous_versions(v8_results)
    
    print("\nV8简化策略回测完成！")


if __name__ == "__main__":
    main()