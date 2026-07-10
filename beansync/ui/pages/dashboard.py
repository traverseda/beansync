from __future__ import annotations

import csv
import datetime
import io
import subprocess
from collections import defaultdict
from decimal import Decimal

from nicegui import ui

from urllib.parse import quote

from beansync.config import LEDGER, load_sources
from beansync.ui.transaction_editor import new_transaction_dialog, transaction_editor_dialog


_PERIODS = {
    "Last Month": lambda: datetime.date.today() - datetime.timedelta(days=30),
    "Last 3 Months": lambda: datetime.date.today() - datetime.timedelta(days=90),
    "Last 6 Months": lambda: datetime.date.today() - datetime.timedelta(days=180),
    "Year to Date": lambda: datetime.date(datetime.date.today().year, 1, 1),
    "All Time": lambda: datetime.date(1970, 1, 1),
}


def _sankey_label(account: str) -> str:
    parts = account.split(":")
    if account.startswith("Liabilities:CreditCard:"):
        return parts[-1]
    elif account.startswith("Assets:"):
        return " ".join(parts[1:])
    # For deep accounts (3+ parts), show only the last segment to keep labels short
    if len(parts) >= 3:
        return parts[-1]
    return ": ".join(parts[1:])


def _expand_flows_with_hierarchy(
    flows: dict[tuple[str, str], Decimal],
) -> dict[tuple[str, str], Decimal]:
    """Route destination accounts with 3+ parts through intermediate parent nodes."""
    expanded: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for (src, dst), amt in flows.items():
        if amt <= 0:
            continue
        parts = dst.split(":")
        if len(parts) <= 2:
            expanded[(src, dst)] += amt
        else:
            # src → level-2 parent (e.g., src → Expenses:Food)
            parent = ":".join(parts[:2])
            expanded[(src, parent)] += amt
            # chain down: Expenses:Food → Expenses:Food:Restaurants → …
            for i in range(2, len(parts)):
                p = ":".join(parts[:i])
                c = ":".join(parts[:i + 1])
                expanded[(p, c)] += amt
    return expanded


def _get_accounts() -> list[str]:
    rows = _run_query("SELECT DISTINCT account ORDER BY account")
    return [row[0] for row in rows if row]


def _sankey_figure(
    since: datetime.date,
    until: datetime.date,
    account_filter: list[str],
):
    """Build a Plotly Sankey figure from raw beancount posting flows."""
    import plotly.graph_objects as go
    from beancount import loader
    from beancount.core import data as bdata

    entries, _errors, _options = loader.load_file(str(LEDGER))

    flows: dict[tuple[str, str], Decimal] = defaultdict(Decimal)

    for entry in entries:
        if not isinstance(entry, bdata.Transaction):
            continue
        if not (since <= entry.date <= until):
            continue
        if any(p.account == "Equity:Initial" for p in entry.postings):
            continue

        cad = [p for p in entry.postings if p.units is not None and p.units.currency == "CAD"]
        srcs = [p for p in cad if p.units.number < 0]
        dsts = [p for p in cad if p.units.number > 0]

        if not srcs or not dsts:
            continue

        total_out = sum(abs(p.units.number) for p in srcs)
        for src in srcs:
            share = abs(src.units.number) / total_out
            for dst in dsts:
                flows[(src.account, dst.account)] += dst.units.number * share

    if account_filter:
        acct_set = set(account_filter)
        flows = {k: v for k, v in flows.items() if k[0] in acct_set or k[1] in acct_set}

    flows = _expand_flows_with_hierarchy(flows)

    if not flows:
        return go.Figure()

    all_accounts = sorted({acc for pair in flows for acc in pair})
    node_idx = {acc: i for i, acc in enumerate(all_accounts)}
    labels = [_sankey_label(acc) for acc in all_accounts]

    link_src, link_dst, link_val = [], [], []
    for (src, dst), amt in flows.items():
        if amt > 0:
            link_src.append(node_idx[src])
            link_dst.append(node_idx[dst])
            link_val.append(round(float(amt), 2))

    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(
            label=labels,
            customdata=all_accounts,
            hovertemplate="%{customdata}<br>CAD %{value:,.2f}<extra></extra>",
            pad=15,
            thickness=20,
        ),
        link=dict(source=link_src, target=link_dst, value=link_val),
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0", size=12),
        margin=dict(l=5, r=5, t=5, b=5),
    )
    return fig


def _pie_figure(
    since: datetime.date,
    until: datetime.date,
    account_filter: list[str],
):
    """Pie chart of outgoing money by level-2 destination account."""
    import plotly.graph_objects as go
    from beancount import loader
    from beancount.core import data as bdata

    entries, _errors, _options = loader.load_file(str(LEDGER))

    totals: dict[str, Decimal] = defaultdict(Decimal)

    for entry in entries:
        if not isinstance(entry, bdata.Transaction):
            continue
        if not (since <= entry.date <= until):
            continue
        if any(p.account == "Equity:Initial" for p in entry.postings):
            continue

        cad = [p for p in entry.postings if p.units is not None and p.units.currency == "CAD"]
        dsts = [p for p in cad if p.units.number > 0]

        if not dsts:
            continue

        for dst in dsts:
            if not dst.account.startswith("Expenses:"):
                continue
            if account_filter and dst.account not in account_filter:
                continue
            parts = dst.account.split(":")
            # Group at level-2 (e.g., Expenses:Food)
            key = ":".join(parts[:2])
            totals[key] += dst.units.number

    if not totals:
        return go.Figure()

    labels = [_sankey_label(k) for k in totals]
    values = [round(float(v), 2) for v in totals.values()]

    fig = go.Figure(go.Pie(
        labels=labels,
        values=values,
        customdata=list(totals.keys()),
        hovertemplate="%{customdata}<br>CAD %{value:,.2f} (%{percent})<extra></extra>",
        texttemplate="%{label}<br>$%{value:,.0f}<br>%{percent}",
        textinfo="text",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0", size=12),
        margin=dict(l=5, r=5, t=30, b=5),
        showlegend=False,
    )
    return fig


def _group_transactions(
    rows: list[list[str]],
) -> list[tuple[str, str, str, str, str, str, bool, str, str, str]]:
    """Return one display row per posting.

    Columns: date, payee, narration, account, debit, credit, is_first_in_group,
             raw_source, filename, lineno.
    Metadata columns are only populated on the first row of each group.
    """
    groups: dict[tuple, list[tuple[str, str]]] = {}
    meta: dict[tuple, tuple[str, str, str]] = {}
    order: list[tuple] = []
    for row in rows:
        date, account, payee, narration, amount = row[0], row[1], row[2], row[3], row[4]
        raw_source = row[5] if len(row) > 5 else ""
        filename = row[6] if len(row) > 6 else ""
        lineno = row[7] if len(row) > 7 else ""
        key = (date, payee, narration)
        if key not in groups:
            groups[key] = []
            order.append(key)
            meta[key] = (raw_source, filename, lineno)
        groups[key].append((account, amount))

    result = []
    for key in order:
        date, payee, narration = key
        raw_source, filename, lineno = meta.get(key, ("", "", ""))
        for i, (account, amount) in enumerate(groups[key]):
            is_first = i == 0
            is_negative = amount.strip().startswith("-")
            debit = "" if is_negative else amount.strip()
            credit = amount.strip().lstrip("-") if is_negative else ""
            result.append((
                date if is_first else "",
                payee if is_first else "",
                narration if is_first else "",
                account,
                debit,
                credit,
                is_first,
                raw_source if is_first else "",
                filename if is_first else "",
                lineno if is_first else "",
            ))
    return result


def _run_query(bql: str) -> list[list[str]]:
    result = subprocess.run(
        ["bean-query", "-f", "csv", str(LEDGER), bql],
        capture_output=True, text=True, timeout=15,
    )
    output = result.stdout.strip()
    if not output or output == "(no results)":
        return []
    reader = csv.reader(io.StringIO(output))
    next(reader)  # skip header row
    return [row for row in reader if any(row)]


def _net_worth_figure(
    since: datetime.date,
    until: datetime.date,
    account_filter: list[str],
):
    """Line chart of net worth (CAD) over time within [since, until]."""
    import plotly.graph_objects as go
    from beancount import loader
    from beancount.core import data as bdata

    entries, _errors, _options = loader.load_file(str(LEDGER))

    balance: dict[str, Decimal] = defaultdict(Decimal)
    date_net: dict[datetime.date, Decimal] = {}

    for entry in entries:
        if not isinstance(entry, bdata.Transaction):
            continue
        if entry.date > until:
            break
        for posting in entry.postings:
            if posting.units is None or posting.units.currency != "CAD":
                continue
            if not posting.account.startswith(("Assets:", "Liabilities:")):
                continue
            if account_filter and posting.account not in account_filter:
                continue
            balance[posting.account] += posting.units.number
        date_net[entry.date] = sum(balance.values())

    all_dates = sorted(date_net.keys())
    points: list[tuple[datetime.date, Decimal]] = []

    # Seed with the net worth value just before the window opens
    pre = [d for d in all_dates if d < since]
    start_val = date_net[pre[-1]] if pre else Decimal(0)
    points.append((since, start_val))

    for d in all_dates:
        if since <= d <= until:
            points.append((d, date_net[d]))

    if not points:
        return go.Figure()

    xs = [str(p[0]) for p in points]
    ys = [float(p[1]) for p in points]

    final = ys[-1] if ys else 0
    positive = final >= 0
    line_color = "#4ade80" if positive else "#f87171"
    fill_color = "rgba(74,222,128,0.15)" if positive else "rgba(248,113,113,0.15)"

    fig = go.Figure(go.Scatter(
        x=xs,
        y=ys,
        mode="lines",
        line=dict(color=line_color, width=2),
        fill="tozeroy",
        fillcolor=fill_color,
        hovertemplate="<b>%{x}</b><br>CAD %{y:,.2f}<extra></extra>",
    ))
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e0e0e0", size=12),
        margin=dict(l=5, r=5, t=5, b=5),
        xaxis=dict(gridcolor="rgba(255,255,255,0.1)", showgrid=True),
        yaxis=dict(gridcolor="rgba(255,255,255,0.1)", showgrid=True, tickprefix="$"),
    )
    return fig


def page() -> None:
    import plotly.graph_objects as go

    with ui.column().classes("p-6 w-full gap-4"):
        with ui.row().classes("w-full items-center"):
            ui.label("Dashboard").classes("text-2xl font-bold flex-1")

        # Current balance — always reflects today, no period filter
        with ui.card().classes("w-full"):
            ui.label("Current Balance").classes("text-lg font-semibold mb-2")
            balance_rows = ui.column()

            def refresh_balances() -> None:
                balance_rows.clear()
                rows = _run_query(
                    "SELECT account, sum(position) AS balance "
                    "WHERE account ~ '^(Assets|Liabilities)' "
                    "GROUP BY account ORDER BY account"
                )
                with balance_rows:
                    if not rows:
                        ui.label("No data — run bean-query manually to verify your ledger.")
                        return
                    with ui.element("table").classes("w-full text-sm"):
                        with ui.element("thead"):
                            with ui.element("tr"):
                                for h in ("Account", "Balance"):
                                    with ui.element("th").classes("text-left py-1 pr-4"):
                                        ui.label(h)
                        with ui.element("tbody"):
                            totals: dict[str, Decimal] = defaultdict(Decimal)
                            for row in rows:
                                with ui.element("tr").classes("border-t"):
                                    for cell in row:
                                        with ui.element("td").classes("py-1 pr-4 font-mono"):
                                            ui.label(cell)
                                if len(row) >= 2:
                                    for part in row[1].split(","):
                                        part = part.strip()
                                        if not part:
                                            continue
                                        pieces = part.split()
                                        if len(pieces) == 2:
                                            try:
                                                totals[pieces[1]] += Decimal(pieces[0])
                                            except Exception:
                                                pass
                            if totals:
                                total_str = ", ".join(
                                    f"{v:,.2f} {c}" for c, v in sorted(totals.items())
                                )
                                with ui.element("tr").classes("border-t border-gray-400 font-bold"):
                                    with ui.element("td").classes("py-1 pr-4 font-mono"):
                                        ui.label("Total")
                                    with ui.element("td").classes("py-1 pr-4 font-mono"):
                                        ui.label(total_str)

            refresh_balances()
            ui.button("Refresh", on_click=refresh_balances).classes("mt-2")

        # Filter panel
        default_since = datetime.date.today() - datetime.timedelta(days=90)
        default_until = datetime.date.today()
        all_accounts = _get_accounts()

        with ui.card().classes("w-full"):
            # Quick presets
            with ui.row().classes("gap-1 mb-3 flex-wrap items-center"):
                ui.label("Quick:").classes("text-xs text-gray-400 mr-1")
                for _preset_label, _preset_fn in _PERIODS.items():
                    def _make_preset(fn=_preset_fn):
                        def _apply():
                            date_from.set_value(str(fn()))
                            date_to.set_value(str(datetime.date.today()))
                            refresh_all()
                        return _apply
                    ui.button(_preset_label, on_click=_make_preset()).props("dense outline").classes("text-xs")

            # Date range + account filter
            with ui.row().classes("gap-4 items-end flex-wrap"):
                date_from = ui.input("From", value=str(default_since)).props("type=date").classes("w-40")
                date_to = ui.input("To", value=str(default_until)).props("type=date").classes("w-40")
                account_select = (
                    ui.select(all_accounts, multiple=True, label="Filter accounts", value=[])
                    .props("use-chips use-input clearable")
                    .classes("flex-1 min-w-64")
                )
                ui.button("Apply", on_click=lambda: refresh_all()).props("outline")

        def _get_filter() -> tuple[datetime.date, datetime.date, list[str]]:
            try:
                since = datetime.date.fromisoformat(date_from.value)
            except (ValueError, TypeError):
                since = default_since
            try:
                until = datetime.date.fromisoformat(date_to.value)
            except (ValueError, TypeError):
                until = default_until
            return since, until, list(account_select.value or [])

        # Net worth over time
        with ui.card().classes("w-full"):
            ui.label("Liquid Assets").classes("text-lg font-semibold mb-2")
            net_worth_chart = ui.plotly(go.Figure()).classes("w-full").style("height: 280px")

        # Money flow Sankey + Pie
        with ui.row().classes("w-full gap-4 items-start"):
            with ui.card().classes("flex-1"):
                ui.label("Money Flow").classes("text-lg font-semibold mb-2")
                sankey_chart = ui.plotly(go.Figure()).classes("w-full").style("height: 560px")
                sankey_status = ui.label("").classes("text-sm text-gray-400")

            with ui.card().classes("w-80"):
                ui.label("Spending Breakdown").classes("text-lg font-semibold mb-2")
                pie_chart = ui.plotly(go.Figure()).classes("w-full").style("height: 560px")

        # Transactions
        with ui.card().classes("w-full"):
            with ui.row().classes("w-full items-center mb-2"):
                ui.label("Transactions").classes("text-lg font-semibold flex-1")
                ui.button("New", icon="add", on_click=lambda: new_transaction_dialog(on_save=refresh_transactions)).props("flat dense outline").classes("text-sm")
            tx_rows = ui.column()

        def refresh_net_worth() -> None:
            since, until, accts = _get_filter()
            net_worth_chart.figure = _net_worth_figure(since, until, accts)
            net_worth_chart.update()

        def refresh_sankey() -> None:
            since, until, accts = _get_filter()
            fig = _sankey_figure(since, until, accts)
            sankey_chart.figure = fig
            sankey_chart.update()
            n = len(fig.data[0].link["value"]) if fig.data else 0
            total = sum(fig.data[0].link["value"]) if fig.data else 0
            sankey_status.set_text(
                f"{n} flows — CAD {total:,.2f} total"
                if n else "No CAD transactions in this period."
            )

        def refresh_pie() -> None:
            since, until, accts = _get_filter()
            pie_chart.figure = _pie_figure(since, until, accts)
            pie_chart.update()

        def refresh_transactions() -> None:
            tx_rows.clear()
            since, until, accts = _get_filter()
            where = f"date >= {since} AND date <= {until}"
            if accts:
                pattern = "|".join(accts)
                where += f" AND account ~ '^({pattern})$'"
            raw = _run_query(
                f"SELECT date, account, payee, narration, position, "
                f"entry_meta('source'), entry_meta('filename'), entry_meta('lineno') "
                f"WHERE {where} ORDER BY date DESC"
            )
            grouped = _group_transactions(raw)
            with tx_rows:
                if not grouped:
                    ui.label("No transactions found.")
                    return
                with ui.element("table").classes("w-full text-sm"):
                    with ui.element("thead"):
                        with ui.element("tr"):
                            for h, cls in (
                                ("Date", "w-24"),
                                ("Payee", ""),
                                ("Narration", ""),
                                ("Account", ""),
                                ("Debit", "text-right w-32"),
                                ("Credit", "text-right w-32"),
                                ("", "w-16"),
                            ):
                                with ui.element("th").classes(f"text-left py-1 pr-4 {cls}"):
                                    ui.label(h)
                    with ui.element("tbody"):
                        for date, payee, narration, account, debit, credit, is_first, raw_source, filename, lineno in grouped:
                            border = "border-t border-gray-600" if is_first else ""
                            with ui.element("tr").classes(border):
                                for cell, cls in (
                                    (date, "py-0.5 pr-4 font-mono text-xs text-gray-400"),
                                    (payee, "py-0.5 pr-4 text-xs"),
                                    (narration, "py-0.5 pr-4 text-xs"),
                                    (account, "py-0.5 pr-4 font-mono text-xs"),
                                    (debit, "py-0.5 pr-4 font-mono text-xs text-right text-green-400"),
                                    (credit, "py-0.5 pr-4 font-mono text-xs text-right text-red-400"),
                                ):
                                    with ui.element("td").classes(cls):
                                        ui.label(cell)
                                with ui.element("td").classes("py-0.5"):
                                    if is_first and filename:
                                        with ui.row().classes("gap-0 items-center flex-nowrap"):
                                            ui.button(
                                                icon="edit",
                                                on_click=lambda rs=raw_source, fn=filename, ln=lineno: transaction_editor_dialog(
                                                    raw_source=rs,
                                                    file_path=fn,
                                                    lineno=int(ln) if ln else 0,
                                                    on_save=refresh_transactions,
                                                ),
                                            ).props("flat dense").classes("text-xs")
                                            if raw_source:
                                                ui.button(
                                                    icon="receipt",
                                                    on_click=lambda rs=raw_source: ui.run_javascript(
                                                        f"window.open('/api/print-packet?path={quote(rs)}', '_blank')"
                                                    ),
                                                ).props("flat dense").classes("text-xs").tooltip("View")
                                                ui.button(
                                                    icon="print",
                                                    on_click=lambda rs=raw_source: ui.run_javascript(
                                                        f"(function(){{var f=document.createElement('iframe');"
                                                        f"f.style.display='none';"
                                                        f"f.src='/api/print-packet?path={quote(rs)}';"
                                                        f"f.onload=function(){{f.contentWindow.print();}};"
                                                        f"document.body.appendChild(f);}})();"
                                                    ),
                                                ).props("flat dense").classes("text-xs").tooltip("Print")

        def refresh_all() -> None:
            refresh_net_worth()
            refresh_sankey()
            refresh_pie()
            refresh_transactions()

        account_select.on("update:model-value", lambda _: refresh_all())
        refresh_all()

        # Source summary
        with ui.card().classes("w-full"):
            ui.label("Sources").classes("text-lg font-semibold mb-2")
            sources = load_sources()
            with ui.grid(columns=3).classes("w-full gap-2"):
                for source in sources:
                    with ui.card().classes("p-2"):
                        ui.label(source.name).classes("font-semibold")
                        ui.label(source.plugin).classes("text-xs text-gray-500")
                        n = len([f for f in source.source_dir.rglob("*.bean") if f.name != "init.bean"])
                        ui.label(f"{n} transactions").classes("text-xs")

