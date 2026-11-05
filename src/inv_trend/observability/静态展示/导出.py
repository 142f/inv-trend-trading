"""Export fixed showcase pages and the latest generated snapshot without writing source data."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path, PurePosixPath
import shutil

from .目录 import GENERATED_FILES, get_page

PACKAGE = "inv_trend.observability.静态展示"
PAGES = resources.files(PACKAGE).joinpath("pages")


@dataclass(frozen=True)
class ExportResult:
    written: tuple[Path, ...]
    missing: tuple[str, ...]


def _copy_file(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != target.resolve():
        shutil.copy2(source, target)
    return target


def _copy_resource_tree(source, target: Path) -> list[Path]:
    written: list[Path] = []
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        if child.is_dir():
            written.extend(_copy_resource_tree(child, target / child.name))
        elif child.is_file():
            destination = target / child.name
            destination.write_bytes(child.read_bytes())
            written.append(destination)
    return written


def _export_asset(page, output: Path) -> list[Path]:
    relative = PurePosixPath(page.path)
    source = PAGES.joinpath(*relative.parts)
    if not source.is_file():
        raise FileNotFoundError(f"packaged page is missing: {page.path}")
    if relative.parent == PurePosixPath("."):
        destination = output / relative.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        return [destination]
    source_dir = PAGES.joinpath(*relative.parent.parts)
    return _copy_resource_tree(source_dir, output / relative.parent)


def _export_generated(page, output: Path, root: Path) -> list[Path]:
    deliverables = root / "deliverables"
    present: list[Path] = []
    missing: list[str] = []
    for name in sorted(GENERATED_FILES):
        source = deliverables / name
        if source.is_file():
            present.append(source)
        else:
            missing.append(name)
    if page.path in missing:
        raise FileNotFoundError(
            f"generated page is missing: {deliverables / page.path}",
        )
    return [_copy_file(source, output / source.name) for source in present]


def export_page(page_id: str, output: str | Path, *, root: str | Path | None = None) -> list[Path]:
    page = get_page(page_id)
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    if page.kind == "asset":
        return _export_asset(page, destination)
    return _export_generated(page, destination, Path(root or Path.cwd()).resolve())


def export_all(output: str | Path, *, root: str | Path | None = None) -> ExportResult:
    destination = Path(output)
    destination.mkdir(parents=True, exist_ok=True)
    written = _copy_resource_tree(PAGES, destination)
    missing: list[str] = []
    deliverables = Path(root or Path.cwd()).resolve() / "deliverables"
    for name in sorted(GENERATED_FILES):
        source = deliverables / name
        if source.is_file():
            written.append(_copy_file(source, destination / name))
        else:
            missing.append(name)
    return ExportResult(tuple(written), tuple(missing))
