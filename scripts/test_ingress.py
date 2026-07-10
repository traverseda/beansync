#!/usr/bin/env python3
"""Verify the app works behind Home Assistant ingress.

Simulates exactly what the HA supervisor forwards to the add-on: the ingress
prefix is stripped from the path and an ``X-Ingress-Path`` header is added
(captured from a real production request). Fetches the index page, then every
asset URL it references, plus the socket.io handshake — the full set of
requests a browser makes to render the UI.

Usage: test_ingress.py [base_url]   (default http://127.0.0.1:8765)
Exits non-zero on any failure.
"""
from __future__ import annotations

import re
import sys

import httpx

INGRESS = "/api/hassio_ingress/TESTTOKEN-fn3XDJ8JJdtTWtiZEqxSpKCPdbx8D"
SUPERVISOR_HEADERS = {
    "X-Ingress-Path": INGRESS,
    "X-Forwarded-For": "203.0.113.7, 172.30.33.0, 172.30.32.1",
    "X-Forwarded-Proto": "https",
    "X-Forwarded-Host": "ha.example.com",
    "X-Hass-Source": "core.ingress",
    "Accept-Encoding": "gzip, deflate, br",
}

PAGES = ["/", "/ingest", "/notes", "/config"]


def supervisor_path(browser_url: str) -> str:
    """The path the add-on receives after the supervisor strips the prefix."""
    if browser_url.startswith(INGRESS):
        return browser_url[len(INGRESS):] or "/"
    return browser_url


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        if not ok:
            failures.append(name)

    with httpx.Client(base_url=base, timeout=15) as client:
        asset_urls: set[str] = set()
        for page in PAGES:
            r = client.get(page, headers=SUPERVISOR_HEADERS)
            check(f"page {page}", r.status_code == 200, f"status {r.status_code}")
            if r.status_code != 200:
                continue
            urls = re.findall(r'(?:src|href)="([^"]+)"', r.text)
            local = [u for u in urls if u.startswith("/")]
            unprefixed = [u for u in local if not u.startswith(INGRESS)]
            check(f"page {page}: all asset URLs carry ingress prefix", not unprefixed,
                  f"unprefixed: {unprefixed[:3]}" if unprefixed else f"{len(local)} URLs")
            asset_urls.update(u for u in local if "/_nicegui/" in u)

        check("index references at least one asset", bool(asset_urls))

        for url in sorted(asset_urls):
            r = client.get(supervisor_path(url), headers=SUPERVISOR_HEADERS)
            ct = r.headers.get("content-type", "")
            ok = r.status_code == 200 and "json" not in ct
            check(f"asset {url.removeprefix(INGRESS)}", ok, f"status {r.status_code}, {ct}")
            if ok and "/static/" in url:
                cc = r.headers.get("cache-control", "")
                check(f"  no long-lived caching on {url.rsplit('/', 1)[-1]}",
                      "immutable" not in cc and "max-age=31536000" not in cc, cc)

        # socket.io polling handshake (what nicegui.js does first)
        r = client.get("/_nicegui_ws/socket.io/?EIO=4&transport=polling",
                       headers=SUPERVISOR_HEADERS)
        check("socket.io handshake", r.status_code == 200 and r.text.startswith("0"),
              f"status {r.status_code}")

    print(f"\n{len(failures)} failure(s)" if failures else "\nall checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
