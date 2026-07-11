from __future__ import annotations

import asyncio
import datetime
import json
from collections.abc import AsyncGenerator, Callable
from pathlib import Path

import litellm
from nicegui import app as nicegui_app, ui

from beansync.config import LEDGER, MODEL, load_accounts
from beansync.llm import (
    Posting,
    Transaction,
    query_ledger,
    tavily_search,
    transaction_to_beancount,
)
from beansync.notes import delete_note, save_note
from beansync.ui.transaction_editor import _replace_in_file


_SYSTEM_PROMPT_TEMPLATE = """\
You are a personal finance assistant for Alex's beancount ledger.
Today's date is {date}.

You can query the ledger, create and edit transactions, and update the dashboard view.

When creating or editing transactions postings must balance to zero.
Expenses are positive, the paying account (liability or asset) is negative.
Operating currency is CAD.

Use query_ledger to look up existing transactions before editing them — \
you need the filename and lineno from entry_meta('filename') and entry_meta('lineno').

Do not use emoji in your responses.

Accounts:
{accounts}"""


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_ledger",
            "description": (
                "Run a BQL query against the beancount ledger. "
                "Example: SELECT date, payee, narration, entry_meta('filename'), entry_meta('lineno') "
                "WHERE payee ~ 'Steam' ORDER BY date DESC LIMIT 5"
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_transaction",
            "description": "Create a new transaction appended to general.bean.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "payee": {"type": "string"},
                    "narration": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "postings": {
                        "type": "array",
                        "description": "Must balance to zero. Expenses +, paying account -.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "account": {"type": "string"},
                                "amount": {"type": "string", "description": "e.g. '57.49' or '-57.49'"},
                                "currency": {"type": "string", "default": "CAD"},
                            },
                            "required": ["account", "amount"],
                        },
                    },
                },
                "required": ["date", "payee", "narration", "postings"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_transaction",
            "description": (
                "Replace an existing transaction in-place. "
                "Use query_ledger with entry_meta('filename') and entry_meta('lineno') first to locate it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filename": {"type": "string", "description": "Absolute path to the .bean file"},
                    "lineno": {"type": "integer", "description": "1-indexed transaction line number"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "payee": {"type": "string"},
                    "narration": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "postings": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "account": {"type": "string"},
                                "amount": {"type": "string"},
                                "currency": {"type": "string", "default": "CAD"},
                            },
                            "required": ["account", "amount"],
                        },
                    },
                },
                "required": ["filename", "lineno", "date", "payee", "narration", "postings"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_filter",
            "description": "Update the dashboard date range and/or account filter. Omit a field to leave it unchanged.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                    "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                    "accounts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Account names to filter to; empty list clears the filter.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sources",
            "description": "List all configured sources with plugin type, enrichment flag, and parsed/total file counts.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_source_files",
            "description": "List raw files in a source directory with their parse status (parsed/null/UNPROCESSED). Use to pick a file for test_parse.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_name": {"type": "string"},
                    "limit": {"type": "integer", "description": "Max files to return, default 20", "default": 20},
                },
                "required": ["source_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_ingest",
            "description": "Fetch new data and parse it for the specified sources (or all sources if omitted).",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_names": {"type": "array", "items": {"type": "string"}, "description": "Sources to ingest; omit for all"},
                    "since": {"type": "string", "description": "Only fetch from this date onwards (YYYY-MM-DD)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "test_parse",
            "description": "Dry-run the LLM parser on a single file using the source's current config. Returns the beancount output in chat; no sidecar is written. Use after editing a source to verify the hint works correctly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_name": {"type": "string"},
                    "file_path": {"type": "string", "description": "Absolute or relative path to the raw .html/.txt/image file"},
                },
                "required": ["source_name", "file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_config",
            "description": "Read the current config.yaml. Use before save_config to see the existing sources.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_config",
            "description": "Write a new config.yaml, replacing the current one. Use get_config first, modify the YAML (add/edit/remove sources), then call this. Validates YAML before saving.",
            "parameters": {
                "type": "object",
                "properties": {
                    "yaml_text": {"type": "string", "description": "Complete config.yaml contents"},
                },
                "required": ["yaml_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "tavily_search",
            "description": "Web search to look up a merchant or any external information.",
            "parameters": {
                "type": "object",
                "properties": {"merchant": {"type": "string"}},
                "required": ["merchant"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": "Persist a note about a merchant for future ingestion runs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Regex pattern matching the merchant in source text"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Delete a persistent note by exact key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
    },
]


async def _llm_loop_stream(
    messages: list[dict], pending_actions: list[dict]
) -> AsyncGenerator[tuple[str, str], None]:
    """Async streaming LLM conversation loop.

    Yields (kind, content) where kind is 'text' for response chunks or
    'status' for progress updates ('Thinking…', 'Calling foo…', etc.).
    """

    def _create_transaction(
        date: str,
        payee: str,
        narration: str,
        postings: list[dict],
        reasoning: str = "",
    ) -> str:
        general_bean = LEDGER.parent / "general.bean"
        tx = Transaction(
            reasoning=reasoning,
            date=date,
            payee=payee,
            narration=narration,
            postings=[
                Posting(account=p["account"], amount=p["amount"], currency=p.get("currency", "CAD"))
                for p in postings
            ],
        )
        new_text = transaction_to_beancount(tx, general_bean)
        existing = general_bean.read_text() if general_bean.exists() else ""
        existing = existing.rstrip("\n")
        prefix = existing + "\n\n" if existing else ""
        general_bean.write_text(prefix + new_text + "\n")
        pending_actions.append({"type": "refresh"})
        return f"Created: {date} {payee} — {narration}"

    def _edit_transaction(
        filename: str,
        lineno: int,
        date: str,
        payee: str,
        narration: str,
        postings: list[dict],
        reasoning: str = "",
    ) -> str:
        filepath = Path(filename)
        if not filepath.exists():
            return f"File not found: {filename}"
        for suffix in (".html", ".txt"):
            candidate = filepath.with_suffix(suffix)
            if candidate.exists():
                source_path = candidate
                break
        else:
            source_path = filepath.with_suffix("")
        tx = Transaction(
            reasoning=reasoning,
            date=date,
            payee=payee,
            narration=narration,
            postings=[
                Posting(account=p["account"], amount=p["amount"], currency=p.get("currency", "CAD"))
                for p in postings
            ],
        )
        new_text = transaction_to_beancount(tx, source_path)
        _replace_in_file(filepath, lineno, new_text)
        pending_actions.append({"type": "refresh"})
        return f"Updated {filepath.name}:{lineno}"

    def _set_filter(
        date_from: str | None = None,
        date_to: str | None = None,
        accounts: list[str] | None = None,
    ) -> str:
        pending_actions.append({"type": "set_filter", "date_from": date_from, "date_to": date_to, "accounts": accounts})
        parts = []
        if date_from:
            parts.append(f"from {date_from}")
        if date_to:
            parts.append(f"to {date_to}")
        if accounts is not None:
            parts.append(f"accounts: {', '.join(accounts) if accounts else '(all)'}")
        return "Filter updated: " + (", ".join(parts) or "no changes")

    def _list_sources() -> str:
        from beansync.config import load_sources
        sources = load_sources()
        if not sources:
            return "No sources configured."
        lines = []
        for s in sources:
            sdir = s.source_dir
            if sdir.exists():
                raw_exts = {".html", ".txt", ".jpg", ".jpeg", ".png", ".webp"}
                raw = [f for f in sdir.rglob("*") if f.suffix.lower() in raw_exts and f.is_file()]
                parsed = sum(1 for f in raw if f.with_suffix(".bean").exists())
                file_info = f"{parsed}/{len(raw)} parsed"
            else:
                file_info = "directory not created yet"
            enrichment = " [enrichment]" if getattr(s, "enrichment", False) else ""
            lines.append(f"- {s.name} ({s.plugin}){enrichment}: {file_info}, dir={sdir}")
        return "\n".join(lines)

    def _list_source_files(source_name: str, limit: int = 20) -> str:
        from beansync.config import load_sources
        sources = {s.name: s for s in load_sources()}
        if source_name not in sources:
            return f"Unknown source: {source_name!r}. Available: {', '.join(sources)}"
        sdir = sources[source_name].source_dir
        if not sdir.exists():
            return f"Source directory {sdir} does not exist yet."
        raw_exts = {".html", ".txt", ".jpg", ".jpeg", ".png", ".webp"}
        raw_files = sorted(
            (f for f in sdir.rglob("*") if f.suffix.lower() in raw_exts and f.is_file()),
            key=lambda p: p.name,
            reverse=True,
        )[:max(1, min(limit, 50))]
        if not raw_files:
            return f"No raw files in {sdir}"
        lines = [f"Files in '{source_name}' ({sdir}), newest first:"]
        for f in raw_files:
            bean = f.with_suffix(".bean")
            if bean.exists() and bean.stat().st_size > 0:
                status = "parsed"
            elif bean.exists():
                status = "null"
            else:
                status = "UNPROCESSED"
            lines.append(f"  {f.relative_to(sdir)}: {status}")
        return "\n".join(lines)

    def _run_ingest(source_names: list[str] | None = None, since: str | None = None) -> str:
        import subprocess
        cmd = ["bean-sync", "ingest"]
        if source_names:
            cmd.extend(source_names)
        if since:
            cmd.extend(["--since", since])
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        pending_actions.append({"type": "refresh"})
        return (result.stdout + result.stderr).strip() or f"Done (exit {result.returncode})."

    def _test_parse(source_name: str, file_path: str) -> str:
        from pathlib import Path as _Path
        from beansync.config import load_sources, load_accounts
        from beansync import llm

        p = _Path(file_path)
        if not p.exists():
            return f"File not found: {file_path}"
        sources = {s.name: s for s in load_sources()}
        if source_name not in sources:
            return f"Unknown source: {source_name!r}. Available: {', '.join(sources)}"
        source = sources[source_name]
        accounts = load_accounts()
        null_instr = llm.NULL_INSTRUCTION if source.nullable else llm.NO_NULL_INSTRUCTION
        enrichment_note = llm.ENRICHMENT_NOTE if source.enrichment else ""

        questions: list[str] = []

        def _mock_ask_user(question: str, options: list[str] | None = None) -> str:
            questions.append(question)
            return "Best guess"

        original = llm.TOOL_HANDLERS.get("ask_user")
        llm.TOOL_HANDLERS["ask_user"] = _mock_ask_user
        try:
            parse_mode = getattr(type(source), "parse_mode", "standard")
            if parse_mode == "image":
                prompt = llm.RECEIPT_SYSTEM_PROMPT_TEMPLATE.format(
                    hint=source.hint, accounts=accounts, null_instruction=null_instr, enrichment_note=enrichment_note
                )
                result = llm.parse_image(p, prompt, nullable=source.nullable, is_enrichment=source.enrichment)
            else:
                all_source_dirs = [s.source_dir for s in load_sources()]
                enrichment_dirs = [d for d in all_source_dirs if d != source.source_dir]
                prompt = llm.SYSTEM_PROMPT_TEMPLATE.format(
                    hint=source.hint, accounts=accounts, null_instruction=null_instr, enrichment_note=enrichment_note
                )
                result = llm.parse_source(p, prompt, enrichment_dirs=enrichment_dirs or None, nullable=source.nullable, is_enrichment=source.enrichment)
        finally:
            if original is not None:
                llm.TOOL_HANDLERS["ask_user"] = original
            else:
                llm.TOOL_HANDLERS.pop("ask_user", None)

        output = result if result is not None else "(null — no transaction)"
        if questions:
            output += f"\n\n⚠ LLM would have called ask_user: {'; '.join(questions)}"
        return output

    def _get_config() -> str:
        from beansync.config import CONFIG_FILE
        return CONFIG_FILE.read_text() if CONFIG_FILE.exists() else "config.yaml not found."

    def _save_config(yaml_text: str) -> str:
        import yaml as _yaml
        from beansync.config import (
            _Loader, Config, _PLUGIN_REGISTRY, _source_from_dict,
            save_config as _save, write_primary_includes,
        )
        try:
            raw = _yaml.load(yaml_text, Loader=_Loader)
        except _yaml.YAMLError as e:
            return f"YAML parse error: {e}"
        if isinstance(raw, Config):
            config = raw
        else:
            registered = tuple(_PLUGIN_REGISTRY.values())
            config = Config(sources=[
                s if isinstance(s, registered) else _source_from_dict(s)
                for s in raw.get("sources", [])
            ])
        _save(config)
        write_primary_includes(config.sources)
        pending_actions.append({"type": "refresh"})
        names = ", ".join(s.name for s in config.sources)
        return f"Saved config.yaml with {len(config.sources)} source(s): {names}"

    handlers: dict[str, Callable] = {
        "query_ledger": query_ledger,
        "create_transaction": _create_transaction,
        "edit_transaction": _edit_transaction,
        "set_filter": _set_filter,
        "tavily_search": tavily_search,
        "save_note": save_note,
        "delete_note": delete_note,
        "list_sources": _list_sources,
        "list_source_files": _list_source_files,
        "run_ingest": _run_ingest,
        "test_parse": _test_parse,
        "get_config": _get_config,
        "save_config": _save_config,
    }

    local_messages = list(messages)
    for _ in range(12):
        yield ("status", "Thinking…")
        stream = await litellm.acompletion(
            model=MODEL,
            messages=local_messages,
            tools=_TOOLS,
            stream=True,
            request_timeout=120,
            extra_body={"provider": {"data_collection": "deny", "sort": "price"}},
        )

        full_content = ""
        tool_calls_acc: dict[int, dict] = {}

        async for chunk in stream:
            delta = chunk.choices[0].delta

            if delta.content:
                full_content += delta.content
                yield ("text", delta.content)

            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls_acc:
                        tool_calls_acc[idx] = {
                            "id": tc.id or "",
                            "name": (tc.function.name or "") if tc.function else "",
                            "args": "",
                        }
                    if tc.function and tc.function.arguments:
                        tool_calls_acc[idx]["args"] += tc.function.arguments

        if not tool_calls_acc:
            return

        tool_calls_list = [
            {
                "id": info["id"],
                "type": "function",
                "function": {"name": info["name"], "arguments": info["args"]},
            }
            for _, info in sorted(tool_calls_acc.items())
        ]
        local_messages.append({
            "role": "assistant",
            "content": full_content or None,
            "tool_calls": tool_calls_list,
        })

        for _, info in sorted(tool_calls_acc.items()):
            yield ("status", f"Calling {info['name']}…")
            fn = handlers.get(info["name"])
            try:
                args = json.loads(info["args"])
                if fn:
                    result = await asyncio.to_thread(fn, **args)
                else:
                    result = f"Unknown tool: {info['name']}"
            except Exception as exc:
                result = f"Error: {exc}"
            local_messages.append({
                "role": "tool",
                "tool_call_id": info["id"],
                "content": str(result),
            })

    yield ("text", "I reached the maximum number of steps. Please try a simpler request.")


def chat_panel(
    set_date_from: Callable[[str], None],
    set_date_to: Callable[[str], None],
    set_accounts: Callable[[list[str]], None],
    refresh_all: Callable[[], None],
    close: Callable[[], None] | None = None,
) -> None:
    """Render the chat panel. Call inside a NiceGUI container (e.g. right_drawer)."""
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        date=datetime.date.today().isoformat(),
        accounts=load_accounts(),
    )
    stored = nicegui_app.storage.general
    if "chat_messages" not in stored:
        stored["chat_messages"] = [{"role": "system", "content": system_prompt}]
    else:
        stored["chat_messages"][0] = {"role": "system", "content": system_prompt}
    messages: list[dict] = stored["chat_messages"]

    ui.add_css("""
        .nicegui-markdown h1 { font-size: 1.1em !important; font-weight: 600; margin: 0.4em 0; }
        .nicegui-markdown h2 { font-size: 1.05em !important; font-weight: 600; margin: 0.3em 0; }
        .nicegui-markdown h3, .nicegui-markdown h4,
        .nicegui-markdown h5, .nicegui-markdown h6 {
            font-size: 1em !important; font-weight: 600; margin: 0.25em 0;
        }
    """)

    with ui.row().classes("w-full items-center pb-2 border-b border-gray-700"):
        ui.label("Finance Assistant").classes("text-lg font-semibold flex-1")
        if close is not None:
            ui.button(icon="close", on_click=close).props("flat dense color=white")

    with ui.scroll_area().classes("w-full flex-1").style("height: calc(480px - 110px)") as scroll:
        msg_col = ui.column().classes("w-full gap-3 p-2")

    with ui.row().classes("w-full gap-2 pt-2"):
        user_input = ui.input(placeholder="Ask me anything...").classes("flex-1").props("dense autofocus")
        send_btn = ui.button(icon="send").props("flat dense color=primary")

    async def _send() -> None:
        text = user_input.value.strip()
        if not text:
            return
        user_input.set_value("")
        send_btn.disable()

        with msg_col:
            ui.chat_message(text=text, name="You", sent=True).classes("w-full")
        scroll.scroll_to(percent=1.0)
        messages.append({"role": "user", "content": text})

        pending_actions: list[dict] = []
        full_reply = ""
        md_label = None

        with msg_col:
            with ui.chat_message(name="Assistant").classes("w-full") as assistant_msg:
                with ui.element("q-chip").props("loading outline color=grey-6") as status_chip:
                    status_label = ui.label("Thinking…").classes("text-sm")
        scroll.scroll_to(percent=1.0)

        def _ensure_bubble() -> None:
            nonlocal md_label
            if md_label is None:
                status_chip.delete()
                with assistant_msg:
                    md_label = ui.markdown("")

        try:
            async for kind, content in _llm_loop_stream(messages, pending_actions):
                if kind == "status":
                    if md_label is None:
                        status_label.set_text(content)
                else:
                    _ensure_bubble()
                    assert md_label is not None
                    full_reply += content
                    md_label.set_content(full_reply)
                    scroll.scroll_to(percent=1.0)
        except Exception as exc:
            _ensure_bubble()
            assert md_label is not None
            full_reply = f"Error: {exc}"
            md_label.set_content(full_reply)

        if md_label is None:
            assistant_msg.delete()

        messages.append({"role": "assistant", "content": full_reply})
        send_btn.enable()

        for action in pending_actions:
            if action["type"] == "set_filter":
                if action.get("date_from"):
                    set_date_from(action["date_from"])
                if action.get("date_to"):
                    set_date_to(action["date_to"])
                if action.get("accounts") is not None:
                    set_accounts(action["accounts"])
                refresh_all()
            elif action["type"] == "refresh":
                refresh_all()

    user_input.on("keydown.enter", _send)
    send_btn.on("click", _send)
