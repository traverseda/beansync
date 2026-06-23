from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from nicegui import app as nicegui_app, ui

from beancountio.ui.pages import dashboard, ingest, notes
from beancountio.ui.pages import config_editor


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
    from beancountio.llm import find_enrichment, html_to_text
    from beancountio.config import load_sources
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


def _nav_drawer() -> None:
    with ui.left_drawer(value=True).classes("bg-gray-900 text-white"):
        ui.label("bean-sync").classes("text-xl font-bold px-4 pt-4 pb-2 text-green-400")
        with ui.column().classes("gap-1 px-2"):
            ui.button("Dashboard", on_click=lambda: ui.navigate.to("/")).props(
                "flat align=left"
            ).classes("w-full text-white hover:bg-gray-700")
            ui.button("Ingest", on_click=lambda: ui.navigate.to("/ingest")).props(
                "flat align=left"
            ).classes("w-full text-white hover:bg-gray-700")
            ui.button("Notes", on_click=lambda: ui.navigate.to("/notes")).props(
                "flat align=left"
            ).classes("w-full text-white hover:bg-gray-700")
            ui.button("Config", on_click=lambda: ui.navigate.to("/config")).props(
                "flat align=left"
            ).classes("w-full text-white hover:bg-gray-700")


@ui.page("/")
def index() -> None:
    _nav_drawer()
    with ui.column().classes("p-6 w-full"):
        dashboard.page()


@ui.page("/ingest")
def ingest_page() -> None:
    _nav_drawer()
    with ui.column().classes("p-6 w-full"):
        ingest.page()


@ui.page("/notes")
def notes_page() -> None:
    _nav_drawer()
    with ui.column().classes("p-6 w-full"):
        notes.page()


@ui.page("/config")
def config_page() -> None:
    _nav_drawer()
    with ui.column().classes("p-6 w-full"):
        config_editor.page()


def run(host: str = "127.0.0.1", port: int = 8080, reload: bool = False) -> None:
    ui.run(host=host, port=port, reload=reload, title="bean-sync", dark=None, show=False, favicon="🫘")
