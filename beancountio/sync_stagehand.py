import os
import subprocess
from datetime import date
from pathlib import Path

from loguru import logger  # type: ignore[import-not-found]

from beancountio.config import SecretRef, StagehandSource


def _resolve(val: str | SecretRef) -> str:
    return val.resolve() if isinstance(val, SecretRef) else val


def fetch(source: StagehandSource, headed: bool = False, since: date | None = None) -> None:
    """Run a Stagehand script to download transactions for a source.

    The script receives BEAN_SYNC_SOURCE_DIR, BEAN_SYNC_SINCE, and BEAN_SYNC_HEADED
    as env vars, plus any secrets defined in source.secrets.
    """
    script = Path(source.script)
    if not script.exists():
        raise FileNotFoundError(f"Stagehand script not found: {script}")

    source.source_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["BEAN_SYNC_SOURCE_DIR"] = str(source.source_dir.resolve())
    env["BEAN_SYNC_HEADED"] = "true" if headed else "false"
    if since:
        env["BEAN_SYNC_SINCE"] = since.isoformat()

    for key, val in source.secrets.items():
        env[key] = _resolve(val)

    logger.info("Running Stagehand script: {}", script)
    subprocess.run(["npx", "tsx", str(script)], env=env, check=True)
