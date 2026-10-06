"""Real loopback HTTP verifies request identity, rejection, retry classes and timeout."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Event, Thread

import pytest

from inv_trend.notifications.渠道 import NotificationError, WebhookNotifier


@contextmanager
def receiver(*, status=200, redirect=None, hold=None):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return None

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            received.append({'body': body, 'identity': self.headers['Idempotency-Key'],
                             'content_type': self.headers['Content-Type']})
            if hold is not None:
                assert hold.wait(3)
            self.send_response(status)
            if redirect:
                self.send_header('Location', redirect)
            self.send_header('X-Request-Id', 'receiver-1')
            self.end_headers()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield 'http://127.0.0.1:' + str(server.server_port) + '/PRIVATE_WEBHOOK_TOKEN', received
    finally:
        if hold is not None:
            hold.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def event():
    return {'event_id': 'a' * 64, 'event_type': 'MARKET_SIGNAL', 'event_name': 'CANDIDATE',
            'instrument_id': 'BTCUSDT.BINANCE.SPOT', 'timeframe': 'H4'}


def test_webhook_posts_real_json_and_returns_observable_acknowledgement():
    with receiver() as (url, received):
        channel = WebhookNotifier(url)
        result = channel.send(event())
        assert result.event_id == event()['event_id'] and result.http_status == 200
        assert result.remote_request_id == 'receiver-1'
        assert received[0]['body'] == event() and received[0]['identity'] == event()['event_id']
        assert received[0]['content_type'].startswith('application/json')
        assert 'PRIVATE_WEBHOOK_TOKEN' not in repr(result) + channel.channel_id


def test_transport_retries_keep_the_same_remote_idempotency_key():
    with receiver() as (url, received):
        channel = WebhookNotifier(url)
        channel.send(event())
        channel.send(event())
        assert len(received) == 2
        assert len({item['identity'] for item in received}) == 1
        # Generic remote endpoints must implement deduplication using this key.


@pytest.mark.parametrize('status,retryable', [(400, False), (401, False), (429, True), (500, True), (503, True)])
def test_webhook_http_errors_are_typed_and_do_not_leak_destination(status, retryable):
    with receiver(status=status) as (url, received):
        with pytest.raises(NotificationError) as error:
            WebhookNotifier(url).send(event())
        assert error.value.retryable is retryable and error.value.http_status == status
        assert 'PRIVATE_WEBHOOK_TOKEN' not in str(error.value)
        assert len(received) == 1


def test_webhook_does_not_follow_redirect_or_forward_payload():
    with receiver() as (other_url, other_received):
        with receiver(status=302, redirect=other_url) as (url, received):
            with pytest.raises(NotificationError) as error:
                WebhookNotifier(url).send(event())
            assert error.value.http_status == 302 and error.value.retryable is False
            assert len(received) == 1 and other_received == []


def test_webhook_timeout_is_retryable_and_bounded():
    release = Event()
    with receiver(hold=release) as (url, received):
        with pytest.raises(NotificationError) as error:
            WebhookNotifier(url, timeout_seconds=.1).send(event())
        assert error.value.retryable is True and error.value.http_status is None
        assert len(received) == 1


@pytest.mark.parametrize('url', ['http://example.org/hook', 'file:///hook', 'https://user:secret@example.org/hook',
                               'https://example.org/hook#secret', 'ftp://127.0.0.1/hook'])
def test_destinations_require_explicit_safe_http_transport(url):
    with pytest.raises(ValueError):
        WebhookNotifier(url)


@pytest.mark.parametrize('payload', [{'event_id': 'bad\r\nHeader: value'},
                                    {'event_id': 'a' * 64, 'close': float('nan')}])
def test_bad_notification_payload_never_reaches_http(payload):
    with receiver() as (url, received):
        with pytest.raises(NotificationError) as error:
            WebhookNotifier(url).send(payload)
        assert error.value.retryable is False and received == []
