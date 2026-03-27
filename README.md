# Crypto Quant MVP

Minimal but runnable crypto quant trading system for Binance USDT-M Futures with:

- Trend following on EMA144 / EMA169
- Multi-timeframe resonance score
- Tiered risk and leverage
- Position state machine
- Backtest + paper/live entrypoints

## Quick Start

```bash
pip install -r requirements.txt
python -m main.run_backtest --use-synthetic
```

For exchange-backed backtest:

```bash
python -m main.run_backtest --symbols BTCUSDT ETHUSDT --limit 1500
```

Backtest with OKX:

```bash
python -m main.run_backtest --exchange-id okx --symbols BTCUSDT ETHUSDT --limit 1500
```

Historical range backtest:

```bash
python -m main.run_backtest_historical --exchange-id okx --symbols BTCUSDT ETHUSDT --start 2024-01-01T00:00:00Z --end 2025-01-01T00:00:00Z
```

Backtest with BTC + ETH + XAU (Gate.io swap):

```bash
python -m main.run_backtest_historical --config config/settings_gateio_xau.yaml --start 2026-02-01T00:00:00Z --end 2026-03-26T00:00:00Z --warmup-days 20 --limit-per-call 1000 --max-pages 80
```

Note: Gate.io 15m API allows only recent ~10,000 bars. For 15m data, keep `start/end + warmup` within roughly 100 days from now.
