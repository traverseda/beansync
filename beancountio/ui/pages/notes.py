from __future__ import annotations

import json
import re
from pathlib import Path

from nicegui import ui

NOTES_FILE = Path(".llm_notes.json")


def _load() -> dict[str, str]:
    return json.loads(NOTES_FILE.read_text()) if NOTES_FILE.exists() else {}


def _save(notes: dict[str, str]) -> None:
    NOTES_FILE.write_text(json.dumps(notes, indent=2))


def page() -> None:
    with ui.column().classes("w-full gap-4"):
        ui.label("Merchant Notes").classes("text-2xl font-bold")
        ui.label(
            "Notes are regex patterns matched against source text. "
            "The LLM uses them to classify known merchants without searching."
        ).classes("text-sm text-gray-500")

        rows_container = ui.column().classes("w-full gap-2")

        def render_rows() -> None:
            rows_container.clear()
            notes = _load()
            with rows_container:
                if not notes:
                    ui.label("No notes yet.").classes("text-gray-400 italic")
                    return

                def make_row(pattern: str, value: str) -> None:
                    with ui.card().classes("w-full"):
                        with ui.row().classes("w-full gap-2 items-center"):
                            pat_in = ui.input(value=pattern).props('dense').classes("font-mono flex-1")
                            val_in = ui.input(value=value).props('dense').classes("flex-[2]")

                            def save_edit(old=pattern, pi=pat_in, vi=val_in) -> None:
                                new_key = pi.value.strip()
                                new_val = vi.value.strip()
                                if not new_key or not new_val:
                                    ui.notify("Both fields are required.", type="warning")
                                    return
                                try:
                                    re.compile(new_key, re.IGNORECASE)
                                except re.error as e:
                                    ui.notify(f"Invalid regex: {e}", type="negative")
                                    return
                                d = _load()
                                if old != new_key:
                                    d.pop(old, None)
                                d[new_key] = new_val
                                _save(d)
                                render_rows()
                                ui.notify("Saved.", type="positive")

                            def delete_note(p=pattern) -> None:
                                d = _load()
                                d.pop(p, None)
                                _save(d)
                                render_rows()
                                ui.notify(f"Deleted: {p}", type="positive")

                            ui.button(icon="save", on_click=save_edit).props("flat dense color=positive")
                            ui.button(icon="delete", on_click=delete_note).props("flat dense color=negative")

                for pat, val in sorted(notes.items()):
                    make_row(pat, val)

        render_rows()

        with ui.card().classes("w-full mt-2"):
            ui.label("Add Note").classes("text-lg font-semibold mb-2")
            with ui.row().classes("w-full gap-2 items-end"):
                key_input = ui.input(
                    label="Pattern (regex)",
                    placeholder="e.g. Steam|Valve",
                ).classes("flex-1")
                value_input = ui.input(
                    label="Note",
                    placeholder="e.g. Game purchases → Expenses:Entertainment:Steam",
                ).classes("flex-[2]")

                def add_note() -> None:
                    k = key_input.value.strip()
                    v = value_input.value.strip()
                    if not k or not v:
                        ui.notify("Both fields are required.", type="warning")
                        return
                    try:
                        re.compile(k, re.IGNORECASE)
                    except re.error as e:
                        ui.notify(f"Invalid regex: {e}", type="negative")
                        return
                    notes = _load()
                    notes[k] = v
                    _save(notes)
                    key_input.value = ""
                    value_input.value = ""
                    render_rows()
                    ui.notify("Saved.", type="positive")

                ui.button("Add", icon="add", on_click=add_note).classes("self-end")
