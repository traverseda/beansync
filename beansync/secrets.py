from __future__ import annotations

import os
import re
from pathlib import Path

import yaml  # type: ignore[import-not-found]

# Defaults to the ledger dir (cwd), matching every other path in config.py.
# The ledger dir can be a git clone target (see git_ops.py) and ingest.py's
# commit flow runs `git add -A`, so a secrets file living here can end up
# committed — fine for a private repo the user controls, but worth knowing.
# The HA add-on overrides this via BEANSYNC_SECRETS_DIR=/data (set in
# addon/run.sh), since secret values never worked at all in that deployment
# before (no keyring, no git repo to opt into either).
SECRETS_DIR = Path(os.environ.get("BEANSYNC_SECRETS_DIR", "."))
SECRETS_FILE = SECRETS_DIR / "secrets.yaml"


def _load() -> dict[str, dict[str, str]]:
    if not SECRETS_FILE.exists():
        return {}
    data = yaml.safe_load(SECRETS_FILE.read_text()) or {}
    return data if isinstance(data, dict) else {}


def _save(data: dict[str, dict[str, str]]) -> None:
    if not data:
        SECRETS_FILE.unlink(missing_ok=True)
        return
    SECRETS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SECRETS_FILE.write_text(yaml.dump(data, default_flow_style=False, sort_keys=True))
    SECRETS_FILE.chmod(0o600)


def list_secrets() -> dict[str, str]:
    """Return {name: description} of all registered secrets (never values)."""
    return {name: entry.get("description", "") for name, entry in _load().items()}


def get_secret(name: str) -> str:
    """Resolve a secret by name. Tries secrets.yaml, then SECRET_<NAME> env var."""
    entry = _load().get(name)
    if entry and entry.get("value"):
        return entry["value"]

    env_key = f"SECRET_{re.sub(r'[^A-Z0-9]', '_', name.upper())}"
    val = os.environ.get(env_key)
    if val:
        return val

    raise KeyError(
        f"Secret '{name}' not found. "
        f"Add it in the config editor, or set the {env_key} env var."
    )


def set_secret(name: str, value: str, description: str = "") -> None:
    """Store a secret's value and description in secrets.yaml."""
    data = _load()
    data[name] = {"value": value, "description": description}
    _save(data)


def delete_secret(name: str) -> None:
    """Remove a secret from secrets.yaml."""
    data = _load()
    data.pop(name, None)
    _save(data)
