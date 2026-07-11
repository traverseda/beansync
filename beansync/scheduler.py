from __future__ import annotations

import datetime as dt
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
_LOCK_FILE = Path("sources/state/ingest.lock")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


@contextmanager
def ingest_lock() -> Generator[None]:
    """Raises RuntimeError if another ingest (any process) is already running."""
    if _LOCK_FILE.exists():
        try:
            pid_str, _, started = _LOCK_FILE.read_text().partition(" ")
            pid = int(pid_str)
        except (ValueError, OSError):
            pid = -1
            started = "unknown time"
        if pid > 0 and _pid_alive(pid):
            raise RuntimeError(f"Another ingest is already running (pid {pid}, started {started}).")
        # Stale lock from a crashed/killed process — safe to reclaim.

    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE.write_text(f"{os.getpid()} {dt.datetime.now().isoformat()}")
    try:
        yield
    finally:
        _LOCK_FILE.unlink(missing_ok=True)


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
        cli_ingest(names=None, headed=False, since=None)
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
