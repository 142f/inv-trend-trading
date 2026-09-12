from __future__ import annotations

import pytest

from inv_trend.core.serializers import canonical_json_bytes, freeze_json, thaw_json


def test_freeze_detaches_nested_mutable_containers() -> None:
    source = {"items": [{"value": 1}]}

    frozen = freeze_json(source)
    source["items"][0]["value"] = 2

    assert thaw_json(frozen) == {"items": [{"value": 1}]}
    with pytest.raises(TypeError):
        frozen["new"] = "blocked"


def test_canonical_json_is_order_independent_and_rejects_nan() -> None:
    assert canonical_json_bytes({"b": 2, "a": 1}) == canonical_json_bytes({"a": 1, "b": 2})
    with pytest.raises(ValueError):
        canonical_json_bytes({"value": float("nan")})
