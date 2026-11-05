"""Stable identifiers for reproducible strategy runs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from uuid import uuid4
from typing import Any, Mapping


def stable_id(*parts: Any) -> str:
    """Return the 24-character identity shared by reports, episodes, and candidates.

    Historical inputs are joined with ``|`` exactly as before, so every ID already
    persisted in reports/episodes/transitions stays byte-identical.  A part that
    itself contains the delimiter would let two different tuples collapse onto the
    same string (``("a|b", "c")`` vs ``("a", "b|c")``); those inputs now use a
    length-prefixed, domain-separated encoding that cannot collide.  The switch is
    deterministic and depends only on the input, never on stored state.
    """

    rendered = [str(part) for part in parts]
    if not any("|" in part for part in rendered):
        # Legacy byte layout, unchanged: every already-persisted ID survives.
        payload = "|".join(rendered).encode("utf-8")
    else:
        # Length-prefix every part and prepend a NUL domain tag.  A legacy join
        # of ordinary values cannot start with NUL, so the two layouts are
        # separated; the length prefixes make the encoding injective for tuples.
        body = "|".join(f"{len(part)}:{part}" for part in rendered)
        payload = b"\x00v2|" + body.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class RunId:
    value: str

    @classmethod
    def create(cls) -> "RunId":
        return cls(uuid4().hex)


@dataclass(frozen=True)
class ConfigFingerprint:
    value: str

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "ConfigFingerprint":
        return cls(_fingerprint(config))


@dataclass(frozen=True)
class DataFingerprint:
    value: str

    @classmethod
    def from_mapping(cls, manifest: Mapping[str, Any]) -> "DataFingerprint":
        return cls(_fingerprint(manifest))
