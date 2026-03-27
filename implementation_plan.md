# implementation_plan.md

## Goal

Build a minimal, runnable, and extensible crypto quant system for Binance USDT-M Futures with:

- BTCUSDT / ETHUSDT
- trend-following + multi-timeframe resonance
- tiered position sizing and leverage
- strict stop-loss / take-profit / forced-exit controls
- backtest + paper + live entrypoints

## Core Constraints

- Decision cycle: only at each closed 15m bar.
- No unclosed candle usage on any timeframe.
- Base indicators: EMA144 / EMA169 (all TF), ATR14 (15m only).
- Raw TF: 15m, 30m, 1h, 2h, 4h, 1d.
- Aggregated TF (from closed 1d only): 2d, 5d, 7d.
- Epoch-aligned UTC day boundaries for aggregation.
- One-direction-only position per symbol, no same-symbol hedge.

## Modules

- `config/`: centralized parameters and loaders
- `data/`: exchange access, kline fetch, aggregation, validation
- `indicators/`: EMA/ATR and trend-state classification
- `signals/`: resonance score, direction gate, pullback/reclaim, entry/exit rules
- `risk/`: sizing, leverage caps, portfolio limits, pre-trade checks
- `portfolio/`: position state machine, accounting, lifecycle manager
- `execution/`: simulated fills and live router abstraction
- `backtest/`: engine, costs, funding, report
- `main/`: run_backtest / run_paper / run_live entrypoints
- `tests/`: unit tests for key strategy and risk invariants

## Execution Order per 15m Close

1. Refresh closed bars
2. Aggregate 2d/5d/7d from closed 1d
3. Compute indicators and trend states
4. Compute resonance score R and gate
5. Manage existing positions first
6. Evaluate add-on / new entries
7. Record logs and snapshots

## Done Criteria

- Runnable backtest pipeline (`python -m main.run_backtest --use-synthetic`)
- Modular code with centralized config
- Position state machine and risk controls implemented
- Fee/slippage/funding integrated in backtest
- Basic validation tests passing (`python -m pytest -q`)
