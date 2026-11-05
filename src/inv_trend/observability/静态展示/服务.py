"""Read-only local preview server for fixed showcase pages."""

from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from .目录 import GENERATED_FILES, PAGES

PACKAGE = "inv_trend.observability.静态展示"
PAGES_ROOT = resources.files(PACKAGE).joinpath("pages")
ALLOWED_SUFFIXES = {".html", ".js", ".css", ".ttf", ".txt", ".json"}
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".ttf": "font/ttf",
    ".txt": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
}


def _available(page, root: Path) -> bool:
    if page.kind == "asset":
        return PAGES_ROOT.joinpath(*PurePosixPath(page.path).parts).is_file()
    return (root / "deliverables" / page.path).is_file()


def render_index(root: str | Path) -> str:
    root = Path(root).resolve()
    cards = []
    for page in PAGES:
        available = _available(page, root)
        href = "/pages/" + quote(page.path, safe="/")
        if available:
            link = f'<a href="{href}">{escape(page.title)}</a>'
            status = "可预览"
        else:
            link = escape(page.title)
            status = "需先生成"
        cards.append(
            '<li><div class="row"><strong>' + link + '</strong>'
            + f'<span class="state">{status}</span></div>'
            + '<p>' + escape(page.description) + '</p>'
            + f'<code>{escape(page.id)}</code></li>'
        )
    return f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>固定展示页面</title>
<style>
body{{font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;margin:0;background:#f4f6f8;color:#1e293b}}
main{{max-width:900px;margin:40px auto;padding:0 20px}}
h1{{font-size:28px}}ul{{list-style:none;padding:0;display:grid;gap:14px}}
li{{background:#fff;border:1px solid #dbe2ea;border-radius:12px;padding:18px 20px}}
.row{{display:flex;justify-content:space-between;gap:12px;align-items:center}}
a{{color:#0c5c56;text-decoration:none;font-size:18px}}a:hover{{text-decoration:underline}}
p{{color:#52647b;margin:8px 0}}.state{{font-size:12px;color:#8a5a12;background:#fff3d6;border-radius:999px;padding:2px 9px}}
code{{font-size:12px;color:#64748b}}
</style>
<main><h1>固定展示页面</h1>
<p>只读预览；美股前100看板需先运行 <code>python scripts/美股前100指标.py</code>。</p>
<ul>{''.join(cards)}</ul></main></html>"""


def _safe_parts(raw: str) -> PurePosixPath | None:
    relative = PurePosixPath(raw)
    if relative.is_absolute() or not relative.parts:
        return None
    if any(part in {"", ".", ".."} or part.startswith(".") for part in relative.parts):
        return None
    if relative.suffix.lower() not in ALLOWED_SUFFIXES:
        return None
    return relative


def _read_package(relative: PurePosixPath) -> bytes | None:
    target = PAGES_ROOT.joinpath(*relative.parts)
    return target.read_bytes() if target.is_file() else None


def _read_generated(relative: PurePosixPath, root: Path) -> bytes | None:
    name = relative.as_posix()
    if name not in GENERATED_FILES:
        return None
    deliverables = root / "deliverables"
    target = (deliverables / relative).resolve()
    if not target.is_relative_to(deliverables.resolve()) or not target.is_file():
        return None
    return target.read_bytes()


class _Handler(BaseHTTPRequestHandler):
    server_version = "StaticShowcase/1.0"

    def do_GET(self) -> None:
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            self._send(200, "text/html; charset=utf-8", render_index(self.server.root).encode("utf-8"))
            return
        if path.startswith("/pages/"):
            relative = _safe_parts(path[len("/pages/"):])
            payload = _read_package(relative) if relative else None
            if payload is None and relative is not None:
                payload = _read_generated(relative, self.server.root)
            if payload is not None:
                self._send(200, CONTENT_TYPES[relative.suffix.lower()], payload)
                return
        self._not_found()

    def _send(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def _not_found(self) -> None:
        self._send(404, "text/html; charset=utf-8", b"<h1>404</h1><p>page not found</p>")

    def log_message(self, format: str, *args) -> None:
        return


def create_server(root: str | Path, port: int = 8765, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), _Handler)
    server.daemon_threads = True
    server.root = Path(root).resolve()
    return server


def serve(root: str | Path, port: int = 8765) -> None:
    server = create_server(root, port)
    print(f"static-pages: http://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
