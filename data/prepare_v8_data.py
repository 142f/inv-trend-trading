"""
为V8简化策略准备回测数据

这个脚本会从现有的Binance Vision数据缓存中准备V8回测所需的数据
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict
import pandas as pd
import zipfile
import os


def extract_kline_data_from_zip(zip_path: Path, symbol: str, timeframe: str) -> pd.DataFrame:
    """
    从Binance Vision的zip文件中提取K线数据
    """
    try:
        with zipfile.ZipFile(zip_path, 'r') as zip_ref:
            # 查找匹配的CSV文件
            csv_files = [f for f in zip_ref.namelist() if f.endswith('.csv')]
            
            if not csv_files:
                return pd.DataFrame()
            
            # 读取第一个CSV文件
            with zip_ref.open(csv_files[0]) as csv_file:
                df = pd.read_csv(csv_file)
                
                # 标准化列名
                if 'close_time' in df.columns:
                    df['close_ts'] = pd.to_datetime(df['close_time'], unit='ms')
                elif 'timestamp' in df.columns:
                    df['close_ts'] = pd.to_datetime(df['timestamp'], unit='ms')
                elif 'time' in df.columns:
                    df['close_ts'] = pd.to_datetime(df['time'], unit='ms')
                
                # 确保必要的列存在
                required_cols = ['open', 'high', 'low', 'close']
                if all(col in df.columns for col in required_cols):
                    return df[['open', 'high', 'low', 'close', 'volume', 'close_ts']]
                
                return pd.DataFrame()
    
    except Exception as e:
        print(f"提取数据失败 {zip_path}: {e}")
        return pd.DataFrame()


def build_higher_timeframe(base_df: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """
    从15分钟数据构建更高时间框架的数据
    
    target_tf: '30m', '1h', '2h', '4h', '1d'
    """
    if base_df.empty:
        return pd.DataFrame()
    
    # 设置时间索引
    df = base_df.copy()
    df.set_index('close_ts', inplace=True)
    
    # 根据目标时间框架确定重采样规则
    resample_rules = {
        '30m': '30min',
        '1h': '1h',
        '2h': '2h',
        '4h': '4h',
        '1d': '1D'
    }
    
    rule = resample_rules.get(target_tf, '1H')
    
    # 重采样
    resampled = df.resample(rule).agg({
        'open': 'first',
        'high': 'max',
        'low': 'min',
        'close': 'last',
        'volume': 'sum'
    }).dropna()
    
    # 重置索引
    resampled.reset_index(inplace=True)
    
    return resampled


def prepare_v8_dataset(
    symbols: list[str] = ["BTCUSDT", "ETHUSDT"],
    data_cache_dir: str = "data_cache/binance_vision/futures_um/monthly/15m",
    output_dir: str = "data_cache/v8_prepared"
) -> Dict[str, Dict[str, pd.DataFrame]]:
    """
    准备V8回测所需的数据集
    
    Returns:
        Dict[symbol, Dict[timeframe, DataFrame]]
    """
    print("=" * 80)
    print("准备V8简化策略回测数据")
    print("=" * 80)
    
    data_cache_path = Path(data_cache_dir)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    dataset: Dict[str, Dict[str, pd.DataFrame]] = {}
    
    for symbol in symbols:
        print(f"\n处理品种：{symbol}")
        dataset[symbol] = {}
        
        # 1. 加载15分钟基础数据
        print(f"  加载15分钟数据...")
        zip_pattern = f"{symbol}-15m-*.zip"
        zip_files = sorted(data_cache_path.glob(zip_pattern))
        
        if not zip_files:
            print(f"  警告：未找到{symbol}的15分钟数据文件")
            continue
        
        # 合并所有月份的数据
        dfs_15m = []
        for zip_file in zip_files:
            df = extract_kline_data_from_zip(zip_file, symbol, "15m")
            if not df.empty:
                dfs_15m.append(df)
        
        if not dfs_15m:
            print(f"  警告：{symbol}的15分钟数据为空")
            continue
        
        df_15m = pd.concat(dfs_15m, ignore_index=True)
        df_15m = df_15m.sort_values('close_ts').reset_index(drop=True)
        
        # 去除重复数据
        df_15m = df_15m.drop_duplicates(subset=['close_ts'], keep='last')
        
        print(f"  15分钟数据：{len(df_15m)}根K线")
        print(f"  时间范围：{df_15m['close_ts'].min()} 到 {df_15m['close_ts'].max()}")
        
        dataset[symbol]["15m"] = df_15m
        
        # 2. 构建更高时间框架的数据
        timeframes = ['30m', '1h', '2h', '4h', '1d']
        for tf in timeframes:
            print(f"  构建{tf}数据...")
            df_tf = build_higher_timeframe(df_15m, tf)
            print(f"  {tf}数据：{len(df_tf)}根K线")
            dataset[symbol][tf] = df_tf
        
        # 3. 保存到输出目录
        symbol_output_dir = output_path / symbol
        symbol_output_dir.mkdir(parents=True, exist_ok=True)
        
        for tf, df in dataset[symbol].items():
            output_file = symbol_output_dir / f"{symbol}-{tf}.csv"
            df.to_csv(output_file, index=False)
            print(f"  已保存：{output_file}")
    
    print(f"\n{'=' * 80}")
    print("数据准备完成")
    print(f"{'=' * 80}")
    
    return dataset


def load_prepared_dataset(
    symbols: list[str] = ["BTCUSDT", "ETHUSDT"],
    data_dir: str = "data_cache/v8_prepared"
) -> Dict[str, Dict[str, pd.DataFrame]]:
    """
    从预处理的数据目录加载数据集
    """
    dataset: Dict[str, Dict[str, pd.DataFrame]] = {}
    data_path = Path(data_dir)
    
    for symbol in symbols:
        dataset[symbol] = {}
        symbol_dir = data_path / symbol
        
        if not symbol_dir.exists():
            print(f"警告：{symbol}的数据目录不存在：{symbol_dir}")
            continue
        
        timeframes = ['15m', '30m', '1h', '2h', '4h', '1d']
        for tf in timeframes:
            csv_file = symbol_dir / f"{symbol}-{tf}.csv"
            if csv_file.exists():
                df = pd.read_csv(csv_file)
                df['close_ts'] = pd.to_datetime(df['close_ts'])
                dataset[symbol][tf] = df
            else:
                print(f"警告：{symbol} {tf}数据文件不存在：{csv_file}")
                dataset[symbol][tf] = pd.DataFrame()
    
    return dataset


def filter_dataset_by_period(
    dataset: Dict[str, Dict[str, pd.DataFrame]],
    period: str,
    end_date: pd.Timestamp = None
) -> Dict[str, Dict[str, pd.DataFrame]]:
    """
    根据时间周期过滤数据集
    
    period: '0.25y', '0.5y', '0.75y', '1.0y', '2.0y', '3.0y', '4.0y', '5.0y'
    """
    period_mapping = {
        '0.25y': 0.25,
        '0.5y': 0.5,
        '0.75y': 0.75,
        '1.0y': 1.0,
        '2.0y': 2.0,
        '3.0y': 3.0,
        '4.0y': 4.0,
        '5.0y': 5.0
    }
    
    years = period_mapping.get(period, 1.0)
    
    # 如果没有指定结束日期，使用数据的最大日期
    if end_date is None:
        max_dates = []
        for symbol_data in dataset.values():
            for df in symbol_data.values():
                if not df.empty:
                    max_dates.append(df['close_ts'].max())
        
        if max_dates:
            end_date = max(max_dates)
        else:
            end_date = pd.Timestamp.now()
    
    start_date = end_date - pd.Timedelta(days=int(365 * years))
    
    # 过滤数据
    filtered_dataset: Dict[str, Dict[str, pd.DataFrame]] = {}
    
    for symbol, symbol_data in dataset.items():
        filtered_dataset[symbol] = {}
        for tf, df in symbol_data.items():
            if not df.empty:
                filtered_df = df[
                    (df['close_ts'] >= start_date) & 
                    (df['close_ts'] <= end_date)
                ].copy()
                filtered_dataset[symbol][tf] = filtered_df
            else:
                filtered_dataset[symbol][tf] = pd.DataFrame()
    
    return filtered_dataset


if __name__ == "__main__":
    # 准备数据
    dataset = prepare_v8_dataset(
        symbols=["BTCUSDT", "ETHUSDT"],
        data_cache_dir="data_cache/binance_vision/futures_um/monthly/15m",
        output_dir="data_cache/v8_prepared"
    )
    
    # 测试加载
    print("\n测试加载数据...")
    loaded_dataset = load_prepared_dataset(
        symbols=["BTCUSDT", "ETHUSDT"],
        data_dir="data_cache/v8_prepared"
    )
    
    for symbol, symbol_data in loaded_dataset.items():
        print(f"\n{symbol}:")
        for tf, df in symbol_data.items():
            if not df.empty:
                print(f"  {tf}: {len(df)}根K线, {df['close_ts'].min()} 到 {df['close_ts'].max()}")
            else:
                print(f"  {tf}: 空数据")