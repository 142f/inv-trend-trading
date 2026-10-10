from __future__ import annotations

import pytest

from inv_trend.core.serializers import (
    as_mapping,
    canonical_json_bytes,
    freeze_json,
    mapping_rows,
    thaw_json,
)


def test_freeze_detaches_nested_mutable_containers() -> None:
    source = {"items": [{"value": 1}]}

    frozen = freeze_json(source)
    source["items"][0]["value"] = 2

    assert thaw_json(frozen) == {"items": [{"value": 1}]}
    with pytest.raises(TypeError):
        frozen["new"] = "blocked"


def test_shared_mapping_helpers_drop_non_mappings_without_copying_mappings() -> None:
    payload = {"a": 1}
    assert as_mapping(payload) is payload
    assert as_mapping(None) == {}
    assert as_mapping([payload]) == {}
    assert mapping_rows([payload, "skip", {"b": 2}, None]) == [payload, {"b": 2}]
    assert mapping_rows({"a": 1}) == []
    assert mapping_rows(None) == []


def test_canonical_json_is_order_independent_and_rejects_nan() -> None:
    assert canonical_json_bytes({"b": 2, "a": 1}) == canonical_json_bytes({"a": 1, "b": 2})
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})
