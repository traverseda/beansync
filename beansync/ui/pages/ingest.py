from __future__ import annotations

import fcntl
import os
import pty
import struct
import subprocess
import termios
import threading
from nicegui import ui
from nicegui.events import XtermDataEventArguments, XtermResizeEventArguments

from beansync.config import load_sources


def page() -> None:
    sources = load_sources()
    selected: dict[str, bool] = {s.name: True for s in sources}
    with ui.column().classes("w-full gap-4"):
        ui.label("Ingest").classes("text-2xl font-bold")

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
