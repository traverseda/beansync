from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from nicegui import ui

from beansync import images
from beansync.config import load_sources
from beansync.llm import find_enrichment, html_to_text
from beansync.ui.urls import app_url


def _display_source(path: Path) -> None:
    if path.suffix.lower() in images.SUFFIXES:
        _display_image(path)
        return
    if path.suffix == ".html":
        url = app_url(f"/api/source?path={quote(str(path))}")
        ui.element("iframe").props(f'src="{url}" sandbox="allow-same-origin"').style(
            "width:100%;height:480px;border:none;background:white;"
        )
    else:
        ui.code(path.read_text(encoding="utf-8", errors="replace"), language="text").classes("w-full text-xs")


def _display_image(path: Path) -> None:
    """Show a receipt photo, defaulting to the flattened copy where one exists.

    The flattened version is what you actually want to read; the original is kept
    one tab away because a bad auto-crop should never hide the real evidence.
    """
    flat = images.flat_path(path)

    def img(p: Path) -> None:
        ui.image(app_url(f"/api/source?path={quote(str(p))}")).classes(
            "w-full max-w-2xl"
        ).style("background:#111;")

    if not flat.exists():
        img(path)
        return
    with ui.tabs().props("dense").classes("w-full") as tabs:
        flat_tab = ui.tab("Flattened")
        orig_tab = ui.tab("Original photo")
    with ui.tab_panels(tabs, value=flat_tab).classes("w-full"):
        with ui.tab_panel(flat_tab):
            img(flat)
        with ui.tab_panel(orig_tab):
            img(path)


def _read_source(path: Path) -> str:
    if path.suffix.lower() in images.SUFFIXES:
        # Binary. There's no text to match enrichment against, and decoding it
        # would just feed megabytes of mojibake into find_enrichment.
        return ""
    raw = path.read_text(encoding="utf-8", errors="replace")
    return html_to_text(raw) if path.suffix == ".html" else raw


def _bean_source_path(bean_text: str) -> Path | None:
    """Extract the source: field path from a beancount snippet."""
    m = re.search(r'source:\s*"([^"]+)"', bean_text)
    return Path(m.group(1)) if m else None


def source_viewer_dialog(raw_source: str) -> None:
    """Open a dialog showing the raw source file and any enrichment matches."""
    source_path = Path(raw_source)

    all_source_dirs = [s.source_dir for s in load_sources()]
    enrichment_dirs = [d for d in all_source_dirs if not source_path.is_relative_to(d)]

    with ui.dialog().props("maximized") as dialog, ui.card().classes("w-full h-full rounded-none overflow-hidden"):
        packet_url = app_url(f"/api/print-packet?path={quote(raw_source)}")

        with ui.row().classes("w-full justify-between items-start mb-1"):
            with ui.column().classes("gap-0 min-w-0"):
                ui.label(str(source_path)).classes("text-xs text-gray-500 truncate")
            with ui.row().classes("gap-0 shrink-0"):
                ui.button(
                    icon="print",
                    on_click=lambda: ui.run_javascript(f"window.open('{packet_url}').print()"),
                ).props("flat dense").tooltip("Print receipt packet")
                ui.button(icon="close", on_click=dialog.close).props("flat dense")

        ui.separator()

        if not source_path.exists():
            ui.label(f"File not found: {raw_source}").classes("text-red-400 mt-2")
            dialog.open()
            return

        content = _read_source(source_path)
        enrichment = find_enrichment(source_path, content, enrichment_dirs)

        bean_path = source_path.with_suffix(".bean")

        with ui.scroll_area().classes("w-full").style("height:520px"):
            if bean_path.exists():
                ui.label("Ledger entry").classes("text-xs font-semibold text-gray-400 mb-1")
                ui.code(bean_path.read_text(), language="text").classes("w-full text-xs")
                ui.separator().classes("my-2")

            ui.label("Raw source").classes("text-xs font-semibold text-gray-400 mb-1")
            _display_source(source_path)

            linked_paths = [
                p
                for bean_text in enrichment
                if (p := _bean_source_path(bean_text)) and p.exists()
            ]
            if linked_paths:
                ui.separator().classes("my-3")
                with ui.expansion(f"Related sources ({len(linked_paths)})").classes("w-full text-xs"):
                    for path in linked_paths:
                        ui.label(path.name).classes("text-xs text-gray-500 mt-2")
                        _display_source(path)

    dialog.open()
