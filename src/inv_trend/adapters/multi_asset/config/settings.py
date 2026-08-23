"""Small, dependency-light configuration layer for backtest entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from inv_trend.config import (
    canonical_to_backtest_mapping,
    is_canonical_strategy_mapping,
    load_strategy_mapping,
)


@dataclass(frozen=True)
class BacktestConfig:
    """Runtime settings that are shared by CLI, research scripts, and tests."""

    initial_equity: float = 100_000.0
    cash_model: str = "derivative"
    liquidate_at_end: bool = True
    strategy_version: str = "corrected-v2"
    html_report: bool = True
    log_level: str = "INFO"
    log_format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    log_datefmt: str = "%Y-%m-%d %H:%M:%S"
    code_version: str = "unknown"
    data_manifest_hash: str = ""
    rules: Mapping[str, Any] = field(default_factory=dict)
    paths: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if self.cash_model not in {"derivative", "cash"}:
            raise ValueError("cash_model must be 'derivative' or 'cash'")
        if self.strategy_version != "corrected-v2":
            raise ValueError("only the executable strategy version 'corrected-v2' is supported")
        if not isinstance(self.rules, Mapping):
            raise ValueError("rules must be a mapping")
        if not isinstance(self.paths, Mapping):
            raise ValueError("paths must be a mapping")
        object.__setattr__(self, "rules", MappingProxyType(dict(self.rules)))
        object.__setattr__(self, "paths", MappingProxyType(dict(self.paths)))


def load_config(path: str | Path | None = None) -> BacktestConfig:
    """Load canonical strategy YAML or an explicitly supplied legacy file."""

    explicit_path = path is not None
    if path is None:
        return BacktestConfig(**canonical_to_backtest_mapping(load_strategy_mapping()))
    path = Path(path)
    if not path.exists():
        if explicit_path:
            raise FileNotFoundError(f"missing config file: {path}")
        return BacktestConfig()

    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            f"PyYAML is required to load backtest configuration: {path}"
        ) from exc

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config file must contain a mapping: {path}")
    if is_canonical_strategy_mapping(raw):
        raw = canonical_to_backtest_mapping(raw)
    allowed = set(BacktestConfig.__dataclass_fields__)
    unknown = set(raw) - allowed
    if unknown:
        names = ", ".join(sorted(unknown))
        raise ValueError(f"unsupported backtest configuration keys: {names}")
    return BacktestConfig(**raw)
