"""Versioned contracts for the unified independent backtest module."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
import hashlib
import json
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "2"
LEGACY_SCHEMA_VERSION = "1"


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        _plain(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _plain(value: Any) -> Any:
    if is_dataclass(value):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class ValidationPolicy:
    # D1 defaults: roughly four years of expanding training, one-year
    # validation steps, and an untouched one-year final holdout.
    train_bars: int = 1_460
    validation_bars: int = 365
    step_bars: int = 365
    holdout_bars: int = 365
    min_folds: int = 3

    def __post_init__(self) -> None:
        for name in (
            "train_bars", "validation_bars", "step_bars", "holdout_bars", "min_folds"
        ):
            if int(getattr(self, name)) < 1:
                raise ValueError(f"validation.{name} must be positive")


@dataclass(frozen=True)
class RankingPolicy:
    min_oos_trades: int = 10
    max_drawdown: float = 0.40
    sharpe_weight: float = 0.35
    mar_weight: float = 0.25
    cagr_weight: float = 0.20
    stability_weight: float = 0.20

    def __post_init__(self) -> None:
        if self.min_oos_trades < 0:
            raise ValueError("ranking.min_oos_trades must be non-negative")
        if not 0 < self.max_drawdown <= 1:
            raise ValueError("ranking.max_drawdown must be within (0, 1]")
        weights = (
            self.sharpe_weight,
            self.mar_weight,
            self.cagr_weight,
            self.stability_weight,
        )
        if any(value < 0 for value in weights) or abs(sum(weights) - 1.0) > 1e-9:
            raise ValueError("ranking weights must be non-negative and sum to 1")


@dataclass(frozen=True)
class BacktestPlan:
    """Normalized parameter-search plan."""

    symbols: tuple[str, ...]
    strategy_id: str = "turtle"
    parameter_space: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    constraints: tuple[str, ...] = ()
    initial_equity: float = 100_000.0
    cash_model: str = "derivative"
    liquidate_at_end: bool = True
    max_combinations: int = 1_000
    validation: ValidationPolicy = field(default_factory=ValidationPolicy)
    ranking: RankingPolicy = field(default_factory=RankingPolicy)
    strategy_config_path: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        normalized_symbols = tuple(dict.fromkeys(str(item).upper() for item in self.symbols))
        if not normalized_symbols:
            raise ValueError("symbols must not be empty")
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if self.cash_model not in {"cash", "derivative"}:
            raise ValueError("cash_model must be 'cash' or 'derivative'")
        if self.max_combinations < 1:
            raise ValueError("max_combinations must be positive")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"unsupported backtest plan schema: {self.schema_version}")
        if not self.strategy_id.strip():
            raise ValueError("strategy_id must not be empty")
        object.__setattr__(self, "symbols", normalized_symbols)
        object.__setattr__(
            self,
            "parameter_space",
            {str(key): tuple(values) for key, values in self.parameter_space.items()},
        )

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "BacktestPlan":
        allowed = {
            "schema_version", "symbols", "parameter_space", "constraints",
            "initial_equity", "cash_model", "liquidate_at_end", "max_combinations",
            "validation", "ranking", "strategy_config_path", "strategy_id",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unsupported backtest plan keys: {sorted(unknown)}")
        parameter_space = raw.get("parameter_space", {})
        if not isinstance(parameter_space, Mapping):
            raise ValueError("parameter_space must be a mapping")
        for name, values in parameter_space.items():
            if not isinstance(values, Sequence) or isinstance(values, (str, bytes)) or not values:
                raise ValueError(f"parameter_space.{name} must be a non-empty list")
        validation_raw = raw.get("validation", {})
        ranking_raw = raw.get("ranking", {})
        if not isinstance(validation_raw, Mapping) or not isinstance(ranking_raw, Mapping):
            raise ValueError("validation and ranking must be mappings")
        raw_schema = str(raw.get("schema_version", SCHEMA_VERSION))
        if raw_schema not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION}:
            raise ValueError(f"unsupported backtest plan schema: {raw_schema}")
        return cls(
            symbols=tuple(raw.get("symbols", ())),
            strategy_id=str(raw.get("strategy_id", "turtle")),
            parameter_space={str(key): tuple(values) for key, values in parameter_space.items()},
            constraints=tuple(str(item) for item in raw.get("constraints", ())),
            initial_equity=float(raw.get("initial_equity", 100_000.0)),
            cash_model=str(raw.get("cash_model", "derivative")),
            liquidate_at_end=bool(raw.get("liquidate_at_end", True)),
            max_combinations=int(raw.get("max_combinations", 1_000)),
            validation=ValidationPolicy(**dict(validation_raw)),
            ranking=RankingPolicy(**dict(ranking_raw)),
            strategy_config_path=(
                None if raw.get("strategy_config_path") is None
                else str(raw["strategy_config_path"])
            ),
            schema_version=SCHEMA_VERSION,
        )

    def to_dict(self) -> dict[str, Any]:
        return _plain(self)


@dataclass(frozen=True)
class SourceInstrument:
    symbol: str
    instrument_id: str
    timeframe: str
    dataset_version: str
    data_result_hash: str
    screening_result_hash: str
    decision_result_hash: str
    formal_signals: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class BacktestSourceBundle:
    source_run_id: str
    source_run_path: str
    strategy_version: str
    instruments: tuple[SourceInstrument, ...]
    strategy_config: Mapping[str, Any]
    strategy_id: str = "turtle"
    code_version: str = "unknown"
    schema_version: str = SCHEMA_VERSION

    @property
    def source_hash(self) -> str:
        payload = _plain(self)
        payload.pop("source_run_path", None)
        return canonical_hash(payload)

    def to_dict(self) -> dict[str, Any]:
        payload = _plain(self)
        payload["source_hash"] = self.source_hash
        return payload


@dataclass(frozen=True)
class BacktestSignalBundle:
    signal_parameter_hash: str
    parameters: Mapping[str, Any]
    source_hash: str
    event_count: int
    events: tuple[Mapping[str, Any], ...]
    prepared_data_hashes: Mapping[str, str]
    schema_version: str = SCHEMA_VERSION

    @property
    def bundle_hash(self) -> str:
        return canonical_hash(_plain(self))

    def to_dict(self) -> dict[str, Any]:
        payload = _plain(self)
        payload["bundle_hash"] = self.bundle_hash
        return payload


@dataclass(frozen=True)
class FoldWindow:
    fold: int
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str


@dataclass(frozen=True)
class FoldResult:
    window: FoldWindow
    train_metrics: Mapping[str, Any]
    validation_metrics: Mapping[str, Any]


@dataclass(frozen=True)
class BacktestCombinationResult:
    combination_id: str
    parameters: Mapping[str, Any]
    signal_parameter_hash: str
    status: str
    metrics: Mapping[str, Any]
    validation_metrics: Mapping[str, Any]
    holdout_metrics: Mapping[str, Any]
    folds: tuple[FoldResult, ...]
    long_metrics: Mapping[str, Any]
    short_metrics: Mapping[str, Any]
    breakdowns: Mapping[str, Any]
    equity_curve: tuple[Mapping[str, Any], ...]
    drawdown_curve: tuple[Mapping[str, Any], ...]
    trades: tuple[Mapping[str, Any], ...]
    orders: tuple[Mapping[str, Any], ...]
    unavailable: Mapping[str, str] = field(default_factory=dict)
    error: str | None = None
    score: float | None = None
    rank: int | None = None
    qualified: bool = False
    stability_score: float | None = None
    overfit_risk: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    @property
    def result_hash(self) -> str:
        return canonical_hash(_plain(self))

    @property
    def execution_result_hash(self) -> str:
        """Hash only execution facts so refactors can prove result parity."""

        return canonical_hash({
            "orders": _normalized_orders(self.orders),
            "trades": self.trades,
            "equity_curve": self.equity_curve,
        })

    def to_dict(self) -> dict[str, Any]:
        payload = _plain(self)
        payload["result_hash"] = self.result_hash
        payload["execution_result_hash"] = self.execution_result_hash
        return payload


@dataclass(frozen=True)
class BacktestBatchResult:
    run_id: str
    report_date: str
    source: BacktestSourceBundle
    plan: BacktestPlan
    signal_bundles: tuple[BacktestSignalBundle, ...]
    combinations: tuple[BacktestCombinationResult, ...]
    validation_status: str
    best_combination_id: str | None
    stable_combination_ids: tuple[str, ...]
    comparison_assumptions_hash: str
    generated_at: str
    conclusion: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION

    @property
    def result_hash(self) -> str:
        payload = _plain(self)
        for operational in ("run_id", "report_date", "generated_at"):
            payload.pop(operational, None)
        source = payload.get("source")
        if isinstance(source, dict):
            source.pop("source_run_path", None)
        return canonical_hash(payload)

    def to_dict(self) -> dict[str, Any]:
        payload = _plain(self)
        payload["source"] = self.source.to_dict()
        payload["signal_bundles"] = [item.to_dict() for item in self.signal_bundles]
        payload["combinations"] = [item.to_dict() for item in self.combinations]
        payload["result_hash"] = self.result_hash
        return payload


def _normalized_orders(
    orders: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Exclude operational IDs while preserving every business order field."""

    return [
        {key: value for key, value in order.items() if key != "intent_id"}
        for order in orders
    ]


__all__ = [
    "BacktestBatchResult", "BacktestCombinationResult", "BacktestPlan",
    "BacktestSignalBundle", "BacktestSourceBundle", "FoldResult", "FoldWindow",
    "LEGACY_SCHEMA_VERSION", "RankingPolicy", "SCHEMA_VERSION", "SourceInstrument", "ValidationPolicy",
    "canonical_hash",
]
