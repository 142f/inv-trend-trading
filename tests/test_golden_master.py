"""Read-only Golden Master regression for shared D1 strategy behavior."""

from __future__ import annotations

import json

from tests.golden_master_support import (
    build_current_snapshot,
    expected_path,
    first_difference,
    fixture_path,
    fixture_sha256,
    load_bars,
)


def test_shared_paths_match_the_recertified_d1_golden_master() -> None:
    """Keep events/orders exact and floats within the documented 1e-10 tolerance.

    This test never writes fixture assets. Intentional updates must go through
    ``scripts/refresh_golden_master.py --confirm`` so the pinned Git baseline is
    compared first and the first business difference is visible to reviewers.
    """

    fixture = fixture_path()
    expected = json.loads(expected_path().read_text(encoding="utf-8"))

    assert expected["schema_version"] == 2
    assert expected["fixture"]["instrument_id"] == "BTCUSDT.BINANCE.SPOT"
    assert expected["fixture"]["dataset_version"] == "028585a633484f05948afa5f"
    assert expected["fixture"]["bars"] == 420
    assert expected["certification"]["status"] == "re-certified"
    assert expected["certification"]["baseline_comparison"] == "equivalent"
    assert fixture_sha256(fixture) == expected["input_sha256"]
    assert b"\r\n" not in fixture.read_bytes()

    bars = load_bars(fixture)
    assert len(bars) == 420
    actual = build_current_snapshot(bars)
    difference = first_difference(actual, expected["outputs"])
    assert difference is None, (
        "Golden Master mismatch. Inspect the first business difference before "
        "intentionally running scripts/refresh_golden_master.py --confirm: "
        f"{difference}"
    )
