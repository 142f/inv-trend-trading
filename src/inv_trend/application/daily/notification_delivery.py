"""Outbox delivery component for the modular D1 workflow.

The service deliberately operates only on events already committed to the
daily outbox.  It preserves the existing retry and recovery semantics while
making them reusable outside the monolithic daily workflow.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .ports import DailyStateRepositoryPort, NotificationPort

if TYPE_CHECKING:
    # These historical names remain available through ``__getattr__`` below
    # for callers that imported them from this compatibility module.  Keeping
    # the concrete notifier import out of the application stage makes the
    # delivery service depend only on its notification port.
    from inv_trend.adapters.detector.alerts.daily_notifier import LogNotifier, SignalNotifier


class NotificationDeliveryService:
    """Deliver a run's outbox entries and retry earlier failed entries."""

    def __init__(
        self,
        repository: DailyStateRepositoryPort,
        notifier: NotificationPort,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.notifier = notifier
        self._now = now or (lambda: datetime.now(timezone.utc))

    def deliver(
        self,
        run_id: str | None = None,
        *,
        include_recovery: bool = True,
    ) -> dict[str, int]:
        """Deliver pending events without creating signals or changing cursors."""

        result = {"notified": 0, "errors": 0, "recovered": 0}
        current = self.repository.pending_notifications(run_id)
        for event in current:
            try:
                self.notifier.notify(event)
                self.repository.mark_notified(event.signal_id, self._timestamp())
                result["notified"] += 1
            except Exception as exc:  # Delivery errors remain in the outbox for retry.
                self.repository.mark_delivery_error(event.signal_id, str(exc))
                result["errors"] += 1

        if not include_recovery:
            return result
        current_ids = {event.signal_id for event in current}
        for event in self.repository.pending_notifications():
            if event.signal_id in current_ids:
                continue
            try:
                self.notifier.notify(event)
                self.repository.mark_notified(event.signal_id, self._timestamp())
                result["recovered"] += 1
            except Exception as exc:  # Leave failed recovery records retryable too.
                self.repository.mark_delivery_error(event.signal_id, str(exc))
        return result

    run = deliver

    def _timestamp(self) -> str:
        value = self._now()
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()


def __getattr__(name: str):
    """Lazily retain historical notifier re-exports without coupling the stage."""

    if name in {"LogNotifier", "SignalNotifier"}:
        from inv_trend.adapters.detector.alerts.daily_notifier import (
            LogNotifier,
            SignalNotifier,
        )

        return {"LogNotifier": LogNotifier, "SignalNotifier": SignalNotifier}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["LogNotifier", "NotificationDeliveryService", "SignalNotifier"]
