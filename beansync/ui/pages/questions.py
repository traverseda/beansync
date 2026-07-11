from __future__ import annotations

from pathlib import Path

from nicegui import ui

from beansync import questions as questions_store


def _reparse_file(q: dict) -> str:
    """Attempt an immediate targeted re-parse of the file a deferred question was about.

    Only possible for sources that fetch raw files to disk before parsing
    (image/html sources) — email-receipt sources haven't downloaded the
    message yet at defer time, so those just wait for the next ingest run
    to pick the saved answer up automatically.
    """
    from beansync import llm
    from beansync.config import load_accounts, load_sources

    raw_file = Path(q["source_file"])
    if not raw_file.exists():
        return "Saved — will be retried on the next ingest run."

    sidecar = raw_file.with_suffix(".bean")
    if sidecar.exists():
        return "Already resolved by a later ingest run."

    sources = {s.name: s for s in load_sources()}
    source = sources.get(q["source_name"])
    if source is None:
        return "Saved — source config not found for immediate retry, will be retried on the next ingest run."

    accounts = load_accounts()
    null_instr = llm.NULL_INSTRUCTION if source.nullable else llm.NO_NULL_INSTRUCTION
    extra_context = questions_store.answered_context_for(raw_file)

    token = llm.UNATTENDED.set(True)
    try:
        if raw_file.suffix.lower() in llm.IMAGE_SUFFIXES:
            prompt = llm.RECEIPT_SYSTEM_PROMPT_TEMPLATE.format(
                hint=source.hint, accounts=accounts, null_instruction=null_instr, enrichment_note="",
            )
            entry = llm.parse_image(raw_file, prompt, nullable=source.nullable, is_enrichment=False, extra_context=extra_context)
        else:
            all_source_dirs = [s.source_dir for s in sources.values()]
            enrichment_dirs = [d for d in all_source_dirs if d != source.source_dir]
            prompt = llm.SYSTEM_PROMPT_TEMPLATE.format(
                hint=source.hint, accounts=accounts, null_instruction=null_instr, enrichment_note="",
            )
            entry = llm.parse_source(raw_file, prompt, enrichment_dirs or None, nullable=source.nullable, is_enrichment=False, extra_context=extra_context)
    except questions_store.QuestionDeferred as exc:
        questions_store.queue_question(q["source_name"], raw_file, exc.question, exc.options)
        return "Still uncertain — the AI asked a follow-up question below."
    finally:
        llm.UNATTENDED.reset(token)

    questions_store.clear_answered_for(raw_file)
    sidecar.write_text(entry + "\n" if entry else "")
    return "Resolved — transaction saved." if entry else "Resolved — the AI decided this isn't a transaction."


def page() -> None:
    with ui.column().classes("w-full gap-4"):
        ui.label("Questions").classes("text-2xl font-bold")
        ui.label(
            "Questions the AI couldn't answer confidently during a scheduled/background ingest "
            "run, where ask_user has no terminal to block on. Only authoritative (non-enrichment) "
            "sources can raise these."
        ).classes("text-sm text-gray-500")

        container = ui.column().classes("w-full gap-3")

        def render() -> None:
            container.clear()
            items = questions_store.pending()
            with container:
                if not items:
                    ui.label("No pending questions.").classes("text-gray-400 italic")
                    return

                for q in items:
                    with ui.card().classes("w-full"):
                        with ui.row().classes("w-full items-center gap-2"):
                            ui.label(q["source_name"]).classes(
                                "text-xs font-mono bg-gray-700 text-gray-200 px-2 py-0.5 rounded"
                            )
                            ui.label(Path(q["source_file"]).name).classes("text-xs text-gray-500 flex-1 truncate")
                            ui.label(q["asked_at"]).classes("text-xs text-gray-500")
                        ui.label(q["question"]).classes("my-2")

                        answer_input = ui.input(label="Answer").classes("w-full")

                        if q.get("options"):
                            with ui.row().classes("w-full flex-wrap gap-2 mt-1"):
                                for opt in q["options"]:
                                    ui.button(
                                        opt, on_click=lambda _, o=opt, ai=answer_input: ai.set_value(o)
                                    ).props("outline dense size=sm")

                        def submit(q=q, answer_input=answer_input) -> None:
                            text = answer_input.value.strip()
                            if not text:
                                ui.notify("Enter an answer first.", type="warning")
                                return
                            questions_store.answer(q["id"], text)
                            status = _reparse_file(q)
                            ui.notify(status, type="positive")
                            render()

                        def dismiss(q=q) -> None:
                            questions_store.discard(q["id"])
                            render()

                        with ui.row().classes("w-full justify-end gap-2 mt-2"):
                            ui.button("Dismiss", on_click=dismiss).props("flat dense")
                            ui.button("Submit Answer", on_click=submit).props("color=primary dense")

        render()
