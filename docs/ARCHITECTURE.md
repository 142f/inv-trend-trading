# Runtime architecture

```text
historical_data ─┐
inv_trend_core ──┼──> inv_trend_application ───> CLI / research
integrations ────┘                 │
                                   └──> inv_trend_observability
```

`historical_data` owns instrument identity, provider adaptation, calendar and
quality semantics. `historical_data/config/assets.yaml` is the default source
of truth for these fields. The application layer adapts it into detector scan
settings only at the use-case boundary; a legacy detector YAML can be supplied
explicitly to the preserved CLI parameter, but is never the default source.

`inv_trend_core` is in-memory and I/O-free. It owns `PreparedBars`, the
run-scoped `FeatureCache`, causal ATR/Donchian/SMA/EMA/MACD, event identity and
generic equity statistics. Features are keyed by an OHLCV content fingerprint
and the full `FeatureRequest`, so they cannot be shared across datasets.

`inv_trend_application` composes data, strategy, state stores and manifests.
Daily scanning and research backtest entry points return result objects; they do
not format HTML or terminal output. `inv_trend_observability` owns report
rendering and manifests. `inv_trend_integrations` contains optional MT5/OKX
ports and is not imported by a CLI or core strategy path.

The established `turtle_detector` and `turtle_multi_asset` packages remain
strategy adapters while the commands keep their public names and options. They
delegate shared features, metrics, config resolution and use-case orchestration
to the new layers rather than maintaining duplicate indicator code.
