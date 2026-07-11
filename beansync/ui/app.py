from __future__ import annotations

import os
from pathlib import Path

# NiceGUI reads NICEGUI_STORAGE_PATH as a class attribute at import time (see
# nicegui/storage.py), so this must run before `from nicegui import ...`
# below. Without it, app.storage.general (used by chat.py to persist chat
# history) writes .nicegui/storage-general.json into the ledger dir — which
# is a git working tree (see git_ops.py) that gets rewritten on every run and
# will conflict with every future `git pull`. This isn't ledger data, so it
# doesn't belong there at all; addon/run.sh overrides this to /data/nicegui
# in the container, same as BEANSYNC_SECRETS_DIR/BEANSYNC_SSH_DIR.
os.environ.setdefault("NICEGUI_STORAGE_PATH", str(Path.home() / ".local" / "share" / "beansync" / "nicegui"))
# NiceGUI's FilePersistentDict.backup() only does mkdir(exist_ok=True), not
# parents=True — it can't create a multi-level path from scratch, so the dir
# has to exist before NiceGUI's first write.
Path(os.environ["NICEGUI_STORAGE_PATH"]).mkdir(parents=True, exist_ok=True)

from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from loguru import logger
from nicegui import app as nicegui_app, ui
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send


class _HAIngressMiddleware:
    """Fix path and root_path so Starlette routing works behind HA ingress.

    Starlette's get_route_path() strips root_path from scope['path'] before
    matching routes — and does so at every Mount level. This means path must
    always START WITH root_path, otherwise route matching and StaticFiles path
    resolution both break. We set root_path to the ingress prefix and ensure
    path includes that prefix, regardless of whether the supervisor stripped it."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            headers = dict(scope.get("headers", []))
            ingress_path = headers.get(b"x-ingress-path", b"").decode().rstrip("/")
            if ingress_path:
                path: str = scope.get("path", "")
                if not path.startswith(ingress_path):
                    path = ingress_path + path
                scope = {**scope, "root_path": ingress_path, "path": path,
                         "raw_path": path.encode()}
        await self.app(scope, receive, send)


class _NiceGUIStaticCORSMiddleware:
    """Add CORS headers to NiceGUI static assets so HA's service worker can proxy
    ES module fetches without getting opaque responses that fail module loading."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith("/_nicegui/"):
            await self.app(scope, receive, send)
            return

        async def send_with_cors(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend([
                    (b"access-control-allow-origin", b"*"),
                    (b"cross-origin-resource-policy", b"cross-origin"),
                ])
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_cors)


class _NotFoundLoggerMiddleware:
    """Log every 404 with the routed path and ingress header so production
    failures are diagnosable straight from the add-on log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_logging(message: dict) -> None:
            if message["type"] == "http.response.start" and message["status"] == 404:
                headers = dict(scope.get("headers", []))
                logger.warning(
                    "404 {} {} (root_path={!r}, x-ingress-path={!r})",
                    scope.get("method"),
                    scope.get("path"),
                    scope.get("root_path", ""),
                    headers.get(b"x-ingress-path", b"").decode(),
                )
            await send(message)

        await self.app(scope, receive, send_logging)


# Starlette runs later-added middleware first, so execution order is:
# CORS (sees the supervisor-stripped path) -> HAIngress (restores the prefix)
# -> NotFoundLogger (sees the final routed path) -> router.
nicegui_app.add_middleware(_NotFoundLoggerMiddleware)
nicegui_app.add_middleware(_HAIngressMiddleware)
nicegui_app.add_middleware(_NiceGUIStaticCORSMiddleware)

from beansync import scheduler

nicegui_app.on_startup(scheduler.start)


@nicegui_app.get("/_debug/ingress")
async def debug_ingress(request: Request) -> dict:
    return {
        "path": request.scope.get("path"),
        "root_path": request.scope.get("root_path"),
        "raw_path": request.scope.get("raw_path", b"").decode(),
        "x_ingress_path": request.headers.get("x-ingress-path", "(not set)"),
        "headers": dict(request.headers),
    }

from beansync.ui.pages import dashboard, ingest, notes
from beansync.ui.pages import config_editor
from beansync.ui.pages import chat as chat_module


def _sources_root() -> Path:
    return Path("sources").resolve()


def _check_source_path(path: str) -> Path | None:
    filepath = Path(path)
    try:
        filepath.resolve().relative_to(_sources_root())
    except ValueError:
        return None
    return filepath if filepath.exists() else None


@nicegui_app.get("/api/source", response_model=None)
async def serve_source(path: str) -> FileResponse | PlainTextResponse:
    filepath = _check_source_path(path)
    if filepath is None:
        return PlainTextResponse("Not found or access denied", status_code=404)
    return FileResponse(filepath)


@nicegui_app.get("/api/print-packet", response_model=None)
async def print_packet(path: str) -> HTMLResponse | PlainTextResponse:
    from beansync.llm import find_enrichment, html_to_text
    from beansync.config import load_sources
    import re

    source_path = _check_source_path(path)
    if source_path is None:
        return PlainTextResponse("Not found or access denied", status_code=404)

    all_source_dirs = [s.source_dir for s in load_sources()]
    enrichment_dirs = [d for d in all_source_dirs if not source_path.is_relative_to(d)]

    def read_text(p: Path) -> str:
        raw = p.read_text(encoding="utf-8", errors="replace")
        return html_to_text(raw) if p.suffix == ".html" else raw

    def bean_source_path(bean_text: str) -> Path | None:
        m = re.search(r'source:\s*"([^"]+)"', bean_text)
        return Path(m.group(1)) if m else None

    primary_text = read_text(source_path)
    enrichment = find_enrichment(source_path, primary_text, enrichment_dirs)
    linked_paths = [
        p for bt in enrichment
        if (p := bean_source_path(bt)) and p.exists()
    ]

    bean_path = source_path.with_suffix(".bean")

    def section(title: str, content_html: str, page_break: bool = True) -> str:
        pb = "page-break-after: always;" if page_break else ""
        return (
            f'<section style="{pb} margin-bottom: 2em;">'
            f'<h2 style="font-family: sans-serif; font-size: 0.9em; color: #555; '
            f'border-bottom: 1px solid #ccc; padding-bottom: 4px;">{title}</h2>'
            f'{content_html}'
            f'</section>'
        )

    def pre(text: str) -> str:
        import html as htmllib
        return f'<pre style="font-size: 0.8em; white-space: pre-wrap; word-break: break-word;">{htmllib.escape(text)}</pre>'

    def embed_html(p: Path) -> str:
        import nh3
        raw = p.read_text(encoding="utf-8", errors="replace")
        # Per the Email Markup Consortium, email HTML follows the full WHATWG standard.
        # We allow all common elements and strip only scripts/event-handlers (nh3 default).
        email_tags = nh3.ALLOWED_TAGS | {
            "img", "picture", "source",
            "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
            "font", "center", "s", "strike", "u", "sup", "sub", "small",
            "dl", "dt", "dd",
            "header", "footer", "main", "article", "section", "nav", "aside",
            "style",
        }
        universal = {"style", "class", "id", "dir", "lang", "role"}
        presentational = {"width", "height", "align", "valign", "bgcolor"}
        email_attrs: dict[str, set[str]] = {
            tag: nh3.ALLOWED_ATTRIBUTES.get(tag, set()) | universal for tag in email_tags
        }
        for tag in ("table", "tr", "td", "th", "div", "p", "h1", "h2", "h3", "h4", "h5", "h6"):
            email_attrs[tag] |= presentational
        email_attrs["table"] |= {"cellpadding", "cellspacing", "border", "summary"}
        email_attrs["td"] |= {"colspan", "rowspan", "nowrap"}
        email_attrs["th"] |= {"colspan", "rowspan", "scope", "nowrap"}
        email_attrs["img"] |= {"src", "alt", "border", "width", "height"}
        email_attrs["a"] |= {"href", "target", "rel", "name"}
        email_attrs["font"] |= {"color", "size", "face"}
        clean = nh3.clean(raw, tags=email_tags, attributes=email_attrs, link_rel=None,
                          clean_content_tags=nh3.CLEAN_CONTENT_TAGS - {"style"})
        return f'<div style="all: revert; font-size: 0.85em;">{clean}</div>'

    sections = []

    if bean_path.exists():
        sections.append(section(bean_path.name, pre(bean_path.read_text())))

    if source_path.suffix == ".html":
        sections.append(section(source_path.name, embed_html(source_path)))
    else:
        sections.append(section(source_path.name, pre(primary_text)))

    for p in linked_paths:
        if p.suffix == ".html":
            sections.append(section(p.name, embed_html(p)))
        else:
            sections.append(section(p.name, pre(read_text(p))))

    # No page break on last section
    if sections:
        sections[-1] = sections[-1].replace("page-break-after: always;", "")

    html_doc = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{source_path.stem}</title>
<style>
  @media print {{ @page {{ margin: 1.5cm; }} }}
  body {{ margin: 1em; font-family: sans-serif; }}
</style>
</head>
<body>
{''.join(sections)}
</body>
</html>"""

    return HTMLResponse(html_doc)


def _nav_header(request: Request) -> None:
    # HA's ingress panel already shows "Beansync" in its own title bar
    # (addon/config.yaml panel_title) — repeating a brand label here just
    # wastes vertical space in the ingress iframe, especially on mobile.
    behind_ingress = bool(request.headers.get("x-ingress-path"))
    with ui.header().classes("bg-gray-900 items-center gap-1 px-4"):
        if not behind_ingress:
            ui.label("bean-sync").classes("text-lg font-bold text-green-400 mr-2")
        for _label, _path in [
            ("Dashboard", "/"),
            ("Ingest", "/ingest"),
            ("Notes", "/notes"),
            ("Config", "/config"),
        ]:
            ui.button(_label, on_click=lambda p=_path: ui.navigate.to(p)).props(
                "flat"
            ).classes("text-white hover:bg-gray-700")
        ui.button(icon="chat", on_click=lambda: chat_div.set_visibility(not chat_div.visible)).props(
            "flat dense"
        ).classes("text-white ml-auto").tooltip("Finance Assistant")

    chat_div = (
        ui.element("div")
        .style(
            "position: fixed; top: 0; left: 0; right: 0; height: 480px;"
            " background: #1f2937; z-index: 3000; overflow: hidden;"
            " display: flex; flex-direction: column; padding: 16px; gap: 8px;"
            " box-shadow: 0 4px 24px rgba(0,0,0,0.5);"
        )
    )
    chat_div.set_visibility(False)
    with chat_div:
        chat_module.chat_panel(
            set_date_from=lambda v: None,
            set_date_to=lambda v: None,
            set_accounts=lambda v: None,
            refresh_all=lambda: None,
            close=lambda: chat_div.set_visibility(False),
        )


@ui.page("/")
def index(request: Request) -> None:
    _nav_header(request)
    dashboard.page()


@ui.page("/ingest")
def ingest_page(request: Request) -> None:
    _nav_header(request)
    with ui.column().classes("p-6 w-full"):
        ingest.page()


@ui.page("/notes")
def notes_page(request: Request) -> None:
    _nav_header(request)
    with ui.column().classes("p-6 w-full"):
        notes.page()


@ui.page("/config")
def config_page(request: Request) -> None:
    _nav_header(request)
    with ui.column().classes("p-6 w-full"):
        config_editor.page()


def run(host: str = "127.0.0.1", port: int = 8765, reload: bool = False) -> None:
    # no-cache: assets revalidate via ETag (cheap 304s). NiceGUI's default is a
    # year-long immutable cache-control, which let Cloudflare / HA's service
    # worker / browsers keep serving broken assets long after fixes shipped.
    ui.run(host=host, port=port, reload=reload, title="bean-sync", dark=None, show=False, favicon="🫘",
           gzip_middleware_factory=None, cache_control_directives="no-cache")
