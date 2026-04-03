import pandas as pd
from pathlib import Path
from main.run_backtest_binance_archive import _build_dataset
from backtest.engine import BacktestEngine
from config.loader import load_settings
import signals.resonance as sr

s = load_settings('config/settings.yaml')
ds = _build_dataset(s, Path('data_cache/binance_vision/futures_um/monthly/15m'), pd.Timestamp('2025-10-01T00:00:00Z'), pd.Timestamp('2025-12-01T00:00:00Z'))

scores = []
old_gate = sr.direction_gate
def hook_gate(states, weights, long_gate, short_gate):
    gate = old_gate(states, weights, long_gate, short_gate)
    scores.append(gate.r_score)
    return gate
sr.direction_gate = hook_gate

eng = BacktestEngine(settings=s, dataset=ds)
eng.run()

s_df = pd.Series(scores)
print("Total rows:", len(s_df))
print(s_df.describe())
print("> 0.6 count:", (s_df >= 0.6).sum())
print("< -0.6 count:", (s_df <= -0.6).sum())