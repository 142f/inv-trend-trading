"""Uniform execution contract for the six daily workflow stages."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Generic, Protocol, TypeVar, runtime_checkable


T = TypeVar("T")
STAGE_ORDER = (
    "data-update",
    "strategy-screen",
    "trend-decide",
    "commit",
    "publish",
    "deliver",
)


@runtime_checkable
class DailyStage(Protocol[T]):
    """A bound stage invocation with no hidden argument construction."""

    stage_name: str

    def execute(self) -> T: ...

    def can_resume(self) -> bool: ...


@dataclass(frozen=True)
class BoundDailyStage(Generic[T]):
    """Bind an existing service method to the common stage protocol."""

    stage_name: str
    executor: Callable[[], T]
    resume_check: Callable[[], bool]

    def __post_init__(self) -> None:
        if self.stage_name not in STAGE_ORDER:
            raise ValueError(f"unsupported daily stage: {self.stage_name}")

    def execute(self) -> T:
        return self.executor()

    def can_resume(self) -> bool:
        return bool(self.resume_check())


@dataclass(frozen=True)
class StageExecutionRecord:
    stage_name: str
    status: str
    recovered: bool
    started_at: str
    finished_at: str
    error_type: str | None = None


class StageExecutionTracker:
    """Operational in-memory audit trail excluded from business hashes."""

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._records: list[StageExecutionRecord] = []

    @property
    def records(self) -> tuple[StageExecutionRecord, ...]:
        return tuple(self._records)

    def run(self, stage: DailyStage[T]) -> T:
        started = self._timestamp()
        recovered = stage.can_resume()
        try:
            result = stage.execute()
        except BaseException as exc:
            self._records.append(
                StageExecutionRecord(
                    stage.stage_name,
                    "FAILED",
                    recovered,
                    started,
                    self._timestamp(),
                    type(exc).__name__,
                )
            )
            raise
        self._records.append(
            StageExecutionRecord(
                stage.stage_name,
                "COMPLETED",
                recovered,
                started,
                self._timestamp(),
            )
        )
        return result

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()


__all__ = [
    "BoundDailyStage",
    "DailyStage",
    "STAGE_ORDER",
    "StageExecutionRecord",
    "StageExecutionTracker",
]
