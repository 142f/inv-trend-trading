from __future__ import annotations

from signals.resonance import compute_r_score, direction_gate


def test_r_score_and_gate():
    weights = {"7d": 24, "5d": 20, "2d": 16, "1d": 14, "4h": 10, "2h": 7, "1h": 5, "30m": 3, "15m": 1}
    states = {"7d": 1, "5d": 1, "2d": 1, "1d": 1, "4h": 1, "2h": 1, "1h": 0, "30m": 0, "15m": 0}
    r = compute_r_score(states, weights)
    assert r > 0.55
    gate = direction_gate(states, weights, long_gate=0.55, short_gate=-0.55)
    assert gate.allow_long is True
    assert gate.allow_short is False
