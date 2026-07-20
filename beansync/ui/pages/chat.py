from __future__ import annotations

import datetime
import uuid
from collections.abc import Callable

import litellm
from nicegui import app as nicegui_app, ui

from beansync.agent_tools import build_system_prompt, llm_loop_stream
from beansync.config import MODEL


def _derive_title(session_messages: list[dict]) -> str:
    for m in session_messages:
        if m.get("role") == "user" and m.get("content"):
            text = " ".join(m["content"].split())
            return text[:40] + ("…" if len(text) > 40 else "")
    return "New chat"


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def chat_panel(
    set_date_from: Callable[[str], None],
    set_date_to: Callable[[str], None],
    set_accounts: Callable[[list[str]], None],
    refresh_all: Callable[[], None],
    close: Callable[[], None] | None = None,
) -> None:
    """Render the chat panel. Call inside a NiceGUI container (e.g. right_drawer)."""
    system_prompt = build_system_prompt()
    stored = nicegui_app.storage.general

    sessions: dict = stored.get("chat_sessions")
    if sessions is None:
        sessions = {}
        legacy = stored.get("chat_messages")
        if legacy:
            legacy_id = str(uuid.uuid4())
            now = _now_iso()
            sessions[legacy_id] = {
                "id": legacy_id,
                "title": _derive_title(legacy),
                "created": now,
                "updated": now,
                "messages": legacy,
            }
            stored["chat_current_session_id"] = legacy_id
            del stored["chat_messages"]
        stored["chat_sessions"] = sessions

    def _make_session() -> dict:
        return {
            "id": str(uuid.uuid4()),
            "title": "New chat",
            "created": _now_iso(),
            "updated": _now_iso(),
            "messages": [{"role": "system", "content": system_prompt}],
        }

    current_id = stored.get("chat_current_session_id")
    if current_id not in sessions:
        session = _make_session()
        current_id = session["id"]
        sessions[current_id] = session
        stored["chat_current_session_id"] = current_id

    sessions[current_id]["messages"][0] = {"role": "system", "content": system_prompt}
    messages: list[dict] = sessions[current_id]["messages"]

    ui.add_css("""
        .nicegui-markdown h1 { font-size: 1.1em !important; font-weight: 600; margin: 0.4em 0; }
        .nicegui-markdown h2 { font-size: 1.05em !important; font-weight: 600; margin: 0.3em 0; }
        .nicegui-markdown h3, .nicegui-markdown h4,
        .nicegui-markdown h5, .nicegui-markdown h6 {
            font-size: 1em !important; font-weight: 600; margin: 0.25em 0;
        }
    """)

    with ui.row().classes("w-full items-center pb-2 border-b border-gray-700"):
        ui.button(icon="add_comment", on_click=lambda: _start_new_session()).props(
            "flat dense color=white"
        ).tooltip("New chat")
        ui.button(icon="history", on_click=lambda: _open_history()).props(
            "flat dense color=white"
        ).tooltip("Chat history")
        ui.label("Finance Assistant").classes("text-lg font-semibold flex-1")
        token_label = ui.label("").classes("text-xs text-gray-400")
        if close is not None:
            ui.button(icon="close", on_click=close).props("flat dense color=white")

    with ui.dialog() as history_dialog, ui.card().classes("w-96 max-w-full"):
        ui.label("Chat history").classes("text-lg font-semibold")
        history_list = ui.column().classes("w-full gap-1 max-h-96 overflow-auto")

    with ui.scroll_area().classes("w-full flex-1").style("height: calc(480px - 110px)") as scroll:
        msg_col = ui.column().classes("w-full gap-3 p-2")

    with ui.row().classes("w-full gap-2 pt-2"):
        user_input = ui.input(placeholder="Ask me anything...").classes("flex-1").props("dense autofocus")
        send_btn = ui.button(icon="send").props("flat dense color=primary")

    def _update_token_count() -> None:
        try:
            n = litellm.token_counter(model=MODEL, messages=messages)
            token_label.set_text(f"{n:,} tok")
        except Exception:
            token_label.set_text("")

    def _render_messages() -> None:
        msg_col.clear()
        with msg_col:
            for m in messages[1:]:
                if m["role"] == "user":
                    ui.chat_message(text=m["content"], name="You", sent=True).classes("w-full")
                elif m["role"] == "assistant":
                    with ui.chat_message(name="Assistant").classes("w-full"):
                        ui.markdown(m["content"] or "")
        scroll.scroll_to(percent=1.0)

    def _select_from_history(sid: str) -> None:
        _switch_session(sid)
        history_dialog.close()

    def _open_history() -> None:
        history_list.clear()
        with history_list:
            for sid, sess in sorted(sessions.items(), key=lambda kv: kv[1].get("updated", ""), reverse=True):
                is_current = sid == current_id
                ui.button(
                    sess.get("title") or "New chat",
                    on_click=lambda _, sid=sid: _select_from_history(sid),
                ).props("flat dense align=left" + (" color=primary" if is_current else "")).classes(
                    "w-full justify-start"
                )
        history_dialog.open()

    def _start_new_session() -> None:
        nonlocal current_id, messages
        if not any(m["role"] == "user" for m in messages):
            return  # current session is already empty, reuse it
        session = _make_session()
        current_id = session["id"]
        sessions[current_id] = session
        stored["chat_current_session_id"] = current_id
        messages = session["messages"]
        _render_messages()
        _update_token_count()

    def _switch_session(sid: str) -> None:
        nonlocal current_id, messages
        if sid == current_id:
            return
        current_id = sid
        stored["chat_current_session_id"] = sid
        sessions[sid]["messages"][0] = {"role": "system", "content": system_prompt}
        messages = sessions[sid]["messages"]
        _render_messages()
        _update_token_count()

    _render_messages()
    _update_token_count()

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
        session = sessions[current_id]
        if session["title"] == "New chat":
            session["title"] = _derive_title(messages)
        session["updated"] = _now_iso()
        _update_token_count()

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
            async for kind, content in llm_loop_stream(messages, pending_actions):
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
        sessions[current_id]["updated"] = _now_iso()
        _update_token_count()
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
