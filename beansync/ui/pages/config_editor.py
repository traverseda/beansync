from __future__ import annotations

import dataclasses as _dc
from pathlib import Path
from typing import Union, get_args as _ga, get_origin as _go, get_type_hints as _gth

import yaml  # type: ignore[import-not-found]
from nicegui import ui  # type: ignore[import-not-found]

from beansync.config import (
    AnySource, Config, SecretRef, _Dumper, _Loader, _PLUGIN_REGISTRY,
    _source_from_dict, load_config, save_config, write_primary_includes,
)
from beansync import secrets as _secrets


# Fields rendered the same way for every source type.
_STANDARD_FIELDS = frozenset({"name", "source_dir", "hint", "nullable", "enrichment"})

_SOURCE_DESCRIPTIONS: dict[str, str] = {
    "email": (
        "Fetches emails from specific senders (e.g. bank transaction alerts) via IMAP. "
        "Each matching email becomes one transaction."
    ),
    "email-receipt": (
        "Scans your whole inbox for receipts and invoices from any sender, "
        "used as enrichment context matched to authoritative bank transactions."
    ),
    "cua": (
        "Browser AI agent that logs into your bank's website and downloads transactions. "
        "Use when the bank sends no per-transaction email alerts."
    ),
    "image": (
        "Parses receipt photos (JPG, PNG, WEBP) dropped into a folder. "
        "No credentials required — just drag files in."
    ),
    "stagehand": (
        "Runs a custom Stagehand browser-automation script to fetch transactions. "
        "For advanced users with their own scraping scripts."
    ),
}

_PLUGIN_SETUP_NOTES: dict[str, str] = {
    "email": (
        "**Gmail:** generate an App Password at myaccount.google.com → Security → App Passwords. "
        "Store it with `bean-sync secrets set <name>`, then pick that secret in the Password field below. "
        "IMAP host: `imap.gmail.com` — IMAP user: your full Gmail address."
    ),
    "email-receipt": (
        "Same IMAP credentials as an Email source. "
        "This source reads your whole inbox — individual EmailSources automatically exclude their own senders, "
        "so this one only sees receipts from unknown senders."
    ),
    "cua": (
        "The browser agent logs in on your behalf. Store your bank username and password as secrets (use the Secrets panel), "
        "then select them in the fields below. "
        "Enable **Show browser window** on the Ingest page the first time to catch login or 2FA issues."
    ),
    "image": (
        "Drop JPG, PNG, or WEBP receipt photos into the source folder. "
        "bean-sync will parse them automatically on the next ingest run — no credentials needed."
    ),
    "stagehand": (
        "Point **Script path** at a `.ts` Stagehand script that logs in and exports transactions. "
        "See the project README for the expected script interface."
    ),
}

_GETTING_STARTED_MD = """\
**How bean-sync works**

1. **Define sources** — each source below connects bean-sync to one financial account or inbox.
2. **Run Ingest** — bean-sync fetches raw data (emails, screenshots, receipt photos) into `sources/`.
3. **LLM parsing** — each raw file is converted into a beancount `.bean` transaction by an AI.
4. **Review** — check the Dashboard and edit any transactions that look wrong.

**Primary vs Enrichment sources**

- **Primary** (Enrichment only = off) — the source's parsed transactions are included in your ledger \
(`main.bean`). Use this for any source that is the *authoritative* record of a transaction: bank alert \
emails, browser-scraped statements, receipt photos.
- **Enrichment only** — the source's `.bean` files are used as *context* when the LLM parses other sources, \
but are NOT included in `main.bean`. Use this for inbox receipt scanning (where the bank alert is the \
authoritative record and the receipt just clarifies the payee). bean-sync automatically manages \
`sources/_primary.bean` — don't edit it by hand.

**Nullable** — when enabled, the LLM may return null for files that contain no transaction \
(e.g. marketing emails, payment confirmations). Disable for sources where every file is guaranteed to be a charge.

**Hint field** — a plain-English prompt that tells the LLM how to parse each source. Include: the institution \
name, which beancount account to use for the liability/asset side, and when to return null. Be specific.

**Secrets** — passwords and API keys are never stored in `config.yaml`. \
Use the Secrets panel to store them in your system keyring, then reference them by name in source fields.
"""


def _plugin_label(source: AnySource) -> str:
    return getattr(type(source), "_label", source.plugin)


def _config_to_yaml(config: Config) -> str:
    return yaml.dump(
        config, Dumper=_Dumper,
        default_flow_style=False, allow_unicode=True, sort_keys=False,
    )


def _yaml_to_config(text: str) -> Config:
    raw = yaml.load(text, Loader=_Loader)
    if isinstance(raw, Config):
        return raw
    registered = tuple(_PLUGIN_REGISTRY.values())
    sources: list[AnySource] = []
    for s in raw.get("sources", []):
        if isinstance(s, registered):  # type: ignore[arg-type]
            sources.append(s)  # type: ignore[arg-type]
        else:
            sources.append(_source_from_dict(s))
    return Config(sources=sources)


def _new_source(cls: type) -> AnySource:
    return cls(name="new_source", source_dir=Path("sources/new_source"), hint="")  # type: ignore[return-value]


def _secret_name(val: str | SecretRef) -> str:
    return val.name if isinstance(val, SecretRef) else ""


# --- Type classification for auto-generated form fields ---

def _ftype(f: _dc.Field, hints: dict) -> str:  # type: ignore[type-arg]
    """Return a widget-type string based on the field's type annotation."""
    h = hints.get(f.name)
    if h is str:
        return "str"
    if h is int:
        return "int"
    if h is bool:
        return "bool"
    if h is Path:
        return "path"
    origin = _go(h)
    args = _ga(h)
    if origin is list and args == (str,):
        return "list_str"
    if origin is Union and SecretRef in args:
        return "secret"
    if origin is dict and len(args) == 2 and SecretRef in _ga(args[1]):
        return "secrets_dict"
    return "str"


def _field_label(f: _dc.Field) -> str:  # type: ignore[type-arg]
    if f.metadata and "label" in f.metadata:
        return f.metadata["label"]
    return f.name.replace("_", " ").title()


def page() -> None:
    with ui.column().classes("w-full gap-4"):
        ui.label("Config Editor").classes("text-2xl font-bold")

        with ui.expansion("Getting Started", icon="help_outline").classes("w-full border border-gray-700 rounded"):
            ui.markdown(_GETTING_STARTED_MD).classes("text-sm text-gray-300 p-2")

        config_ref: list[Config] = [load_config()]
        current_tab: list[str] = ["gui"]
        gui_container: list[ui.column] = []
        yaml_editor: list[ui.codemirror] = []
        secrets_container: list[ui.column] = []
        git_container: list[ui.column] = []

        # Per-source input refs: list of {field_name: widget}
        source_inputs: list[dict] = []

        def _capture() -> None:
            for i, inp in enumerate(source_inputs):
                src = config_ref[0].sources[i]
                src.name = inp["name"].value.strip()
                src.source_dir = Path(inp["source_dir"].value.strip())
                src.nullable = inp["nullable"].value
                src.enrichment = inp["enrichment"].value
                src.hint = inp["hint"].value

                hints = _gth(type(src))
                for f in _dc.fields(src):
                    if f.name in _STANDARD_FIELDS:
                        continue
                    widget = inp.get(f.name)
                    if widget is None:
                        continue
                    ft = _ftype(f, hints)
                    if ft == "secret":
                        v = widget.value
                        setattr(src, f.name, SecretRef(v) if v else "")
                    elif ft == "list_str":
                        setattr(src, f.name, [ln.strip() for ln in widget.value.splitlines() if ln.strip()])
                    elif ft == "int":
                        try:
                            setattr(src, f.name, int(widget.value or 0))
                        except (ValueError, TypeError):
                            pass
                    elif ft == "bool":
                        setattr(src, f.name, bool(widget.value))
                    elif ft == "secrets_dict":
                        result: dict = {}
                        for line in (widget.value or "").splitlines():
                            if "=" in line:
                                k, _, v = line.partition("=")
                                k, v = k.strip(), v.strip()
                                if k and v:
                                    result[k] = SecretRef(v)
                                elif k:
                                    result[k] = ""
                        setattr(src, f.name, result)
                    else:
                        setattr(src, f.name, widget.value or "")

        def _render_secrets() -> None:
            if not secrets_container:
                return
            secrets_container[0].clear()
            with secrets_container[0]:
                known = _secrets.list_secrets()
                if known:
                    with ui.column().classes("w-full gap-1"):
                        for sname, desc in known.items():
                            with ui.row().classes("items-center gap-2"):
                                ui.icon("key").classes("text-yellow-400")
                                ui.label(sname).classes("font-mono font-bold")
                                if desc:
                                    ui.label(desc).classes("text-gray-400 text-sm")
                                ui.space()

                                def _del(n=sname) -> None:
                                    _secrets.delete_secret(n)
                                    _render_secrets()
                                    _render_gui()

                                ui.button(icon="delete", on_click=_del).props("flat dense color=negative")
                else:
                    ui.label("No secrets yet.").classes("text-gray-400 text-sm")

                ui.separator()
                with ui.row().classes("items-end gap-2 flex-wrap"):
                    new_name = ui.input("Secret name").props("dense").classes("w-40")
                    new_value = ui.input("Value (password)", password=True).props("dense").classes("w-48")
                    new_desc = ui.input("Description (optional)").props("dense").classes("flex-1 min-w-40")

                    def _add_secret() -> None:
                        n = new_name.value.strip()
                        v = new_value.value
                        if not n:
                            ui.notify("Secret name is required", type="warning")
                            return
                        if not v:
                            ui.notify("Value is required", type="warning")
                            return
                        _secrets.set_secret(n, v, new_desc.value.strip())
                        new_name.set_value("")
                        new_value.set_value("")
                        new_desc.set_value("")
                        _render_secrets()
                        _render_gui()

                    ui.button("Add Secret", icon="add", on_click=_add_secret).props("color=primary dense")

        def _render_git() -> None:
            import asyncio
            from beansync import git_ops
            from beansync.config import LEDGER

            if not git_container:
                return
            git_container[0].clear()
            with git_container[0]:
                with ui.row().classes("items-center gap-2"):
                    ui.icon("vpn_key").classes("text-yellow-400")
                    if git_ops.has_ssh_key():
                        ui.label("SSH key generated").classes("text-sm")
                    else:
                        ui.label("No SSH key yet").classes("text-sm text-gray-400")
                    ui.space()

                    async def _do_keygen(force: bool = False) -> None:
                        try:
                            await asyncio.to_thread(git_ops.generate_ssh_key, force)
                        except git_ops.GitError as e:
                            ui.notify(str(e), type="negative")
                            return
                        ui.notify("SSH key generated.", type="positive")
                        _render_git()

                    async def _regen_confirm() -> None:
                        with ui.dialog() as dialog, ui.card():
                            ui.label(
                                "Regenerating invalidates the deploy key already "
                                "registered on your git host. Continue?"
                            ).classes("text-sm")
                            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                                ui.button("Cancel", on_click=dialog.close).props("flat")

                                async def _confirm() -> None:
                                    dialog.close()
                                    await _do_keygen(force=True)

                                ui.button("Regenerate", on_click=_confirm).props("color=negative")
                        dialog.open()

                    if git_ops.has_ssh_key():
                        ui.button("Regenerate", on_click=_regen_confirm).props("flat dense")
                    else:
                        ui.button("Generate SSH Key", icon="add", on_click=lambda: _do_keygen()).props("color=primary dense")

                pubkey = git_ops.public_key()
                if pubkey:
                    ui.label(
                        "Add this as a deploy key on your git host "
                        "(e.g. GitHub: repo Settings → Deploy keys → Add key):"
                    ).classes("text-xs text-gray-400")
                    ui.code(pubkey).classes("w-full text-xs break-all")

                ui.separator()

                ledger_dir = LEDGER.parent
                if git_ops.is_git_repo(ledger_dir):
                    url = git_ops.remote_url(ledger_dir)
                    with ui.row().classes("items-center gap-2"):
                        ui.icon("check_circle").classes("text-green-400")
                        ui.label(f"Connected to {url}" if url else "Git repo (no remote configured)").classes("text-sm font-mono")
                else:
                    ui.label(
                        "Clone a remote repo into the ledger directory. Any existing "
                        "files will be moved to a backup folder first."
                    ).classes("text-xs text-gray-400")
                    with ui.row().classes("items-end gap-2 flex-wrap"):
                        url_input = ui.input("Repository URL").props("dense").classes("flex-1 min-w-64")

                        async def _do_clone() -> None:
                            url = url_input.value.strip()
                            if not url:
                                ui.notify("Repository URL is required", type="warning")
                                return
                            with ui.dialog() as dialog, ui.card():
                                ui.label(
                                    f"Any existing files in {ledger_dir} will be moved to a "
                                    "timestamped backup folder, then the repository will be "
                                    "cloned in their place. Continue?"
                                ).classes("text-sm")
                                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                                    ui.button("Cancel", on_click=dialog.close).props("flat")

                                    async def _confirm_clone() -> None:
                                        dialog.close()
                                        result = await asyncio.to_thread(git_ops.clone, url, ledger_dir)
                                        if result.returncode != 0:
                                            ui.notify(f"Clone failed: {result.stderr.strip()}", type="negative")
                                            return
                                        ui.notify("Cloned successfully. Reloading…", type="positive")
                                        ui.navigate.reload()

                                    ui.button("Clone", on_click=_confirm_clone).props("color=primary")
                            dialog.open()

                        ui.button("Clone", icon="download", on_click=_do_clone).props("color=primary dense")

        def _render_gui() -> None:
            source_inputs.clear()
            gui_container[0].clear()
            with gui_container[0]:
                for idx, source in enumerate(config_ref[0].sources):
                    _render_source_card(idx, source)

                with ui.row().classes("gap-2 mt-2"):
                    for plugin_name, cls in _PLUGIN_REGISTRY.items():
                        label = getattr(cls, "_label", plugin_name)
                        desc = _SOURCE_DESCRIPTIONS.get(plugin_name, "")
                        def _add(c=cls) -> None:
                            _capture()
                            config_ref[0].sources.append(_new_source(c))
                            _render_gui()
                        btn = ui.button(f"+ {label}", on_click=_add).props("flat outline dense")
                        if desc:
                            btn.tooltip(desc)

        def _render_source_card(idx: int, source: AnySource) -> None:
            inp: dict = {}
            source_inputs.append(inp)
            known_secrets = list(_secrets.list_secrets().keys())
            hints = _gth(type(source))

            with ui.card().classes("w-full"):
                with ui.row().classes("w-full items-center gap-2 mb-1"):
                    ui.badge(_plugin_label(source)).props("color=teal")
                    ui.space()

                    def _move_up(i=idx) -> None:
                        _capture()
                        s = config_ref[0].sources
                        if i > 0:
                            s[i - 1], s[i] = s[i], s[i - 1]
                        _render_gui()

                    def _move_down(i=idx) -> None:
                        _capture()
                        s = config_ref[0].sources
                        if i < len(s) - 1:
                            s[i], s[i + 1] = s[i + 1], s[i]
                        _render_gui()

                    def _delete(i=idx) -> None:
                        _capture()
                        del config_ref[0].sources[i]
                        _render_gui()

                    ui.button(icon="arrow_upward", on_click=_move_up).props("flat dense")
                    ui.button(icon="arrow_downward", on_click=_move_down).props("flat dense")
                    ui.button(icon="delete", on_click=_delete).props("flat dense color=negative")

                note = _PLUGIN_SETUP_NOTES.get(source.plugin, "")
                if note:
                    with ui.expansion("Setup guide", icon="info").props("dense").classes("w-full text-xs text-gray-400 border border-gray-700 rounded"):
                        ui.markdown(note).classes("text-sm text-gray-300 p-2")

                # Standard fields
                with ui.row().classes("w-full gap-3 items-center flex-wrap"):
                    inp["name"] = (
                        ui.input("Name", value=source.name).props("dense").classes("w-36")
                        .tooltip("Internal identifier — used in CLI commands: bean-sync ingest <name>")
                    )
                    inp["source_dir"] = (
                        ui.input("Source dir", value=str(source.source_dir)).props("dense").classes("flex-1 min-w-48")
                        .tooltip("Folder where raw files and parsed .bean files are stored. Created automatically on first ingest.")
                    )
                    inp["nullable"] = (
                        ui.checkbox("Nullable", value=source.nullable)
                        .tooltip(
                            "Allow the LLM to return null for files that are not transactions "
                            "(e.g. marketing emails, payment confirmations). "
                            "Disable only if every file in this source must produce a transaction."
                        )
                    )
                    inp["enrichment"] = (
                        ui.checkbox("Enrichment only", value=source.enrichment)
                        .tooltip(
                            "Enrichment sources provide context to the LLM when parsing other sources "
                            "but are NOT included in main.bean. Use this for inbox receipt scanning "
                            "or any source whose parsed transactions should not appear in your ledger directly."
                        )
                    )

                # Plugin-specific fields: inline (str/int/secret/bool) then block (list_str/secrets_dict)
                plugin_fields = [f for f in _dc.fields(source) if f.name not in _STANDARD_FIELDS]
                inline_fields = [(f, _ftype(f, hints)) for f in plugin_fields if _ftype(f, hints) in ("str", "int", "bool", "secret")]
                block_fields  = [(f, _ftype(f, hints)) for f in plugin_fields if _ftype(f, hints) in ("list_str", "secrets_dict")]

                if inline_fields:
                    with ui.row().classes("w-full gap-3 items-center flex-wrap"):
                        for f, ft in inline_fields:
                            label = _field_label(f)
                            current = getattr(source, f.name)
                            if ft == "secret":
                                secret_val = _secret_name(current)
                                inp[f.name] = (
                                    ui.select(known_secrets,
                                              value=secret_val if secret_val in known_secrets else None,
                                              label=label)
                                    .props("dense clearable")
                                    .classes("w-48")
                                )
                            elif ft == "int":
                                inp[f.name] = ui.number(label, value=current, min=1, step=1).props("dense").classes("w-24")
                            elif ft == "bool":
                                inp[f.name] = ui.checkbox(label, value=current)
                            else:
                                inp[f.name] = ui.input(label, value=str(current) if current else "").props("dense").classes("flex-1 min-w-48")

                for f, ft in block_fields:
                    label = _field_label(f)
                    current = getattr(source, f.name)
                    if ft == "list_str":
                        lines = "\n".join(current) if current else ""
                        with ui.column().classes("w-full gap-0"):
                            ui.label(label).classes("text-xs text-gray-400")
                            inp[f.name] = (
                                ui.textarea(value=lines)
                                .props("dense rows=2 outlined")
                                .classes("w-full font-mono text-sm")
                            )
                    elif ft == "secrets_dict":
                        lines = "\n".join(
                            f"{k}={v.name if isinstance(v, SecretRef) else v}"
                            for k, v in current.items()
                        )
                        with ui.column().classes("w-full gap-0"):
                            ui.label(label).classes("text-xs text-gray-400")
                            inp[f.name] = (
                                ui.textarea(value=lines)
                                .props("dense rows=3 outlined")
                                .classes("w-full font-mono text-sm")
                            )

                # Hint always last
                with ui.column().classes("w-full gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Hint (LLM prompt)").classes("text-xs text-gray-400")
                        ui.icon("help_outline").classes("text-xs text-gray-500").tooltip(
                            "Plain-English instructions for the AI parser. "
                            "Describe the institution and what kind of emails/receipts to expect, "
                            "which beancount accounts to use for assets and liabilities, "
                            "and when to return null. The more specific, the better."
                        )
                    inp["hint"] = (
                        ui.textarea(value=source.hint)
                        .props("dense rows=4 outlined")
                        .classes("w-full font-mono text-sm")
                    )

        def _switch_to_yaml() -> None:
            _capture()
            if yaml_editor:
                yaml_editor[0].value = _config_to_yaml(config_ref[0])

        def _switch_to_gui() -> None:
            if yaml_editor:
                try:
                    config_ref[0] = _yaml_to_config(yaml_editor[0].value)
                    _render_gui()
                except Exception as e:
                    ui.notify(f"YAML parse error: {e}", type="negative")

        def _save() -> None:
            if current_tab[0] == "gui":
                _capture()
            elif yaml_editor:
                try:
                    config_ref[0] = _yaml_to_config(yaml_editor[0].value)
                except Exception as e:
                    ui.notify(f"YAML parse error: {e}", type="negative")
                    return
            try:
                save_config(config_ref[0])
                write_primary_includes(config_ref[0].sources)
                ui.notify("Saved config.yaml", type="positive")
            except Exception as e:
                ui.notify(f"Error saving: {e}", type="negative")

        # Tab strip
        with ui.row().classes("gap-0 border-b border-gray-700 w-full"):
            gui_btn = ui.button("GUI Editor").props("flat no-caps").classes("rounded-none border-b-2 border-green-400")
            yaml_btn = ui.button("YAML Editor").props("flat no-caps").classes("rounded-none border-b-2 border-transparent")

        gui_panel = ui.column().classes("w-full gap-4")
        yaml_panel = ui.column().classes("w-full gap-4")
        yaml_panel.set_visibility(False)

        with gui_panel:
            with ui.expansion("Secrets", icon="key").classes("w-full border border-gray-700 rounded"):
                sc = ui.column().classes("w-full gap-2 p-2")
                secrets_container.append(sc)

            with ui.expansion("Git Repository", icon="commit").classes("w-full border border-gray-700 rounded"):
                gc = ui.column().classes("w-full gap-2 p-2")
                git_container.append(gc)

            col = ui.column().classes("w-full gap-3")
            gui_container.append(col)

        with yaml_panel:
            editor = ui.codemirror(value="", language="YAML", theme="dracula").classes("w-full").style("height: 600px; font-size: 13px;")
            yaml_editor.append(editor)

        def _to_gui() -> None:
            _switch_to_gui()
            current_tab[0] = "gui"
            gui_panel.set_visibility(True)
            yaml_panel.set_visibility(False)
            gui_btn.props("flat no-caps").classes("border-b-2 border-green-400")
            yaml_btn.props("flat no-caps").classes("border-b-2 border-transparent")

        def _to_yaml() -> None:
            _switch_to_yaml()
            current_tab[0] = "yaml"
            gui_panel.set_visibility(False)
            yaml_panel.set_visibility(True)
            yaml_btn.props("flat no-caps").classes("border-b-2 border-green-400")
            gui_btn.props("flat no-caps").classes("border-b-2 border-transparent")

        gui_btn.on_click(_to_gui)
        yaml_btn.on_click(_to_yaml)

        with ui.row().classes("w-full justify-end mt-2"):
            ui.button("Save", icon="save", on_click=_save).props("color=primary")

        _render_secrets()
        _render_git()
        _render_gui()
