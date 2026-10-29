"""One declared authority per data root, shared by the data and storage stacks.

Two database stacks run side by side and must not be confused:

``market``  — the market-data lake opened by ``data.open_data_lake(data_root)``;
``results`` — the research-result store opened by ``storage.open_storage(project)``,
              which nests the same data root under ``<project>/data``.

In both stacks the v3 catalog below is the single writable authority.  The
legacy catalogs stay readable during the retirement window, but a root that
already holds the v3 authority refuses to open them, so a page can never be
looking at one stack while a job writes to the other.
"""

from __future__ import annotations

from pathlib import Path

#: The only writable authority in a data root.
V3_AUTHORITY = "v3"
V3_AUTHORITY_PATH = Path("metadata") / "研究目录_v3.sqlite3"

#: Legacy catalog files that remain readable during the retirement window.
LEGACY_CATALOGS = ("catalog.sqlite3", "catalog.duckdb")
LEGACY_STATUS = "retirement_window"


def resolve_authority(root: str | Path, *, project_scoped: bool = False) -> str:
    """Return the declared authority of one root without opening anything.

    ``project_scoped`` resolves the research-result stack, whose data root is
    ``<project>/data``; the market-data stack passes the data root itself.
    """
    base = Path(root) / "data" if project_scoped else Path(root)
    return V3_AUTHORITY if (base / V3_AUTHORITY_PATH).is_file() else "legacy"
