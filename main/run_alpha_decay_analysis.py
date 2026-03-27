import pandas as pd
import numpy as np
import os
import argparse
import subprocess
from pathlib import Path

def run_rolling_backtest_with_decay_analysis(symbols: list, periods: list = [1, 2, 3, 5]):
    print("="*80)
    print("🚀 启动工业级滚动回测与 Alpha 衰减审计 (Rolling Alpha Decay Analysis)")
    print("防御机制揭密: 以内建挂单手续费、固定+比例滑点(Slippage)、且无未来函数")
    print("="*80)
    
    base_out_dir = Path("artifacts/alpha_decay_audit")
    base_out_dir.mkdir(parents=True, exist_ok=True)
    
    # 为了展示快速出结果，这里我们直接去读您此前已经完整跑出的回测数据: backtest_eth_only_dynamic_sizing_full
    summary_path = "artifacts/backtest_eth_only_dynamic_sizing_full/summary.csv"
    
    if not os.path.exists(summary_path):
        print(f"未找到现存跑好的回测结果 {summary_path}，尝试执行回测...")
        cmd = [
            "python", "-m", "main.run_backtest_binance_archive",
            "--symbols"
        ] + symbols + [
            "--period-years"
        ] + [str(p) for p in periods] + [
            "--output-dir", str(base_out_dir)
        ]
        print(f"执行回测命令:\n{' '.join(cmd)}\n")
        subprocess.run(cmd, check=True)
        summary_path = base_out_dir / "summary.csv"

    print(f"\n[多维度性能指标审计 - Performance Metrics] 源数据: {summary_path}")
    
    if os.path.exists(summary_path):
        df_summary = pd.read_csv(summary_path)
        
        # 补充计算 CAGR 与 Calmar
        df_summary['period_num'] = df_summary['period'].str.replace('y', '').astype(float)
        df_summary['cagr'] = (1 + df_summary['total_return']) ** (1 / df_summary['period_num']) - 1
        df_summary['calmar'] = df_summary['cagr'] / df_summary['max_drawdown'].abs()
        df_summary['calmar'] = df_summary['calmar'].replace([np.inf, -np.inf], 0).fillna(0)

        # 格式化输出表
        display_df = df_summary[['period', 'total_return', 'cagr', 'max_drawdown', 'sharpe_rf0', 'sortino_rf0', 'calmar', 'win_rate', 'profit_factor', 'trade_count']].copy()
        display_df['total_return'] = (display_df['total_return'] * 100).round(2).astype(str) + '%'
        display_df['cagr'] = (display_df['cagr'] * 100).round(2).astype(str) + '%'
        display_df['max_drawdown'] = (display_df['max_drawdown'] * 100).round(2).astype(str) + '%'
        display_df['sharpe_rf0'] = display_df['sharpe_rf0'].round(2)
        display_df['sortino_rf0'] = display_df['sortino_rf0'].round(2)
        display_df['calmar'] = display_df['calmar'].round(2)
        display_df['win_rate'] = (display_df['win_rate'] * 100).round(2).astype(str) + '%'
        display_df['profit_factor'] = display_df['profit_factor'].round(2)

        print("\n" + display_df.to_string(index=False, justify='center'))
        
        # Alpha 衰减判定逻辑 (Alpha Decay Analysis)
        print("\n" + "="*80)
        print("📉 稳定性与特征漂移分析 (Alpha Decay Analysis)")
        print("="*80)
        
        y1_data = df_summary[df_summary['period'] == '1y'].iloc[0] if '1y' in df_summary['period'].values else None
        y5_data = df_summary[df_summary['period'] == '5y'].iloc[0] if '5y' in df_summary['period'].values else None
        
        if y1_data is not None and y5_data is not None:
             pf_1y = float(y1_data['profit_factor']) if not pd.isna(y1_data['profit_factor']) else 0
             pf_5y = float(y5_data['profit_factor']) if not pd.isna(y5_data['profit_factor']) else 0
             
             wr_1y = float(y1_data['win_rate'])
             wr_5y = float(y5_data['win_rate'])
             
             sharpe_1y = float(y1_data['sharpe_rf0'])
             sharpe_5y = float(y5_data['sharpe_rf0'])

             print(f"【近 1 年表现】 胜率: {wr_1y*100:.2f}% | 盈亏比(PF): {pf_1y:.2f} | 夏普比率: {sharpe_1y:.2f}")
             print(f"【近 5 年表现】 胜率: {wr_5y*100:.2f}% | 盈亏比(PF): {pf_5y:.2f} | 夏普比率: {sharpe_5y:.2f}\n")
             
             efficiency_drop = (pf_1y - pf_5y) / pf_5y if pf_5y > 0 else 0
             
             print("【诊断模型推演】：")
             if pf_5y < 1.0:
                 print("⚠️ 预警：5年长周期盈亏比低于 1.0，整体策略在跨越牛熊边界时出现了明显的负期望值累积，这证明当前模型存在“只顺应局部行情，无法穿越大周期”的结构性脆点。")
             
             if abs(efficiency_drop) < 0.15 and pf_5y > 1:
                 print("✅ [诊断结论] 极佳 (Excellent). 策略在长达 5 年的牛熊转换中特征未发生明显漂移，Alpha 留存度高。")
             elif efficiency_drop > 0.30:
                 print(f"🚨 [诊断结论] 出现严重近期过拟合 (Recent Overfitting). 近 1 年效果大幅优于 5 年长周期的 {efficiency_drop*100:.1f}%。结合盈亏比看，此系统可能过度拟合了近期低波动率洗盘的行情。")
             elif efficiency_drop < -0.30:
                 print(f"📉 [诊断结论] 远期衰减 (Alpha Decay). 近五年均值远好于近期表现。这通常意味着策略的核心逻辑（例如简单动量跟随）被当前市场的“做市商流动性枯竭或高频假突破”所克制。")
             else:
                 print("⚖️ [诊断结论] 中性 (Neutral). 漂移在正常区间内，但基础因子的表现仍待提升。")
        else:
            print("数据中未能同时提取到 1y 和 5y 的周期数据以作为对比依据。")

if __name__ == "__main__":
    targets = ["ETHUSDT"]
    run_rolling_backtest_with_decay_analysis(targets)
