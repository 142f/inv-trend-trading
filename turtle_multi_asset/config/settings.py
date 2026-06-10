"""Small, dependency-light configuration layer for backtest entry points."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class BacktestConfig:
    """Runtime settings that are shared by CLI, research scripts, and tests."""

    initial_equity: float = 100_000.0
    cash_model: str = "derivative"
    liquidate_at_end: bool = True
    log_level: str = "INFO"
    log_format: str = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    log_datefmt: str = "%Y-%m-%d %H:%M:%S"
    rules: Mapping[str, Any] = field(default_factory=dict)
    paths: Mapping[str, str] = field(default_factory=dict)


def load_config(path: str | Path | None = None) -> BacktestConfig:
    """Load config from YAML when available, otherwise return safe defaults.

    PyYAML is intentionally optional so importing the package does not require
    extra dependencies in test or minimal runtime environments.
    """

    if path is None:
        path = Path(__file__).with_name("defaults.yaml")
    path = Path(path)
    if not path.exists():
        return BacktestConfig()

    try:
        import yaml  # type: ignore
    except ImportError:
        return BacktestConfig()

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config file must contain a mapping: {path}")
    allowed = set(BacktestConfig.__dataclass_fields__)
    values = {key: value for key, value in raw.items() if key in allowed}
    return BacktestConfig(**values)
