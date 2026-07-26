# Quant Trading System Code Audit

Audit date: 2026-07-26  
Revision: `e750c9e` (`main`)  
Scope: tracked package and tests, plus the local ignored research and processed-data
trees required by those tests.

## Executive conclusion

The repository has a coherent research backtester and good unit-level coverage of
its central event loop, but it is not yet a reproducible research system or a safe
foundation for live execution.

The immediate work is not a wholesale rewrite and not machine-learning expansion.
It is to make the current results closed, deterministic, and semantically explicit:

1. make a clean checkout able to install and test itself;
2. version experiment definitions and dataset manifests/hashes;
3. resolve the failing D1 regression;
4. fix System 1 skip-state semantics and window warm-up;
5. separate signal, risk, execution, and accounting state.

## Implementation update

The first remediation pass was applied after this audit:

- research Python sources are no longer hidden by the generated-output ignore rule;
- `pyproject.toml` now declares the core, integration, and test dependency groups;
- explicit YAML paths and unknown configuration keys fail closed;
- `AssetSpec` and cluster-limit values validate trading invariants;
- `evaluation_start` preserves pre-window indicator history without trading before
  the evaluation boundary;
- D1 runs reject missing requested symbols unless partial-universe behavior is
  explicitly enabled;
- skipped System 1 breakouts advance as virtual trades and can restore fast-system
  eligibility after a virtual loss;
- general data-build reports include pre-clean quality and rejected-row counts;
- the external D1 regression is input-fingerprint-gated and has a new semantic
  baseline;
- focused tests cover these behaviors.

Post-change verification: 42 tests passed, 1 environment-dependent MT5 sample test
was skipped, Ruff passed, and `git diff --check` passed.

The attached draft's claim that `config/defaults.yaml` is missing is false for this
revision. The file exists and is used by the CLI's metal-tech build path. The draft
also over-prioritized the Wilder ATR Python loop: that is a performance improvement,
not a P0 correctness defect.

## Verification performed

- `python -m compileall`: passed.
- `pytest -q`: 32 passed, 1 skipped, 1 failed.
- Failing regression:
  - actual final equity: `291,888.188621`
  - expected final equity: `301,571.449408`
  - difference: about `-3.21%`
- Ruff: package code passed the default checks; ignored research scripts have 10
  findings (3 unused imports and 7 import-order findings).
- Line coverage from the current local test run: 63% overall. Important core
  modules are substantially higher (`runner.py` 89%, `engine.py` 83%,
  `indicators.py` 91%), while integrations and data-build orchestration are mostly
  15-43%. Coverage is provisional because the suite is red and branch coverage was
  not measured.

## Current execution architecture

The reusable path is:

```text
CSV / MT5 / OKX adapters
        |
        v
normalization -> cleaning -> optional resampling -> exported CSV
        |
        v
BacktestDataStore
  - validates OHLC
  - computes N and shifted channels
  - builds union calendar and record caches
        |
        v
TurtleBacktester daily/bar loop
  1. execute pending orders at this bar's open
  2. process intrabar stops
  3. apply funding/borrow carry
  4. liquidate symbols on their final bar when requested
  5. mark equity at close
  6. generate exits, adds, and entries for a later bar
        |
        v
MultiAssetTurtleStrategy
  - breakout/exit decisions
  - N-based sizing
  - risk and leverage budget allocation
        |
        v
equity curve + order/trade tables + summary metrics
```

There are three operational entry families:

- `turtle_multi_asset.cli.data_cli`: data build, metal-tech build/backtest, US trend
  alerts.
- `turtle_multi_asset.us_trend_alerts`: standalone alert scanner.
- ignored `research/...` scripts: the actual D1 presets and experiment suites.

Configuration is not unified. `BacktestConfig` can configure the reusable
backtester, but D1 research constructs `TurtleRules` and `AssetSpec` directly.
`run_core_backtest_and_compare` also supplies explicit rules, so YAML `rules` are
ignored on that path even though other `BacktestConfig` fields apply.

## Findings

### P0: release and research blockers

#### 1. A clean checkout is not self-contained

Tracked tests import:

- `research.d1_suite.scripts.d1_backtest_common`
- `research.d1_suite.scripts.run_d1_pruned_universe_experiments`
- `research.d1_suite.scripts.run_d1_candidate9_audit`

They also consume local files under `processed_data/cleaned`.

Both `research/*` and `processed_data/*` are ignored, and the required scripts/data
are not present in `HEAD`. The tests pass collection only because this workspace
contains ignored local files. CI or a new developer clone cannot reproduce the
current suite.

Fix:

- track research source code while continuing to ignore generated research outputs;
- replace large local regression data with a small tracked deterministic fixture,
  or add a checksummed fetch/build command and mark the external-data test
  explicitly;
- add CI that builds a fresh environment from declared dependencies.

#### 2. The locked D1 result currently fails

`test_unified_runner_matches_revised8_2020_window` differs by roughly 3.21% in final
equity. Because required data are ignored and manifests contain no content hashes,
the repository cannot determine whether this is engine drift, data drift, or a
stale expected value.

Fix:

- freeze the input manifest (URI/source, retrieval timestamp, row count, date range,
  SHA-256, schema version, adjustment policy);
- store the fully resolved strategy/spec config with the result;
- compare the first divergent order/equity timestamp, not only terminal metrics;
- update the expected values only after the divergence is explained.

#### 3. D1 windows discard indicator warm-up history

`run_backtest` calls `trim_data(start=...)` before the backtester computes N and
breakout channels. A test beginning on 2020-01-01 therefore has no valid 55-bar
channel at the requested start; it begins trading only after the in-window warm-up.
This changes every rolling-window experiment and makes segment comparisons
dependent on the chosen cut.

Fix:

- distinguish `data_start` from `evaluation_start`;
- load at least `max(n_period, entry periods, exit periods, MA period)` prior bars;
- allow feature/state warm-up before suppressing fills and metric collection until
  `evaluation_start`.

#### 4. Requested universes silently lose assets

The D1 helper reduces a requested symbol list to symbols available in the local
dictionary and continues as long as two remain. A misspelled, missing, or stale
asset therefore changes the experiment without failing.

Fix:

- fail closed by default with an exact missing-symbol list;
- allow an explicit `allow_partial_universe` research option;
- persist requested, resolved, missing, and excluded universes in every run
  manifest.

#### 5. Fast-system skip state does not implement the Turtle rule

`last_fast_trade_won` is changed only when an executed fast position exits.
`_skip_fast` then suppresses subsequent fast entries, but the engine does not track
the hypothetical result of a skipped breakout. It therefore cannot reset System 1
eligibility when the skipped breakout would have lost. A slow-system fallback can
still trade, but it does not repair this state.

Fix:

- model System 1 breakout state explicitly (`eligible`, `virtual_open`,
  `virtual_won/lost`);
- advance virtual trades with the same causal exit rules;
- test winner -> skipped loser -> next fast breakout accepted, as well as the
  winner -> 55-day fallback case.

### P1: material correctness and control risks

#### 6. `trigger_mode="intraday"` is not an intraday fill model

The strategy inspects the completed bar's high/low, stores an extreme/trigger as
the signal price, and the backtester still queues the order for a later bar's open.
The value is also used for scoring and leverage admission, although the actual fill
can be materially different.

Choose and name one contract:

- close-confirmed / next-open execution; or
- true intrabar trigger execution with explicit gap and same-bar ambiguity rules.

Do not expose both under one event loop without a broker/fill policy abstraction.

#### 7. Risk approval is stale across an overnight gap

Sizing and leverage checks use signal-bar equity, N, and price. The accepted order
is not revalidated at its next-open fill. A gap can breach symbol, cluster,
direction, or total leverage caps. Minimum notional is also checked against the
signal price rather than the fill.

Fix:

- create order intents at signal time;
- reserve risk for pending intents;
- re-price and revalidate quantity at fill time;
- record reject/resize reasons.

#### 8. Data-quality reporting can erase the evidence

The general builder normalizes, cleans, and then validates. Since cleaning drops
duplicates, null prices, non-positive prices, and invalid OHLC rows, the exported
validation report describes the cleaned output rather than the rejected input.
The metal-tech path does capture some pre-clean counts, but the two pipelines are
inconsistent.

Fix:

- report raw, normalized, rejected, and accepted counts separately;
- export a reject ledger with row identifiers and reasons;
- never silently select the last duplicate without a declared source-precedence
  rule.

#### 9. Configuration can be silently ignored

- If PyYAML is unavailable, `load_config` silently returns defaults even for a
  caller-supplied config file.
- Unknown top-level YAML keys are silently dropped.
- Explicit `rules` passed by the core dataset path override YAML rules.
- D1 presets bypass `BacktestConfig`.

Fix:

- make YAML a declared dependency or reject YAML input when its parser is absent;
- validate the entire schema and reject unknown keys;
- resolve one immutable `RunConfig` and write it to every artifact directory.

#### 10. Accounting is not a complete ledger

Funding and borrow charges change cash/equity but are absent from orders, trades,
and contribution tables. Consequently, trade P&L cannot reconcile to the equity
curve. There are no explicit cash-flow events, margin events, rejected orders, or
reserved cash/risk.

Fix:

- use an append-only ledger of fills, fees, financing, dividends/splits, deposits,
  and adjustments;
- derive cash, positions, equity, and attribution from ledger events;
- add a strict reconciliation invariant for every timestamp and final result.

#### 11. End-of-data liquidation uses knowledge of the future

Each symbol is liquidated when the loop knows it has reached that symbol's final
local bar. For a deliberately bounded test this is acceptable; for an accidental
source outage or unequal asset history it introduces an unrealistic exit.

Fix:

- pass an explicit evaluation end and liquidation policy;
- distinguish planned test-end liquidation from asset delisting/data outage;
- fail or quarantine unexpected stale assets.

#### 12. Domain models accept invalid trading parameters

`AssetSpec` has no validation for negative costs, invalid quantity steps, negative
minimums, inconsistent symbol limits, or `max_units < 1`. `TurtleRules` validates
some scalars but not cluster-map values or relationships among caps.

Fix:

- validate all domain invariants at construction;
- use decimal/integer lot units where exchange precision matters;
- reject non-finite values throughout configuration.

#### 13. Market-data semantics are underspecified

Naive timestamps are interpreted as UTC; D1 resampling uses UTC midnight for every
asset; equity adjusted/unadjusted price policy is not recorded; exchange calendars,
corporate actions, delistings, and survivorship policy are absent.

These omissions can dominate a strategy result even when the event loop is
perfectly causal.

### P2: maintainability, evaluation, and performance

#### 14. Package and dependency metadata are incomplete

There is no `pyproject.toml` or complete requirements file. `requirements-okx.txt`
contains only the OKX slice. Python and library versions are not locked, and there
is no CI definition.

#### 15. Responsibilities are concentrated

- `backtest/runner.py`: 612 lines, combining orchestration, fills, accounting,
  stops, financing, and reporting rows.
- `strategy/engine.py`: 519 lines, combining signal rules, sizing, portfolio risk,
  and candidate ranking.
- `data/core_dataset.py`: 452 lines, combining selection policy, ETL, backtest,
  comparison, and reporting.

This makes semantic changes hard to isolate and test.

#### 16. Rules and asset profiles are duplicated

Asset parameters exist in `profiles/asset_profiles.py`, `integrations/mt5.py`,
`data/core_dataset.py`, and ignored D1 research helpers. Turtle rule presets are
also split between package profiles and research.

One profile registry should resolve a fully explicit spec; research should only
provide overrides.

#### 17. Metrics are too weak for research decisions

The current set is total return, CAGR, drawdown, volatility, a zero-rate
Sharpe-like value, MAR, trade count, and inferred periods/year. Missing items
include downside metrics, drawdown duration, exposure, turnover, capacity,
profit-factor distribution, tail loss, benchmark-relative results, and
cross-validation stability. Negative or zero equity also needs explicit metric
behavior instead of relying on the CAGR formula.

#### 18. Performance work should follow correctness work

Real hotspots include:

- Python Wilder recursion;
- full DataFrame-to-dict copies;
- `O(calendar dates * symbols * searchsorted)` calendar-map construction;
- duplicate bar representations in DataFrames, records, and timestamp maps.

These are P2. Optimize with benchmarks after event semantics are locked. Avoid
adding Numba solely for ATR until a dependency-free implementation has been
benchmarked against the actual universe.

#### 19. Dead and low-use surfaces add noise

`cached_dataframe` and `setup_logging` are exported but unused internally.
`EntrySignal.event_frozen` is never meaningfully propagated. OKX has no tests, and
MT5 tests cover only inference/time conversion rather than adapter failures and
schema behavior.

## Target architecture

Keep the existing package name and migrate by ports, not by a big-bang directory
rename:

```text
data sources -> canonical dataset + manifest -> feature pipeline
                                                |
                                                v
portfolio snapshot -> strategy -> order intents -> risk policy
                                                |
                                                v
                                      broker/fill simulator
                                                |
                                                v
                                      fills -> accounting ledger
                                                |
                                                v
                                   portfolio/equity/attribution
```

### Boundaries

| Component | Owns | Must not own |
|---|---|---|
| `domain` | Bar, Signal, Intent, Order, Fill, Position, cash-flow events | pandas I/O, network clients |
| `data` | adapters, canonical schema, calendars, adjustments, manifests | strategy selection |
| `features` | causal, versioned feature transforms | fills or portfolio mutation |
| `strategy` | pure signal/intent generation from history + snapshot | cash mutation, exchange API |
| `risk` | portfolio limits, reservation, resize/reject decisions | breakout logic |
| `execution` | fill timing, gaps, spread, slippage, commissions | signal ranking |
| `accounting` | append-only ledger and reconciliation | market selection |
| `backtest` | event clock and component orchestration | embedded strategy rules |
| `experiment` | resolved config, dataset hash, code revision, artifacts | reusable trading logic |
| `live` | broker adapters and durable order state using the same ports | separate strategy implementation |

The first interfaces should be small:

- `DataPortal.snapshot(clock) -> MarketSnapshot`
- `Strategy.on_bar(snapshot, portfolio) -> list[OrderIntent]`
- `RiskPolicy.evaluate(intents, portfolio, market) -> RiskDecision`
- `Broker.on_bar(clock, accepted_intents) -> list[FillEvent]`
- `Ledger.apply(events) -> PortfolioSnapshot`

## Migration plan

### Phase 0: restore trust (1-3 days)

1. Track research source; stop tracking/importing ignored-only code.
2. Add `pyproject.toml`, locked/test dependency groups, and fresh-clone CI.
3. Create a tiny tracked regression dataset and an external-data test marker.
4. Resolve the current D1 divergence using first-difference order diagnostics.
5. Add input SHA-256 and resolved-config manifests.

Exit criterion: a clean clone produces the same green tests and deterministic
artifacts.

### Phase 1: lock semantics (3-6 days)

1. Add warm-up/evaluation boundaries.
2. Fail on missing universe members.
3. Implement virtual System 1 skip state.
4. Choose explicit close/next-open and intrabar execution policies.
5. Revalidate risk at fills and reserve pending risk.
6. Add invalid-config/domain tests.

Exit criterion: documented timing and accounting invariants are covered by focused
tests, including gap and ambiguous-bar cases.

### Phase 2: extract execution and accounting (5-10 days)

1. Introduce `OrderIntent`, `RiskDecision`, and `FillEvent`.
2. Move costs/slippage/stops into a broker simulator.
3. Move financing and P&L into an append-only ledger.
4. Reconcile equity to ledger and trade attribution.
5. Preserve legacy results behind a compatibility policy during migration.

Exit criterion: strategies no longer mutate or depend on fill/accounting details.

### Phase 3: research discipline (3-7 days)

1. One profile/config registry.
2. Walk-forward train/validation/test orchestration.
3. Benchmark and data-adjustment metadata.
4. Exposure, turnover, tail, drawdown-duration, and stability reports.
5. Parameter-neighborhood and cost/slippage stress tests.

Exit criterion: model selection uses out-of-sample criteria and every report can be
reconstructed from its manifest.

### Phase 4: performance and live-readiness

1. Benchmark feature preparation, calendar access, and event-loop memory.
2. Optimize only proven hotspots.
3. Add durable order IDs, idempotency, restart recovery, reconciliation, kill
   switches, stale-data detection, and paper trading.
4. Keep live broker adapters behind the same execution interface.

Machine learning or regime models should come only after Phase 3. Adding model
complexity before reproducibility, accounting, and validation are trustworthy
would make the system harder to audit without improving the evidence for an edge.

## Acceptance tests for the redesigned core

- No-lookahead: changing bars after time `t` cannot alter signals through `t`.
- Warm-up invariance: adding earlier warm-up bars cannot shift the evaluation
  window except through correctly initialized state.
- Gap risk: accepted intent is resized/rejected if the fill-bar gap breaches caps.
- Ambiguous bar: stop/add/exit priority is deterministic and policy-labeled.
- Fast skip: winner -> virtual skipped loser -> next fast entry is eligible.
- Missing universe: default run fails with an exact symbol list.
- Accounting: initial equity + external cash flows + ledger P&L equals final equity.
- Financing: carry appears in both equity and attribution.
- Reproducibility: dataset hash + config hash + code revision reproduce every
  order and metric.
- Clean clone: install, lint, unit tests, and deterministic regression all pass
  without ignored local files.
