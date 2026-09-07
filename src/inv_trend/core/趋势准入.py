"""Shared, I/O-free trend gate for research and daily decisions."""
from __future__ import annotations

import math


def trend_filter(side, price, reference):
    if side not in (-1, 1) or not all(math.isfinite(v) and v > 0 for v in (price, reference)):
        return False
    return side * (price - reference) >= 0


def confirmed_eligibility(eligibility):
    return (str(eligibility.get('status', '')).upper() == 'PASSED' and
            eligibility.get('evaluated') is True and eligibility.get('passed') is True)
