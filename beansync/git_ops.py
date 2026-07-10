from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from loguru import logger  # type: ignore[import-not-found]

# HA add-ons get an implicit per-add-on /data volume regardless of the `map:`
# list in config.yaml, and unlike /config or /share it is not browsable
# through HA's file-explorer add-ons — the right place for private key
# material, since the ledger dir itself becomes a git clone target and is
# user-browsable.
SSH_DIR = Path(os.environ.get("BEANSYNC_SSH_DIR", "/data/ssh"))
KEY_PATH = SSH_DIR / "id_ed25519"
PUB_KEY_PATH = SSH_DIR / "id_ed25519.pub"


class GitError(RuntimeError):
    pass


def _run(cmd: list[str], cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True)


def has_ssh_key() -> bool:
    return KEY_PATH.exists()


def public_key() -> str | None:
    return PUB_KEY_PATH.read_text().strip() if PUB_KEY_PATH.exists() else None


def generate_ssh_key(force: bool = False) -> str:
    """Generate an ed25519 keypair for git if one doesn't already exist.

    Returns the public key text. Refuses to overwrite an existing key unless
    force=True — regenerating invalidates the deploy key already registered
    on the remote.
    """
    if KEY_PATH.exists() and not force:
        return PUB_KEY_PATH.read_text().strip()

    SSH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    KEY_PATH.unlink(missing_ok=True)
    PUB_KEY_PATH.unlink(missing_ok=True)

    result = _run(["ssh-keygen", "-t", "ed25519", "-f", str(KEY_PATH), "-N", "", "-C", "beansync"])
    if result.returncode != 0:
        raise GitError(f"ssh-keygen failed: {result.stderr.strip() or result.stdout.strip()}")

    KEY_PATH.chmod(0o600)
    logger.info("Generated new SSH key at {}", KEY_PATH)
    return PUB_KEY_PATH.read_text().strip()


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    ssh_cmd = "ssh -o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
    if KEY_PATH.exists():
        ssh_cmd += f" -i {KEY_PATH}"
    env["GIT_SSH_COMMAND"] = ssh_cmd
    return env


def is_git_repo(path: Path) -> bool:
    return (path / ".git").is_dir()


def remote_url(path: Path) -> str | None:
    result = _run(["git", "remote", "get-url", "origin"], cwd=path)
    return result.stdout.strip() if result.returncode == 0 else None


def clone(url: str, target: Path) -> subprocess.CompletedProcess:
    """Clone `url` into `target`.

    If `target` already exists and is non-empty, it's moved aside to
    `<target>.backup.<timestamp>` first. On failure, the partial clone is
    removed and the backup (if any) is restored, so a bad clone never leaves
    the ledger dir missing or half-populated.
    """
    backup: Path | None = None
    if target.exists() and any(target.iterdir()):
        backup = target.parent / f"{target.name}.backup.{int(time.time())}"
        target.rename(backup)
        logger.info("Moved existing ledger dir to {} before clone", backup)

    target.mkdir(parents=True, exist_ok=True)
    result = _run(["git", "clone", url, str(target)], env=_git_env())

    if result.returncode != 0:
        logger.warning("git clone failed: {}", result.stderr.strip())
        shutil.rmtree(target, ignore_errors=True)
        if backup is not None:
            backup.rename(target)
            logger.info("Restored ledger dir from backup after failed clone")

    return result


def pull(path: Path) -> subprocess.CompletedProcess:
    return _run(["git", "pull"], cwd=path, env=_git_env())


def push(path: Path) -> subprocess.CompletedProcess:
    return _run(["git", "push"], cwd=path, env=_git_env())
