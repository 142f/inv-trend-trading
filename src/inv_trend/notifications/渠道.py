"""Explicit webhook destinations, stable event identity and bounded HTTP I/O."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
import re
from typing import Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from inv_trend.observability.脱敏 import redact


@dataclass(frozen=True)
class DeliveryReceipt:
    channel_id: str
    event_id: str
    http_status: int
    remote_request_id: str | None = None


class NotificationError(RuntimeError):
    def __init__(self, message, *, retryable, http_status=None):
        super().__init__(redact(message))
        self.retryable = retryable
        self.http_status = http_status


class NotificationChannel(Protocol):
    channel_id: str

    def send(self, event: Mapping[str, object]) -> DeliveryReceipt: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WebhookNotifier:
    """POST one event; remote exactly-once delivery requires event_id deduplication.

    HTTPS is required except loopback HTTP for local integration. The URL is
    held only in memory; identities and errors never contain its secret path.
    No implicit provider, credential or proxy environment is consulted.
    """

    def __init__(self, url: str, *, timeout_seconds=10.):
        if not isinstance(url, str):
            raise ValueError('webhook destination must be an explicit URL')
        parsed = urlsplit(url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError('webhook URL requires a host and forbids userinfo/fragments')
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError('webhook port is invalid')
        local = parsed.hostname.lower() == 'localhost'
        if not local:
            try:
                local = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                local = False
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and local):
            raise ValueError('webhook requires HTTPS; HTTP is allowed only on loopback')
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError('webhook timeout must be finite and positive')
        self._url, self.timeout_seconds = url, timeout_seconds
        self.channel_id = 'webhook:' + hashlib.sha256(url.encode()).hexdigest()
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def send(self, event: Mapping[str, object]) -> DeliveryReceipt:
        event_id = event.get('event_id')
        if not isinstance(event_id, str) or not re.fullmatch('[a-f0-9]{64}', event_id):
            raise NotificationError('notification event identity must be a SHA-256 id', retryable=False)
        try:
            body = json.dumps(event, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')
        except (ValueError, TypeError) as exc:
            raise NotificationError('notification payload is not finite JSON: ' + str(exc), retryable=False) from exc
        request = Request(self._url, data=body, method='POST', headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Idempotency-Key': event_id,
            'X-Monitor-Event-Id': event_id,
            'User-Agent': 'inv-trend-monitor/1',
        })
        try:
            with self._opener.open(request, timeout=self.timeout_seconds) as response:
                status = response.status
                if not 200 <= status < 300:
                    raise NotificationError(f'webhook returned HTTP {status}',
                                            retryable=status == 429 or status >= 500, http_status=status)
                # Endpoint acceptance is observable; human receipt is not.
                return DeliveryReceipt(self.channel_id, event_id, status,
                                       redact(response.headers.get('X-Request-Id')))
        except HTTPError as exc:
            exc.close()
            raise NotificationError(f'webhook returned HTTP {exc.code}',
                                    retryable=exc.code == 429 or exc.code >= 500, http_status=exc.code) from exc
        except (URLError, OSError) as exc:
            raise NotificationError('webhook transport failed: ' + str(exc), retryable=True) from exc
