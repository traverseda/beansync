from __future__ import annotations

import datetime
import fcntl
import os
import pty
import struct
import subprocess
import termios
import threading
from nicegui import ui
from nicegui.events import XtermDataEventArguments, XtermResizeEventArguments

from beansync import git_ops
from beansync.config import LEDGER, load_sources


async def _commit_dialog() -> None:
    import asyncio

    default_msg = f"Update ledger {datetime.date.today()}"
    with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
        ui.label("Commit Changes").classes("text-lg font-semibold mb-2")
        with ui.element("pre").classes("text-xs bg-gray-800 p-2 rounded overflow-auto max-h-48 w-full"):
            status_text = ui.label("Loading…").classes("text-gray-400")
        msg_input = ui.input("Commit message", value=default_msg).classes("w-full")
        error_label = ui.label("").classes("text-sm text-red-400")

        async def do_commit() -> None:
            msg = msg_input.value.strip()
            if not msg:
                error_label.set_text("Commit message required.")
                return
            add = await asyncio.to_thread(
                subprocess.run, ["git", "add", "-A"],
                capture_output=True, text=True, cwd=str(LEDGER.parent),
            )
            if add.returncode != 0:
                error_label.set_text(f"git add failed: {add.stderr.strip()}")
                return
            commit = await asyncio.to_thread(
                subprocess.run, ["git", "commit", "-m", msg],
                capture_output=True, text=True, cwd=str(LEDGER.parent),
            )
            if commit.returncode == 0:
                ui.notify("Committed successfully.", type="positive")
                dialog.close()
            else:
                error_label.set_text(commit.stdout.strip() or commit.stderr.strip())

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")
            ui.button("Commit", on_click=do_commit).props("color=primary")

    dialog.open()

    result = await asyncio.to_thread(
        subprocess.run, ["git", "status", "--short"],
        capture_output=True, text=True, cwd=str(LEDGER.parent),
    )
    status_text.set_text(result.stdout.strip() or "No changes to commit.")


async def _do_pull() -> None:
    import asyncio
    result = await asyncio.to_thread(git_ops.pull, LEDGER.parent)
    if result.returncode != 0:
        ui.notify(f"Pull failed: {result.stderr.strip()}", type="negative")
        return
    ui.notify(result.stdout.strip() or "Already up to date.", type="positive")


async def _do_push() -> None:
    import asyncio
    result = await asyncio.to_thread(git_ops.push, LEDGER.parent)
    if result.returncode != 0:
        ui.notify(f"Push failed: {result.stderr.strip()}", type="negative")
        return
    ui.notify(result.stdout.strip() or result.stderr.strip() or "Pushed.", type="positive")


def page() -> None:
    sources = load_sources()
    selected: dict[str, bool] = {s.name: True for s in sources}
    with ui.column().classes("w-full gap-4"):
        with ui.row().classes("w-full items-center"):
            ui.label("Ingest").classes("text-2xl font-bold flex-1")
            if git_ops.is_git_repo(LEDGER.parent):
                ui.button("Pull", icon="cloud_download", on_click=_do_pull).props("outline")
                ui.button("Push", icon="cloud_upload", on_click=_do_push).props("outline")
            ui.button("Commit", icon="commit", on_click=_commit_dialog).props("outline")

        term = ui.xterm().classes("w-full").style("height: 400px; overflow: hidden")

        run_btn = ui.button("Run Ingest")

        with ui.expansion("Settings").classes("w-full"):
            with ui.card().classes("w-full"):
                ui.label("Select Sources").classes("text-lg font-semibold mb-2")
                checkboxes: dict[str, ui.checkbox] = {}
                with ui.grid(columns=2).classes("w-full"):
                    for source in sources:
                        cb = ui.checkbox(source.name, value=True)
                        cb.on("update:model-value", lambda v, n=source.name: selected.update({n: v}))
                        checkboxes[source.name] = cb

            with ui.card().classes("w-full"):
                ui.label("Options").classes("text-lg font-semibold mb-2")
                headed = ui.checkbox("Show browser window (CUA only)")
                since_input = ui.input(
                    label="Since date (YYYY-MM-DD, optional)",
                    placeholder="e.g. 2026-06-01",
                ).classes("w-64")

        master_fd: list[int] = []
        current_size: list[int] = [24, 80]  # [rows, cols]

        def on_data(e: XtermDataEventArguments) -> None:
            if master_fd:
                os.write(master_fd[0], e.data.encode())

        def on_resize(e: XtermResizeEventArguments) -> None:
            current_size[0] = e.rows
            current_size[1] = e.cols
            if master_fd:
                winsize = struct.pack("HHHH", e.rows, e.cols, 0, 0)
                fcntl.ioctl(master_fd[0], termios.TIOCSWINSZ, winsize)

        term.on_data(on_data)
        term.on_resize(on_resize)
        ui.element('q-resize-observer').on('resize', lambda _: term.fit())

        def run_ingest() -> None:
            names = [n for n, v in selected.items() if v]
            if not names:
                ui.notify("Select at least one source.", type="warning")
                return

            cmd = ["bean-sync", "ingest"] + names
            if headed.value:
                cmd += ["--headed"]
            since_val = since_input.value.strip()
            if since_val:
                cmd += ["--since", since_val]

            run_btn.disable()
            term.clear()

            def _stream() -> None:
                mfd, sfd = pty.openpty()
                master_fd.clear()
                master_fd.append(mfd)
                winsize = struct.pack("HHHH", current_size[0], current_size[1], 0, 0)
                fcntl.ioctl(sfd, termios.TIOCSWINSZ, winsize)
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdin=sfd,
                        stdout=sfd,
                        stderr=sfd,
                        close_fds=True,
                        preexec_fn=os.setsid,
                    )
                    os.close(sfd)
                    while True:
                        try:
                            data = os.read(mfd, 4096)
                        except OSError:
                            break
                        if not data:
                            break
                        term.write(data)
                    proc.wait()
                finally:
                    master_fd.clear()
                    try:
                        os.close(mfd)
                    except OSError:
                        pass
                    run_btn.enable()
                    run_btn.update()

            threading.Thread(target=_stream, daemon=True).start()

        run_btn.on_click(run_ingest)
