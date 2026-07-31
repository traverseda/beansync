from __future__ import annotations

import datetime as dt
import fcntl
import os
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from croniter import croniter  # type: ignore[import-not-found]
from loguru import logger  # type: ignore[import-not-found]

# Ingest can be triggered from three places that don't share process memory:
# this scheduler (in-process), the Ingest page's manual "Run Ingest" button
# (spawns `bean-sync ingest` as a subprocess via PTY), and a user running
# `bean-sync ingest` by hand in a terminal. A threading.Lock only protects
# same-process callers, so coordination has to go through a file instead.
#
# The lock is an advisory fcntl.flock on that file, NOT the file's existence:
# the kernel drops a flock as soon as the holding process dies, however it
# dies (crash, SIGKILL, container stop), so a lock can never go stale. The
# earlier pid-liveness check could not manage that — pids restart from 1 in a
# container, so after a restart an unrelated process would frequently occupy
# the recorded pid and the lock would look held forever. The file's *contents*
# are only ever a human-readable hint for the error message.
_LOCK_FILE = Path("sources/state/ingest.lock")


@contextmanager
def ingest_lock() -> Generator[None]:
    """Raises RuntimeError if another live ingest (any process) is already running."""
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = os.pread(fd, 256, 0).decode(errors="replace").strip() or "unknown process"
            raise RuntimeError(f"Another ingest is already running ({holder}).")
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"pid {os.getpid()}, started {dt.datetime.now().isoformat(timespec='seconds')}".encode(), 0)
        yield
    finally:
        # Closing the fd releases the flock. The file itself is deliberately
        # left behind: unlinking it would race with another process that has
        # already opened it and is waiting on the lock.
        os.close(fd)


_last_check: dt.datetime | None = None


def _check_and_maybe_run() -> None:
    global _last_check
    from beansync.config import load_config

    now = dt.datetime.now()
    try:
        cron_expr = load_config().ingest_cron.strip()
    except Exception as exc:
        logger.warning("Could not load config for schedule check: {}", exc)
        return

    if not cron_expr:
        _last_check = now
        return
    if _last_check is None:
        # First tick after startup: seed state without firing, so restarting
        # the add-on never causes a surprise immediate ingest.
        _last_check = now
        return
    if not croniter.is_valid(cron_expr):
        logger.warning("Invalid ingest_cron {!r}, skipping schedule check", cron_expr)
        _last_check = now
        return

    if croniter(cron_expr, _last_check).get_next(dt.datetime) <= now:
        threading.Thread(target=_run_ingest_once, daemon=True).start()
    _last_check = now


def _run_ingest_once() -> None:
    import typer

    from beansync.cli import ingest as cli_ingest

    try:
        logger.info("Scheduled ingest starting")
        # unattended=True: no terminal is watching this thread, so ask_user()
        # must not block on stdin — it raises QuestionDeferred instead, which
        # the parse loops turn into a queued entry on the Questions page.
        cli_ingest(names=None, headed=False, since=None, unattended=True)
        logger.success("Scheduled ingest completed")
    except (SystemExit, typer.Exit):
        pass  # e.g. lock already held — cli.ingest() already logged why.
        # typer.Exit is a RuntimeError subclass in this version, not
        # SystemExit, so it must be caught explicitly here too.
    except Exception as exc:
        # The app's custom loguru formatter (beansync/__init__.py) doesn't
        # include {exception}, so logger.exception()'s traceback would
        # otherwise be silently dropped — this runs unattended with no
        # terminal watching it, so the log has to be self-contained.
        logger.error("Scheduled ingest failed: {}: {}", type(exc).__name__, exc)


def start() -> None:
    from nicegui import app

    app.timer(60, _check_and_maybe_run)
