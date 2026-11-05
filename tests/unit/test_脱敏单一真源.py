"""Redaction must be identical wherever an artifact or message is written."""

from __future__ import annotations

from inv_trend.infra.脱敏 import redact as infra_redact
from inv_trend.observability.脱敏 import redact as observability_redact
from inv_trend.storage.基础 import redact as storage_redact


_PAYLOADS = (
    {"token": "abc", "signature": "deadbeef"},
    {"api_key": "key-123", "period": 2},
    {"nested": [{"access_token": "t", "keep": 1}]},
    {"webhook": "https://hooks.example.com/services/T1/B2/SUPERSECRET"},
    "password=hunter2 token=SECRET",
    "authorization: Bearer abc.def",
    '{"api_key": "XYZ", "period": 2}',
    "plain message without secrets",
)


def test_all_three_entry_points_share_one_rule_set() -> None:
    for payload in _PAYLOADS:
        expected = infra_redact(payload)
        assert storage_redact(payload) == expected
        assert observability_redact(payload) == expected


def test_storage_manifest_secrets_are_actually_removed() -> None:
    """The storage layer writes manifests and run blobs, not just messages."""

    secret_values = (
        {"token": "abc", "signature": "deadbeef"},
        {"webhook": "https://hooks.example.com/services/T1/B2/SUPERSECRET"},
        "password=hunter2 token=SECRET",
    )
    forbidden = ("abc", "deadbeef", "SUPERSECRET", "hunter2", "SECRET")
    rendered = [str(storage_redact(payload)) for payload in secret_values]
    for value in forbidden:
        assert all(value not in text for text in rendered), value


def test_long_strings_keep_the_historical_truncation() -> None:
    long_value = "x" * 5000
    assert len(storage_redact(long_value)) == 2048
    assert storage_redact(long_value, max_length=None) == long_value
