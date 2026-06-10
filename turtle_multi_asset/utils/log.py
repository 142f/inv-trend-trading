"""Idempotent logging setup for command-line and research workflows."""

from __future__ import annotations

import logging


def setup_logging(
    level: str | int = "INFO",
    fmt: str = "%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt: str = "%Y-%m-%d %H:%M:%S",
) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter(fmt=fmt, datefmt=datefmt)
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
        return
    for handler in root.handlers:
        handler.setFormatter(formatter)
