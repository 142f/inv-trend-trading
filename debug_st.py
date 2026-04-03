import pandas as pd
from pathlib import Path
from main.run_backtest_binance_archive import _build_dataset
from backtest.engine import BacktestEngine
from config.loader import load_settings

s = load_settings('config/settings.yaml')
ds = _build_dataset(s, Path('data_cache/binance_vision/futures_um/monthly/15m'), pd.Timestamp('2025-10-01T00:00:00Z'), pd.Timestamp('2025-12-01T00:00:00Z'))

for symbol in ["ETHUSDT"]:
    for tf in ["1d", "4h", "2h", "1h", "30m", "15m"]:
        df = ds[symbol][tf]
        print(f"{tf} trends:", df['trend_state'].value_counts().to_dict() if 'trend_state' in df.columns else "MISSING")
