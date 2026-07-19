import os
import sys
from pathlib import Path

from loguru import logger


def _formatter(record: dict) -> str:
    source = record["extra"].get("source", "")
    source_part = f"[{source}] " if source else ""
    return f"<green>{{time:HH:mm:ss}}</green> <level>{{level:<7}}</level> {source_part}{{message}}\n{{exception}}"


# The HA add-on's own container log only ever shows the current container
# instance's stdout, and a hard kill (e.g. OOM) can restart the container
# before that buffer is even flushed — so a crash mid-run can leave zero
# trace there. A file sink under a persistent, per-add-on volume survives
# that: locally that's the user's XDG data dir, the add-on overrides it via
# BEANSYNC_LOG_DIR=/data/logs (set in addon/run.sh), matching the BEANSYNC_SSH_DIR
# pattern in beansync/git_ops.py.
LOG_DIR = Path(os.environ.get("BEANSYNC_LOG_DIR", str(Path.home() / ".local" / "share" / "beansync" / "logs")))

logger.remove()
logger.add(sys.stderr, format=_formatter)
logger.add(LOG_DIR / "beansync.log", format=_formatter, rotation="10 MB", retention=5)
