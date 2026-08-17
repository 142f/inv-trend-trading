"""Canonical namespace for the inv-trend trading system.

The package is deliberately organized by responsibility: pure calculations in
``core``, market-data ownership in ``data``, use cases in ``application``,
optional adapters in ``integrations``, presentation in ``observability``, and
thin command boundaries in ``cli``.  The two legacy Turtle implementations
live under ``adapters`` while their remaining responsibilities are migrated.
"""

__all__: list[str] = []
