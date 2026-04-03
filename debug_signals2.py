import pandas as pd
from pathlib import Path
from main.run_backtest_binance_archive import _build_dataset
from backtest.engine import BacktestEngine
from config.loader import load_settings
import signals.entry_rules as er

s = load_settings('config/settings.yaml')
ds = _build_dataset(s, Path('data_cache/binance_vision/futures_um/monthly/15m'), pd.Timestamp('2025-10-01T00:00:00Z'), pd.Timestamp('2025-12-01T00:00:00Z'))

reasons = []
old_eval = er.evaluate_entry
def hook_eval(*args, **kwargs):
    dec = old_eval(*args, **kwargs)
    if not dec.can_enter:
        reasons.append(dec.reason)
    return dec
er.evaluate_entry = hook_eval

eng = BacktestEngine(settings=s, dataset=ds)
eng.run()

from collections import Counter
print("Rejection reasons:")
for k, v in Counter(reasons).most_common():
    print(k, v)
