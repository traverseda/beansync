from __future__ import annotations

from nicegui import context


def app_url(path: str) -> str:
    """Prefix an app-absolute path with the mount prefix of the current client.

    Links built as bare "/api/..." break when bean-sync is mounted on a subpath
    (HA ingress, or any reverse proxy setting X-Forwarded-Prefix) because the
    browser resolves them against the host root, not the mount point. This
    mirrors how NiceGUI itself derives the prefix for its own asset URLs (see
    Client.build_response in nicegui/client.py).
    """
    try:
        request = context.client.request
    except RuntimeError:  # no page context (e.g. background task) — nothing to prefix
        return path
    prefix = request.headers.get("X-Forwarded-Prefix", "") + request.scope.get("root_path", "")
    return prefix.rstrip("/") + path
