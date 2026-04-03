import pandas as pd
from pathlib import Path
from main.run_backtest_binance_archive import _build_dataset
from backtest.engine import BacktestEngine
from config.loader import load_settings
import signals.entry_rules as er
import warnings
warnings.filterwarnings('ignore')

s = load_settings('config/settings.yaml')
ds = _build_dataset(s, Path('data_cache/binance_vision/futures_um/monthly/15m'), pd.Timestamp('2025-10-01T00:00:00Z'), pd.Timestamp('2025-11-15T00:00:00Z'))

reasons = []
old_eval = er.evaluate_entry
def hook_eval(*args, **kwargs):
    dec = old_eval(*args, **kwargs)
    if not dec.can_enter:
        reasons.append(dec.reason)
    return dec
er.evaluate_entry = hook_eval

eng = BacktestEngine(settings=s, dataset=ds)
eng.trade_start_ts = pd.Timestamp('2025-11-01T00:00:00Z')

# Run manually
timeline = eng._collect_timeline()
print('Timeline length:', len(timeline))
count = 0
for ts in timeline:
    if ts < pd.Timestamp('2025-11-01T00:00:00Z'): continue
    count += 1
    if count > 800: break
    for symbol in ["ETHUSDT"]:
        ctx = eng._context_rows_fast(symbol, ts)
        states = eng._states_from_ctx(ctx)
        
        from signals.resonance import direction_gate
        dg = direction_gate(states, s.strategy.weights, s.strategy.r_gate_long, s.strategy.r_gate_short)
        if dg.allow_long or dg.allow_short:
             side = "long" if dg.allow_long else "short"
             from backtest.engine import ALL_TFS
             frames = {k: ds[symbol].get(k) for k in ALL_TFS}
             er.evaluate_entry(
                 symbol=symbol, side=side, gate=dg, trend_states=states,
                 frames=frames, btc_frames=ds.get('BTCUSDT'), decision_close_ts=ts,
                 breakout_filter=0.0
             )

from collections import Counter
print("Blocks:", Counter(reasons).most_common())
