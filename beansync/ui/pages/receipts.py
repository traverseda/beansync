"""Upload receipt photos from the browser and manage the ones already on disk.

Covers the half of ImageSource that `bean-sync ingest` doesn't: those source dirs
are filled by hand (see ImageSource.fetch, which is a no-op), which until now
meant SSHing into the box or mounting a share to drop a JPEG. This page is that
drop, plus a view of what has landed and whether the AI made sense of it.
"""

from __future__ import annotations

import asyncio
import datetime
import re
from pathlib import Path
from urllib.parse import quote

from loguru import logger
from nicegui import ui
from nicegui.events import UploadEventArguments

from beansync import images
from beansync.config import ImageSource, load_config, save_config
from beansync.ui.source_viewer import source_viewer_dialog
from beansync.ui.urls import app_url


def _image_sources() -> list[ImageSource]:
    return [s for s in load_config().sources if isinstance(s, ImageSource)]


def _exif_date(path: Path) -> datetime.date | None:
    """Prefer the date the photo was taken over the date it was uploaded.

    A receipt photographed on Friday and uploaded on Sunday should file under
    Friday — and parse_image feeds the filename's date prefix to the model as
    its date hint, so getting this wrong quietly misdates the transaction.
    """
    try:
        from PIL import Image

        with Image.open(path) as img:
            exif = img.getexif()
        # 36867 DateTimeOriginal, 306 DateTime
        raw = exif.get(36867) or exif.get(306)
        if raw:
            return datetime.datetime.strptime(str(raw)[:10], "%Y:%m:%d").date()
    except Exception:
        pass
    return None


def _slugify(name: str) -> str:
    stem = Path(name).stem
    # Strip a date the phone or a previous upload already prefixed, so we don't
    # end up with 2026-07-31_2026-07-31_foo.jpg
    stem = re.sub(r"^\d{4}[-_]\d{2}[-_]\d{2}[-_ ]*", "", stem)
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", stem).strip("_").lower()
    return slug[:48] or "receipt"


def _unique_path(directory: Path, date: datetime.date, slug: str, suffix: str) -> Path:
    base = f"{date.isoformat()}_{slug}"
    candidate = directory / f"{base}{suffix}"
    n = 2
    while candidate.exists():
        candidate = directory / f"{base}_{n}{suffix}"
        n += 1
    return candidate


def save_upload(source: ImageSource, filename: str, data: bytes) -> Path:
    """Write an uploaded photo into a source dir under the naming ingest expects."""
    source.source_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(filename).suffix.lower() or ".jpg"
    if suffix not in images.SUFFIXES:
        raise ValueError(f"Unsupported image type: {suffix or filename}")

    # Written under a temp name first: the real name needs the EXIF date, which
    # needs the bytes on disk.
    staging = source.source_dir / f".upload_{_slugify(filename)}{suffix}"
    staging.write_bytes(data)
    date = _exif_date(staging) or datetime.date.today()
    target = _unique_path(source.source_dir, date, _slugify(filename), suffix)
    staging.rename(target)
    return images.normalize(target)


def parse_one(source: ImageSource, path: Path) -> str:
    """Run the vision parse for a single receipt. Returns a status line for the UI."""
    from beansync import llm
    from beansync import questions as questions_store
    from beansync.config import load_accounts

    accounts = load_accounts()
    null_instr = llm.NULL_INSTRUCTION if source.nullable else llm.NO_NULL_INSTRUCTION
    enrichment_note = llm.ENRICHMENT_NOTE if source.enrichment else ""
    prompt = llm.RECEIPT_SYSTEM_PROMPT_TEMPLATE.format(
        hint=source.hint, accounts=accounts, null_instruction=null_instr,
        enrichment_note=enrichment_note,
    )

    # Nobody is guaranteed to still have the tab open when this finishes, so
    # ask_user must not block — uncertain receipts become Questions instead.
    token = llm.UNATTENDED.set(True)
    try:
        entry = llm.parse_image(
            path, prompt, nullable=source.nullable, is_enrichment=source.enrichment,
            extra_context=questions_store.answered_context_for(path),
        )
    except questions_store.QuestionDeferred as exc:
        questions_store.queue_question(source.name, path, exc.question, exc.options)
        return f"{path.name}: the AI asked a question — see the Questions page."
    finally:
        llm.UNATTENDED.reset(token)

    questions_store.clear_answered_for(path)
    path.with_suffix(".bean").write_text(entry + "\n" if entry else "")
    return f"{path.name}: {'transaction saved' if entry else 'no transaction found'}"


def _status(path: Path) -> tuple[str, str]:
    """(label, tailwind colour class) for a receipt's sidecar state."""
    sidecar = path.with_suffix(".bean")
    if not sidecar.exists():
        return "unparsed", "bg-gray-600 text-gray-100"
    if not sidecar.read_text().strip():
        return "no transaction", "bg-yellow-800 text-yellow-100"
    return "parsed", "bg-green-800 text-green-100"


def _receipts(sources: list[ImageSource]) -> list[tuple[ImageSource, Path]]:
    found: list[tuple[ImageSource, Path]] = []
    for source in sources:
        if not source.source_dir.exists():
            continue
        for path in source.source_dir.rglob("*"):
            if (
                path.is_file()
                and path.suffix.lower() in images.SUFFIXES
                and not images.is_flat_copy(path)
                and not path.name.startswith(".")
            ):
                found.append((source, path))
    # Newest first — filenames are date-prefixed, so this is reverse chronological.
    return sorted(found, key=lambda pair: pair[1].name, reverse=True)


def _create_source_dialog(on_done) -> None:
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-lg"):
        ui.label("Add a receipts source").classes("text-lg font-semibold")
        ui.label(
            "Uploads need somewhere to live. This adds an ImageSource to config.yaml."
        ).classes("text-sm text-gray-500 mb-2")
        name = ui.input("Name", value="receipts").classes("w-full")
        directory = ui.input("Directory", value="sources/receipts").classes("w-full")
        hint = ui.textarea(
            "Hint for the AI",
            value="Photos of paper receipts for personal household spending. "
                  "Pay from Assets:Checking:CUA unless the receipt shows another card.",
        ).classes("w-full")

        def create() -> None:
            if not name.value.strip() or not directory.value.strip():
                ui.notify("Name and directory are required.", type="warning")
                return
            config = load_config()
            config.sources.append(ImageSource(
                name=name.value.strip(),
                source_dir=Path(directory.value.strip()),
                hint=hint.value.strip(),
            ))
            save_config(config)
            # Primary sources are included into main.bean by glob, so this has to
            # be rewritten before the first sidecar can count toward balances.
            from beansync.config import write_primary_includes

            write_primary_includes(config.sources)
            dialog.close()
            ui.notify(f"Created source '{name.value.strip()}'.", type="positive")
            on_done()

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Create", on_click=create).props("color=primary")
    dialog.open()


def page() -> None:
    sources = _image_sources()
    root = ui.column().classes("w-full gap-4")

    with root:
        with ui.row().classes("w-full items-center"):
            ui.label("Receipts").classes("text-2xl font-bold flex-1")

        if not sources:
            with ui.card().classes("w-full"):
                ui.label("No image sources configured.").classes("text-gray-400 italic")
                ui.label(
                    "Receipt photos are stored in an ImageSource directory. "
                    "Create one to start uploading."
                ).classes("text-sm text-gray-500")
                ui.button(
                    "Add a receipts source", icon="add",
                    on_click=lambda: _create_source_dialog(lambda: ui.navigate.reload()),
                ).props("color=primary").classes("mt-2")
            return

        selected: dict[str, ImageSource] = {"source": sources[0]}
        # Declared before the upload card so handlers can close over it, but only
        # placed on the page after it — uploading is the point of this page, so
        # the drop zone belongs above the list of what's already there.
        gallery = ui.column().classes("w-full gap-3")

        def render() -> None:
            gallery.clear()
            items = _receipts(sources)
            with gallery:
                ui.label(f"{len(items)} receipt(s)").classes("text-sm text-gray-500")
                if not items:
                    ui.label("Nothing uploaded yet.").classes("text-gray-400 italic")
                    return
                for source, path in items:
                    _receipt_card(source, path, render)

        async def handle_upload(e: UploadEventArguments) -> None:
            source = selected["source"]
            data = await e.file.read()
            try:
                path = await asyncio.to_thread(save_upload, source, e.file.name, data)
            except ValueError as exc:
                ui.notify(str(exc), type="negative")
                return
            except Exception as exc:
                logger.exception("upload of {} failed", e.file.name)
                ui.notify(f"Upload failed: {exc}", type="negative")
                return

            ui.notify(f"Uploaded {path.name} — asking the AI…")
            render()
            try:
                status = await asyncio.to_thread(parse_one, source, path)
            except Exception as exc:
                logger.exception("parse of {} failed", path)
                ui.notify(f"{path.name}: parse failed ({exc}). Re-parse to retry.", type="negative")
                render()
                return
            ui.notify(status, type="positive")
            render()

        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center gap-4"):
                ui.label("Upload to").classes("text-sm text-gray-400")
                ui.select(
                    {s.name: s.name for s in sources},
                    value=sources[0].name,
                    on_change=lambda e: selected.update(
                        {"source": next(s for s in sources if s.name == e.value)}
                    ),
                ).props("dense outlined").classes("min-w-40")
            ui.upload(
                on_upload=handle_upload,
                multiple=True,
                auto_upload=True,
                label="Drop receipt photos here, or take one with your phone camera",
            ).props('accept="image/*" capture="environment"').classes("w-full")
            ui.label(
                "Each photo is EXIF-rotated, downscaled, and sent to the vision model, "
                "which also returns the receipt's corners so a flattened copy is saved "
                "alongside the original."
            ).classes("text-xs text-gray-500")

        gallery.move(root)
        render()


def _receipt_card(source, path: Path, refresh) -> None:
    label, colour = _status(path)
    sidecar = path.with_suffix(".bean")
    flat = images.flat_path(path)
    # The flattened copy is the better thumbnail when it exists — it's the
    # receipt without the tablecloth.
    thumb = flat if flat.exists() else path
    thumb_url = app_url(f"/api/source?path={quote(str(thumb))}")

    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-start gap-4 no-wrap"):
            ui.image(thumb_url).classes(
                "w-24 h-32 object-cover rounded cursor-pointer shrink-0 bg-gray-800"
            ).on("click", lambda: source_viewer_dialog(str(path)))

            with ui.column().classes("flex-1 gap-1 min-w-0"):
                with ui.row().classes("items-center gap-2 w-full"):
                    ui.label(path.name).classes("text-sm font-mono truncate")
                    ui.label(label).classes(f"text-xs px-2 py-0.5 rounded {colour}")
                    if flat.exists():
                        ui.label("flattened").classes(
                            "text-xs px-2 py-0.5 rounded bg-blue-900 text-blue-100"
                        )
                ui.label(source.name).classes("text-xs text-gray-500")

                if sidecar.exists() and sidecar.read_text().strip():
                    ui.code(sidecar.read_text().strip(), language="text").classes("w-full text-xs")

            with ui.column().classes("gap-1 shrink-0"):
                async def reparse() -> None:
                    # The sidecar is what marks a receipt done; clearing it is
                    # what makes the parse rerun.
                    sidecar.unlink(missing_ok=True)
                    ui.notify(f"Re-parsing {path.name}…")
                    try:
                        status = await asyncio.to_thread(parse_one, source, path)
                    except Exception as exc:
                        logger.exception("re-parse of {} failed", path)
                        ui.notify(f"Re-parse failed: {exc}", type="negative")
                        refresh()
                        return
                    ui.notify(status, type="positive")
                    refresh()

                def delete() -> None:
                    with ui.dialog() as dialog, ui.card():
                        ui.label(f"Delete {path.name}?").classes("font-semibold")
                        ui.label(
                            "Removes the photo, its flattened copy, and its ledger entry."
                        ).classes("text-sm text-gray-500")

                        def confirm() -> None:
                            path.unlink(missing_ok=True)
                            sidecar.unlink(missing_ok=True)
                            flat.unlink(missing_ok=True)
                            dialog.close()
                            ui.notify(f"Deleted {path.name}.", type="positive")
                            refresh()

                        with ui.row().classes("w-full justify-end gap-2 mt-2"):
                            ui.button("Cancel", on_click=dialog.close).props("flat")
                            ui.button("Delete", on_click=confirm).props("color=negative")
                    dialog.open()

                ui.button(icon="visibility", on_click=lambda: source_viewer_dialog(str(path))).props(
                    "flat dense"
                ).tooltip("View receipt and ledger entry")
                ui.button(icon="refresh", on_click=reparse).props("flat dense").tooltip("Re-parse with the AI")
                ui.button(icon="delete", on_click=delete).props("flat dense color=negative").tooltip("Delete")
