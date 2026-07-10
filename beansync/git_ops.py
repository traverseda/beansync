from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from loguru import logger  # type: ignore[import-not-found]

# Unlike secrets.yaml, the SSH key never defaults into the ledger dir —
# gitignore is one missed entry away from pushing a private key, so it always
# lives in a fixed per-user location outside the git tree. Locally that's the
# user's XDG data dir; the HA add-on overrides this via BEANSYNC_SSH_DIR=/data/ssh
# (set in addon/run.sh) — /data is an implicit per-add-on volume that, unlike
# /config or /share, isn't browsable through HA's file-explorer add-ons.
SSH_DIR = Path(os.environ.get("BEANSYNC_SSH_DIR", str(Path.home() / ".local" / "share" / "beansync" / "ssh")))
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


def _move_children(src: Path, dst: Path) -> None:
    """Move every entry inside `src` into `dst` (both must exist)."""
    for entry in src.iterdir():
        entry.rename(dst / entry.name)


def clone(url: str, target: Path) -> subprocess.CompletedProcess:
    """Clone `url` into `target`.

    If `target` already exists and is non-empty, its contents are moved
    aside to `<target>.backup.<timestamp>` first — `target` itself is left
    in place and only emptied, never renamed, since `target` may be the
    server process's own current working directory (bean-sync serve runs
    with the ledger dir as cwd) and renaming a directory that's a process's
    cwd fails with EBUSY on Linux. On failure, the partial clone is removed
    and the backup contents are moved back, so a bad clone never leaves the
    ledger dir missing or half-populated.
    """
    # Resolve first: LEDGER.parent is the bare relative Path(".") (bean-sync
    # serve runs with the ledger dir as cwd), and Path(".").parent/.name are
    # both degenerate ("." and "" respectively) — building a backup path from
    # those would nest the backup inside itself instead of beside it.
    target = target.resolve()
    backup: Path | None = None
    if target.exists() and any(target.iterdir()):
        backup = target.parent / f"{target.name}.backup.{int(time.time())}"
        backup.mkdir(parents=True)
        _move_children(target, backup)
        logger.info("Moved existing ledger contents to {} before clone", backup)

    target.mkdir(parents=True, exist_ok=True)
    result = _run(["git", "clone", url, str(target)], env=_git_env())

    if result.returncode != 0:
        logger.warning("git clone failed: {}", result.stderr.strip())
        for entry in target.iterdir():
            shutil.rmtree(entry) if entry.is_dir() and not entry.is_symlink() else entry.unlink()
        if backup is not None:
            _move_children(backup, target)
            backup.rmdir()
            logger.info("Restored ledger contents from backup after failed clone")

    return result


def pull(path: Path) -> subprocess.CompletedProcess:
    # --no-rebase is explicit rather than relying on pull.rebase/merge.ff
    # config: this runs headlessly in the add-on container with no way to
    # `git config` interactively, and modern git refuses to pick a
    # reconciliation strategy on its own once branches have diverged
    # ("Need to specify how to reconcile divergent branches"). A real content
    # conflict still leaves the repo in a conflicted state and pull() failing
    # with that in stderr — resolving conflicts needs a human, not autopull.
    return _run(["git", "pull", "--no-rebase"], cwd=path, env=_git_env())


def push(path: Path) -> subprocess.CompletedProcess:
    return _run(["git", "push"], cwd=path, env=_git_env())
