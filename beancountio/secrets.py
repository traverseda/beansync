from __future__ import annotations

import re
from pathlib import Path

import yaml  # type: ignore[import-not-found]
from loguru import logger  # type: ignore[import-not-found]

try:
    import secretstorage  # type: ignore[import-not-found]
    HAS_SECRETSTORAGE = True
except ImportError:
    secretstorage = None  # type: ignore[assignment]
    HAS_SECRETSTORAGE = False

SECRETS_FILE = Path("secrets.yaml")
KEYRING_SERVICE = "bean-sync"
_OLD_KEYRING_SERVICE = "beancountio-email"


def list_secrets() -> dict[str, str]:
    """Return {name: description} of all registered secrets."""
    if not SECRETS_FILE.exists():
        return {}
    data = yaml.safe_load(SECRETS_FILE.read_text()) or {}
    return data if isinstance(data, dict) else {str(k): "" for k in data}


def _save_registry(secrets: dict[str, str]) -> None:
    if secrets:
        SECRETS_FILE.write_text(yaml.dump(secrets, default_flow_style=False, sort_keys=True))
    elif SECRETS_FILE.exists():
        SECRETS_FILE.unlink()


def get_secret(name: str) -> str:
    """Resolve a secret by name. Tries keyring, then SECRET_<NAME> env var."""
    import os

    if HAS_SECRETSTORAGE and secretstorage is not None:
        try:
            conn = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(conn)
            if collection.is_locked():
                collection.unlock()
            items = list(collection.search_items({"service": KEYRING_SERVICE, "name": name}))
            if items:
                item = items[0]
                if item.is_locked():
                    item.unlock()
                return item.get_secret().decode()
        except Exception as exc:
            logger.warning("Keyring lookup for secret '{}' failed: {}", name, exc)

    env_key = f"SECRET_{re.sub(r'[^A-Z0-9]', '_', name.upper())}"
    val = os.environ.get(env_key)
    if val:
        return val

    raise KeyError(
        f"Secret '{name}' not found in keyring or environment. "
        f"Add it in the config editor, or set the {env_key} env var."
    )


def set_secret(name: str, value: str, description: str = "") -> None:
    """Store a secret in keyring and register its name in secrets.yaml."""
    if HAS_SECRETSTORAGE and secretstorage is not None:
        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        if collection.is_locked():
            collection.unlock()
        collection.create_item(
            f"bean-sync: {name}",
            {"service": KEYRING_SERVICE, "name": name},
            value.encode(),
            replace=True,
        )
        logger.info("Secret '{}' saved to keyring.", name)
    else:
        logger.warning("No keyring available — secret '{}' will not persist across sessions.", name)

    secrets = list_secrets()
    secrets[name] = description
    _save_registry(secrets)


def delete_secret(name: str) -> None:
    """Remove a secret from keyring and secrets.yaml."""
    if HAS_SECRETSTORAGE and secretstorage is not None:
        try:
            conn = secretstorage.dbus_init()
            collection = secretstorage.get_default_collection(conn)
            if collection.is_locked():
                collection.unlock()
            for item in list(collection.search_items({"service": KEYRING_SERVICE, "name": name})):
                item.delete()
        except Exception as exc:
            logger.warning("Could not delete '{}' from keyring: {}", name, exc)

    secrets = list_secrets()
    secrets.pop(name, None)
    _save_registry(secrets)


def migrate_old_credentials() -> dict[str, str]:
    """Move old 'beancountio-email' keyring entries to named bean-sync secrets.

    Returns {username: secret_name} for each credential migrated.
    """
    if not (HAS_SECRETSTORAGE and secretstorage is not None):
        return {}

    migrated: dict[str, str] = {}
    try:
        conn = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(conn)
        if collection.is_locked():
            collection.unlock()
        old_items = list(collection.search_items({"service": _OLD_KEYRING_SERVICE}))
        for item in old_items:
            if item.is_locked():
                item.unlock()
            attrs = item.get_attributes()
            username = attrs.get("username", "")
            if not username:
                continue
            password = item.get_secret().decode()
            secret_name = "imap_" + re.sub(r"[^a-z0-9]+", "_", username.lower()).strip("_")
            set_secret(secret_name, password, f"IMAP password for {username} (migrated)")
            item.delete()
            migrated[username] = secret_name
            logger.info("Migrated credentials for {} → secret '{}'", username, secret_name)
    except Exception as exc:
        logger.warning("Old credential migration failed: {}", exc)

    return migrated
