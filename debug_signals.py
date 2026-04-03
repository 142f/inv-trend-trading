import pandas as pd
from pathlib import Path
from main.run_backtest_binance_archive import _build_dataset
from backtest.engine import BacktestEngine
from config.loader import load_settings
import signals.pullback_reclaim as pr

s = load_settings('config/settings.yaml')
ds = _build_dataset(s, Path('data_cache/binance_vision/futures_um/monthly/15m'), pd.Timestamp('2025-10-01T00:00:00Z'), pd.Timestamp('2025-12-01T00:00:00Z'))

reasons = []
old_detect = pr.detect_reclaim_at_close
def hook_detect(df, close_ts, side, timeframe):
    sig = old_detect(df, close_ts, side, timeframe)
    reasons.append((sig.valid, timeframe))
    return sig
pr.detect_reclaim_at_close = hook_detect

eng = BacktestEngine(settings=s, dataset=ds)
eng.run()

from collections import Counter
print("Signals evaluated:", Counter(reasons))
