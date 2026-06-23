from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nicegui import ui

from beancountio.llm import Posting, Transaction, transaction_to_beancount


def _ledger_accounts() -> list[str]:
    from beancount import loader
    from beancount.core import data as bdata
    from beancountio.config import LEDGER

    entries, _errors, _options = loader.load_file(str(LEDGER))
    accounts = sorted({
        e.account for e in entries if isinstance(e, bdata.Open)
    })
    return accounts


def _ledger_payees() -> list[str]:
    from beancount import loader
    from beancount.core import data as bdata
    from beancountio.config import LEDGER

    entries, _errors, _options = loader.load_file(str(LEDGER))
    payees = sorted({
        e.payee for e in entries
        if isinstance(e, bdata.Transaction) and e.payee
    })
    return payees


def _parse_entry(filepath: Path, lineno: int) -> tuple[Transaction | None, str]:
    """Parse a single transaction from filepath at the given 1-indexed lineno.

    Returns (Transaction, raw_source_str). raw_source_str is empty if not a sidecar.
    """
    from beancount import loader
    from beancount.core import data as bdata

    raw_lines = filepath.read_text().splitlines()
    reasoning = ""
    idx = lineno - 1  # 0-indexed
    if idx > 0 and raw_lines[idx - 1].strip().startswith(";"):
        reasoning = raw_lines[idx - 1].strip().lstrip(";").strip()

    entries, _errors, _options = loader.load_file(str(filepath))
    for entry in entries:
        if not isinstance(entry, bdata.Transaction):
            continue
        if entry.meta.get("lineno") != lineno:
            continue
        raw_source = entry.meta.get("source", "")
        certainty = int(entry.meta.get("certainty", 100))
        postings = []
        for p in entry.postings:
            if p.units is None:
                continue
            num = p.units.number
            amount_str = str(abs(num))
            if num < 0:
                amount_str = f"-{amount_str}"
            postings.append(Posting(account=p.account, amount=amount_str, currency=p.units.currency))
        return Transaction(
            reasoning=reasoning,
            date=str(entry.date),
            payee=entry.payee or "",
            narration=entry.narration or "",
            certainty=certainty,
            postings=postings,
        ), raw_source
    return None, ""



def _replace_in_file(filepath: Path, lineno: int, new_text: str) -> None:
    """Replace the transaction at lineno (1-indexed) in filepath with new_text."""
    lines = filepath.read_text().splitlines(keepends=True)
    tx_idx = lineno - 1  # 0-indexed

    # Walk back to include preceding ; comment
    start = tx_idx
    while start > 0 and lines[start - 1].strip().startswith(";"):
        start -= 1

    # Walk forward to include all indented continuation lines
    end = tx_idx + 1
    while end < len(lines) and (lines[end].startswith("  ") or lines[end].startswith("\t")):
        end += 1

    filepath.write_text("".join(lines[:start]) + new_text + "\n" + "".join(lines[end:]))


def transaction_editor_dialog(
    raw_source: str,
    file_path: str,
    lineno: int,
    on_save: Callable | None = None,
) -> None:
    """Open a dialog to edit a transaction and save it back.

    If raw_source is set, edits the corresponding sidecar (.bean alongside the raw file)
    and reassembles. Otherwise edits file_path in-place at lineno.
    """
    # Determine the actual file to parse from
    if raw_source:
        sidecar = Path(raw_source).with_suffix(".bean")
        parse_path = sidecar
        # Sidecar files always have the transaction at a fixed position; find lineno within them
        sidecar_lineno = _sidecar_tx_lineno(sidecar)
    else:
        sidecar = None
        parse_path = Path(file_path)
        sidecar_lineno = None

    effective_lineno = sidecar_lineno if sidecar_lineno is not None else lineno
    tx, _ = _parse_entry(parse_path, effective_lineno)

    if tx is None:
        ui.notify("Could not parse transaction", type="negative")
        return

    source_for_save = Path(raw_source) if raw_source else None
    edit_path = sidecar if sidecar else Path(file_path)
    edit_lineno = sidecar_lineno if sidecar_lineno is not None else lineno

    accounts = _ledger_accounts()
    payees = _ledger_payees()

    posting_state: list[dict] = [
        {"account": p.account, "amount": p.amount, "currency": p.currency}
        for p in tx.postings
    ]
    posting_inputs: list[tuple[ui.input, ui.input, ui.input]] = []

    with ui.dialog() as dialog, ui.card().classes("w-[720px] max-w-full gap-3"):
        ui.label("Edit Transaction").classes("text-xl font-bold")

        with ui.row().classes("w-full gap-3 items-start"):
            date_in = ui.input("Date", value=tx.date).props("type=date").classes("w-36")
            payee_in = ui.input("Payee", value=tx.payee, autocomplete=payees).classes("flex-1")
            narration_in = ui.input("Narration", value=tx.narration).classes("flex-1")

        with ui.row().classes("w-full gap-3 items-start"):
            certainty_in = ui.number("Certainty %", value=tx.certainty, min=0, max=100, step=1).classes("w-32")
            reasoning_in = ui.input("Reasoning", value=tx.reasoning).classes("flex-1")

        ui.separator()
        ui.label("Postings").classes("text-sm font-semibold")
        postings_col = ui.column().classes("w-full gap-1")

        def render_postings() -> None:
            _capture()
            postings_col.clear()
            posting_inputs.clear()
            with postings_col:
                for i, p in enumerate(posting_state):
                    with ui.row().classes("w-full gap-2 items-center"):
                        acct = ui.input(value=p["account"], autocomplete=accounts).props("placeholder=Account dense").classes("flex-1")
                        amt = ui.input(value=p["amount"]).props("placeholder=Amount dense").classes("w-28")
                        cur = ui.input(value=p["currency"]).props("placeholder=Currency dense").classes("w-16")
                        posting_inputs.append((acct, amt, cur))

                        def _remove(idx=i) -> None:
                            _capture()
                            del posting_state[idx]
                            render_postings()

                        ui.button(icon="delete", on_click=_remove).props("flat dense color=negative")

        def _capture() -> None:
            for j, (a, m, c) in enumerate(posting_inputs):
                if j < len(posting_state):
                    posting_state[j].update(account=a.value, amount=m.value, currency=c.value)

        def _add() -> None:
            _capture()
            posting_state.append({"account": "", "amount": "", "currency": "CAD"})
            render_postings()

        render_postings()
        ui.button("+ Add Posting", on_click=_add).props("flat dense").classes("text-sm text-blue-400")
        ui.separator()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            def _save() -> None:
                _capture()
                try:
                    new_tx = Transaction(
                        reasoning=reasoning_in.value.strip(),
                        date=date_in.value,
                        payee=payee_in.value.strip(),
                        narration=narration_in.value.strip(),
                        certainty=int(certainty_in.value or 100),
                        postings=[
                            Posting(account=p["account"], amount=p["amount"], currency=p["currency"])
                            for p in posting_state
                            if p["account"] and p["amount"]
                        ],
                    )
                except Exception as e:
                    ui.notify(f"Validation error: {e}", type="negative")
                    return

                use_source = source_for_save if source_for_save else edit_path.with_suffix("")
                new_text = transaction_to_beancount(new_tx, use_source)

                if sidecar:
                    edit_path.write_text(new_text + "\n")
                else:
                    _replace_in_file(edit_path, edit_lineno, new_text)

                ui.notify("Saved", type="positive")
                dialog.close()
                if on_save:
                    on_save()

            ui.button("Save", on_click=_save).props("color=primary")

    dialog.open()


def new_transaction_dialog(on_save: Callable | None = None) -> None:
    """Open a dialog to create a new transaction and append it to general.bean."""
    import datetime

    from beancountio.config import LEDGER

    general_bean = LEDGER.parent / "general.bean"
    accounts = _ledger_accounts()
    payees = _ledger_payees()

    posting_state: list[dict] = [
        {"account": "", "amount": "", "currency": "CAD"},
        {"account": "", "amount": "", "currency": "CAD"},
    ]
    posting_inputs: list[tuple[ui.input, ui.input, ui.input]] = []

    with ui.dialog() as dialog, ui.card().classes("w-[720px] max-w-full gap-3"):
        ui.label("New Transaction").classes("text-xl font-bold")

        with ui.row().classes("w-full gap-3 items-start"):
            date_in = ui.input("Date", value=str(datetime.date.today())).props("type=date").classes("w-36")
            payee_in = ui.input("Payee", autocomplete=payees).classes("flex-1")
            narration_in = ui.input("Narration").classes("flex-1")

        with ui.row().classes("w-full gap-3 items-start"):
            certainty_in = ui.number("Certainty %", value=100, min=0, max=100, step=1).classes("w-32")
            reasoning_in = ui.input("Reasoning").classes("flex-1")

        ui.separator()
        ui.label("Postings").classes("text-sm font-semibold")
        postings_col = ui.column().classes("w-full gap-1")

        def render_postings() -> None:
            _capture()
            postings_col.clear()
            posting_inputs.clear()
            with postings_col:
                for i, p in enumerate(posting_state):
                    with ui.row().classes("w-full gap-2 items-center"):
                        acct = ui.input(value=p["account"], autocomplete=accounts).props("placeholder=Account dense").classes("flex-1")
                        amt = ui.input(value=p["amount"]).props("placeholder=Amount dense").classes("w-28")
                        cur = ui.input(value=p["currency"]).props("placeholder=Currency dense").classes("w-16")
                        posting_inputs.append((acct, amt, cur))

                        def _remove(idx=i) -> None:
                            _capture()
                            del posting_state[idx]
                            render_postings()

                        ui.button(icon="delete", on_click=_remove).props("flat dense color=negative")

        def _capture() -> None:
            for j, (a, m, c) in enumerate(posting_inputs):
                if j < len(posting_state):
                    posting_state[j].update(account=a.value, amount=m.value, currency=c.value)

        def _add() -> None:
            _capture()
            posting_state.append({"account": "", "amount": "", "currency": "CAD"})
            render_postings()

        render_postings()
        ui.button("+ Add Posting", on_click=_add).props("flat dense").classes("text-sm text-blue-400")
        ui.separator()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            def _save() -> None:
                _capture()
                try:
                    tx = Transaction(
                        reasoning=reasoning_in.value.strip(),
                        date=date_in.value,
                        payee=payee_in.value.strip(),
                        narration=narration_in.value.strip(),
                        certainty=int(certainty_in.value or 100),
                        postings=[
                            Posting(account=p["account"], amount=p["amount"], currency=p["currency"])
                            for p in posting_state
                            if p["account"] and p["amount"]
                        ],
                    )
                except Exception as e:
                    ui.notify(f"Validation error: {e}", type="negative")
                    return

                new_text = transaction_to_beancount(tx, general_bean)
                existing = general_bean.read_text() if general_bean.exists() else ""
                existing = existing.rstrip("\n")
                prefix = existing + "\n\n" if existing else ""
                general_bean.write_text(prefix + new_text + "\n")

                ui.notify("Saved", type="positive")
                dialog.close()
                if on_save:
                    on_save()

            ui.button("Save", on_click=_save).props("color=primary")

    dialog.open()


def _sidecar_tx_lineno(sidecar: Path) -> int | None:
    """Return the 1-indexed line number of the transaction directive in a sidecar file."""
    if not sidecar.exists():
        return None
    for i, line in enumerate(sidecar.read_text().splitlines(), start=1):
        stripped = line.strip()
        if stripped and not stripped.startswith(";"):
            return i
    return None
