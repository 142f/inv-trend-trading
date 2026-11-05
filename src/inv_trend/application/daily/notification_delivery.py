"""Outbox delivery component for the modular D1 workflow.

The service deliberately operates only on events already committed to the
daily outbox.  The commit boundary admits only confirmed execution-decision
events into that outbox.  Delivery preserves retry and recovery semantics while
making them reusable outside the monolithic daily workflow.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from inv_trend.core.时间 import utc_datetime

from .ports import DailyStateRepositoryPort, NotificationPort

if TYPE_CHECKING:
    # These historical names remain available through ``__getattr__`` below
    # for callers that imported them from this compatibility module.  Keeping
    # the concrete notifier import out of the application stage makes the
    # delivery service depend only on its notification port.
    from inv_trend.adapters.detector.alerts.daily_notifier import LogNotifier, SignalNotifier


class NotificationDeliveryService:
    """Deliver confirmed decision events and retry earlier failed entries."""

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

        # ``errors`` is the counter the run status and the CLI exit code read.
        # A recovery failure that only logged would let an undelivered
        # notification look like a completed run.
        result = {"notified": 0, "errors": 0, "recovered": 0, "persist_errors": 0}
        current = self.repository.pending_notifications(run_id)
        for event in current:
            outcome = self._deliver_one(event)
            if outcome == "delivered":
                result["notified"] += 1
                continue
            result["errors"] += 1
            if outcome == "unrecorded":
                result["persist_errors"] += 1

        if not include_recovery:
            return result
        current_ids = {event.signal_id for event in current}
        for event in self.repository.pending_notifications():
            if event.signal_id in current_ids:
                continue
            outcome = self._deliver_one(event)
            if outcome == "delivered":
                result["recovered"] += 1
                continue
            result["errors"] += 1
            if outcome == "unrecorded":
                result["persist_errors"] += 1
        return result

    def _deliver_one(self, event) -> str:
        """Notify one event and persist the outcome.

        Returns ``delivered`` on success, ``notify_failed`` when the channel
        rejected the event (it stays retryable) and ``unrecorded`` when the
        notification left but the outbox write failed, which must be visible
        rather than silently counted as a success.
        """

        try:
            self.notifier.notify(event)
        except Exception as exc:  # Delivery errors remain in the outbox for retry.
            self.repository.mark_delivery_error(event.signal_id, str(exc))
            return "notify_failed"
        try:
            self.repository.mark_notified(event.signal_id, self._timestamp())
        except Exception as exc:  # The notification is out; the record is not.
            self.repository.mark_delivery_error(
                event.signal_id, f"notified but unrecorded: {exc}"
            )
            return "unrecorded"
        return "delivered"

    run = deliver

    def _timestamp(self) -> str:
        return utc_datetime(self._now()).isoformat()


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
