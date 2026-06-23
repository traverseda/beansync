from __future__ import annotations

from pathlib import Path

import typer
from loguru import logger  # type: ignore[import-not-found]

from beancountio.config import AnySource, CUASource, EmailSource, InboxSource, load_accounts, load_sources, write_primary_includes

app = typer.Typer(help="bean-sync: LLM-assisted beancount transaction ingestion")
secrets_app = typer.Typer(help="Manage named secrets stored in the system keyring.")
app.add_typer(secrets_app, name="secrets")

_SOURCES_TEMPLATE = """\
# bean-sync source configuration — edit to match your accounts.
# See config.yaml.example for a fully annotated reference.
# Source types: !InboxSource, !EmailSource, !ImageSource, !CUASource, !StagehandSource
#
# enrichment: true  — .bean files used as LLM context for other sources; NOT in main.bean.
# enrichment: false — (default) .bean files included in main.bean via sources/_primary.bean.

!Config
sources:

  # Scan the whole inbox for receipts/invoices — enrichment context for bank transactions.
  # enrichment: true means these are matched to authoritative transactions, not added to the ledger.
  - !InboxSource
    name: inbox
    plugin: email-receipt
    source_dir: sources/inbox
    nullable: true
    enrichment: true
    hint: |-
      Generic email receipts and invoices from any sender. These are enrichment context —
      they will be attached to authoritative bank/card transactions to help identify payee
      and narration. Always make your best guess; do not call ask_user.
      If the amount is missing or unclear, still produce a transaction with amount 0.00 CAD
      so the payee and narration context is preserved.
      Return null only for things that are definitely not a purchase or charge: newsletters,
      marketing, bank statements, verification codes, or shipping updates with no charge.

  # A credit card that sends per-transaction email alerts.
  # Duplicate this block for each card you want to track.
  - !EmailSource
    name: mycard
    plugin: email
    source_dir: sources/mycard
    nullable: true
    enrichment: false
    sender: alerts@mybank.com
    hint: |-
      MyBank Visa credit card purchase alerts (alerts@mybank.com).
      Purchases: positive amount on the appropriate Expenses account,
      negative on Liabilities:CreditCard:MyCard.
      Return null if the email is a payment confirmation or contains no purchase.

  # Drop receipt photos (JPG/PNG/WEBP) into sources/receipts/ for automatic parsing.
  - !ImageSource
    name: receipts
    plugin: image
    source_dir: sources/receipts
    nullable: true
    enrichment: false
    hint: |-
      Photos of paper or digital receipts. Use the payment method visible on the receipt
      to pick the liability or asset account; if unclear, default to Assets:Checking.
      Use the grand total (after tax) as the transaction amount.
"""

_LEDGER_TEMPLATE = """\
* Options

option "title" "My Personal Finance"
option "operating_currency" "USD"
option "inferred_tolerance_default" "*:0.001"

plugin "beancount.plugins.auto_accounts"

* Accounts

; Edit this section to match your actual accounts.
; bean-sync reads everything under "* Accounts" to guide LLM categorisation.

1970-01-01 open Assets:Cash
1970-01-01 open Assets:Checking
1970-01-01 open Assets:Savings

1970-01-01 open Income:Paycheck
1970-01-01 open Income:Bonus
1970-01-01 open Income:InterestIncome
1970-01-01 open Income:Reimbursement

1970-01-01 open Expenses:AutoAndTransport
1970-01-01 open Expenses:BillsAndUtilities
1970-01-01 open Expenses:Electronics
1970-01-01 open Expenses:Entertainment
1970-01-01 open Expenses:FeesAndCharges
1970-01-01 open Expenses:Food
1970-01-01 open Expenses:Food:Groceries
1970-01-01 open Expenses:Food:Restaurants
1970-01-01 open Expenses:HealthAndFitness
1970-01-01 open Expenses:Housing
1970-01-01 open Expenses:Misc
1970-01-01 open Expenses:Shopping
1970-01-01 open Expenses:Travel

; Add your actual account numbers/card names as comments for reference.
1970-01-01 open Liabilities:CreditCard:MyCard

* Data

1970-01-01 open Equity:Initial

; bean-sync manages this file — run: bean-sync ingest  to regenerate after config changes.
include "sources/_primary.bean"
"""

_GITIGNORE = """\
# Credentials and secrets
.env

# State files (safe to delete — will be rebuilt on next sync)
sources/state/

# LLM merchant notes (personal, keep in your private ledger repo)
# .llm_notes.json
"""


def _filter(sources: list[AnySource], names: list[str] | None) -> list[AnySource]:
    if not names:
        return sources
    name_set = set(names)
    selected = [s for s in sources if s.name in name_set]
    missing = name_set - {s.name for s in selected}
    if missing:
        logger.warning("Unknown source name(s): {}", sorted(missing))
    return selected


@app.command()
def ingest(
    names: list[str] | None = typer.Argument(default=None, help="Source names to ingest (default: all, in order)"),
    headed: bool = typer.Option(False, "--headed/--headless", help="Show browser window (CUA only)"),
    since: str | None = typer.Option(None, "--since", help="Fetch data from this date onwards, e.g. 2026-06-18 (overrides saved state)"),
) -> None:
    """Fetch new data and parse it for each source."""
    import datetime as dt
    from beancountio import llm, sync_email, sync_cua

    since_date: dt.date | None = None
    if since:
        try:
            since_date = dt.date.fromisoformat(since)
        except ValueError:
            logger.error("Invalid date '{}' — use YYYY-MM-DD format", since)
            raise typer.Exit(1)

    all_sources = load_sources()
    write_primary_includes(all_sources)
    selected = _filter(all_sources, names)
    accounts = load_accounts()
    all_source_dirs = [s.source_dir for s in all_sources]

    i = 0
    while i < len(selected):
        source = selected[i]

        if source.plugin == "cua":
            # Batch consecutive CUA sources into one browser session.
            cua_batch: list[CUASource] = []
            while i < len(selected) and selected[i].plugin == "cua":
                s = selected[i]
                assert isinstance(s, CUASource)
                cua_batch.append(s)
                i += 1
            sync_cua.fetch_batch(cua_batch, headed=headed, since=since_date)
            for cua_source in cua_batch:
                with logger.contextualize(source=cua_source.name):
                    null_instr = llm.NULL_INSTRUCTION if cua_source.nullable else llm.NO_NULL_INSTRUCTION
                    prompt = llm.SYSTEM_PROMPT_TEMPLATE.format(hint=cua_source.hint, accounts=accounts, null_instruction=null_instr)
                    llm.parse_unprocessed(cua_source, prompt, all_source_dirs, nullable=cua_source.nullable)

        elif source.plugin == "email-receipt":
            # Integrated: needs the full excluded-senders list from other email sources.
            assert isinstance(source, InboxSource)
            excluded_senders = [addr for s in all_sources if isinstance(s, EmailSource) for addr in s.sender]
            with logger.contextualize(source=source.name):
                null_instr = llm.NULL_INSTRUCTION if source.nullable else llm.NO_NULL_INSTRUCTION
                prompt = llm.SYSTEM_PROMPT_TEMPLATE.format(hint=source.hint, accounts=accounts, null_instruction=null_instr)
                sync_email.ingest_receipt(source, prompt, all_source_dirs, since=since_date, excluded_senders=excluded_senders)
            i += 1

        else:
            # Generic dispatch: call source.fetch() then choose LLM parser by parse_mode.
            parse_mode = getattr(type(source), "parse_mode", "standard")
            with logger.contextualize(source=source.name):
                source.fetch(headed=headed, since=since_date)  # type: ignore[call-arg]
                null_instr = llm.NULL_INSTRUCTION if source.nullable else llm.NO_NULL_INSTRUCTION
                if parse_mode == "image":
                    prompt = llm.RECEIPT_SYSTEM_PROMPT_TEMPLATE.format(hint=source.hint, accounts=accounts, null_instruction=null_instr)
                    llm.parse_unprocessed_images(source, prompt, nullable=source.nullable)
                else:
                    prompt = llm.SYSTEM_PROMPT_TEMPLATE.format(hint=source.hint, accounts=accounts, null_instruction=null_instr)
                    llm.parse_unprocessed(source, prompt, all_source_dirs, nullable=source.nullable)
            i += 1


@app.command()
def parse(
    names: list[str] | None = typer.Argument(default=None, help="Source names to parse (default: all)"),
) -> None:
    """Parse existing unprocessed raw files without fetching new data.

    Useful after deleting sidecars to re-run the LLM with an updated model or prompt.
    """
    from beancountio import llm

    all_sources = load_sources()
    selected = _filter(all_sources, names)
    accounts = load_accounts()
    all_source_dirs = [s.source_dir for s in all_sources]

    for source in selected:
        if not source.source_dir.exists():
            logger.warning("Source dir {} does not exist, skipping", source.source_dir)
            continue
        null_instr = llm.NULL_INSTRUCTION if source.nullable else llm.NO_NULL_INSTRUCTION
        if source.plugin == "image":
            prompt = llm.RECEIPT_SYSTEM_PROMPT_TEMPLATE.format(hint=source.hint, accounts=accounts, null_instruction=null_instr)
            llm.parse_unprocessed_images(source, prompt, nullable=source.nullable)
        else:
            prompt = llm.SYSTEM_PROMPT_TEMPLATE.format(hint=source.hint, accounts=accounts, null_instruction=null_instr)
            llm.parse_unprocessed(source, prompt, all_source_dirs)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind"),
    port: int = typer.Option(8080, "--port", help="Port to listen on"),
    reload: bool = typer.Option(False, "--reload/--no-reload", help="Auto-reload on file changes"),
) -> None:
    """Launch the web UI."""
    from beancountio.ui.app import run
    run(host=host, port=port, reload=reload)


@app.command()
def init(
    directory: Path = typer.Argument(default=Path("."), help="Directory to initialise (default: current directory)"),
    force: bool = typer.Option(False, "--force", help="Overwrite existing files"),
) -> None:
    """Scaffold a new beancount ledger directory with bean-sync configuration."""
    target = directory.resolve()
    target.mkdir(parents=True, exist_ok=True)

    files = {
        target / "config.yaml": _SOURCES_TEMPLATE,
        target / "main.bean": _LEDGER_TEMPLATE,
        target / ".gitignore": _GITIGNORE,
    }

    created = []
    skipped = []
    for path, content in files.items():
        if path.exists() and not force:
            skipped.append(path.name)
            continue
        path.write_text(content)
        created.append(path.name)

    # Bootstrap sources/_primary.bean from the template config.
    primary_path = target / "sources" / "_primary.bean"
    if not primary_path.exists() or force:
        import os
        old_cwd = os.getcwd()
        try:
            os.chdir(target)
            write_primary_includes(load_sources())
        finally:
            os.chdir(old_cwd)
        created.append("sources/_primary.bean")

    for name in created:
        logger.success("Created {}", name)
    for name in skipped:
        logger.info("Skipped {} (already exists — use --force to overwrite)", name)

    if created:
        print(f"\nLedger initialised in {target}")
        print("\nNext steps:")
        print("  1. Set OPENROUTER_API_KEY (required for LLM parsing)")
        print("  2. Edit config.yaml — add your email senders and account names")
        print("  3. Edit main.bean — update the account list to match your finances")
        print("  4. Run: bean-sync ingest")


# --- secrets subcommands ---

@secrets_app.command("list")
def secrets_list() -> None:
    """List all registered secret names."""
    from beancountio.secrets import list_secrets
    known = list_secrets()
    if not known:
        print("No secrets registered. Use 'bean-sync secrets set <name>' to add one.")
        return
    for name, desc in sorted(known.items()):
        suffix = f"  # {desc}" if desc else ""
        print(f"  {name}{suffix}")


@secrets_app.command("set")
def secrets_set(
    name: str = typer.Argument(help="Secret name (used in config.yaml as !secret <name>)"),
    description: str = typer.Option("", "--description", "-d", help="Optional description"),
    value: str = typer.Option("", "--value", "-v", help="Secret value (omit to be prompted)"),
) -> None:
    """Store a secret value in the system keyring."""
    import getpass
    from beancountio.secrets import set_secret
    if not value:
        value = getpass.getpass(f"Value for '{name}': ")
    if not value:
        logger.error("No value provided — aborting.")
        raise typer.Exit(1)
    set_secret(name, value, description)
    logger.success("Secret '{}' saved.", name)


@secrets_app.command("delete")
def secrets_delete(
    name: str = typer.Argument(help="Secret name to remove"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Delete a secret from the keyring and secrets.yaml."""
    from beancountio.secrets import delete_secret, list_secrets
    if name not in list_secrets():
        logger.error("Secret '{}' not found.", name)
        raise typer.Exit(1)
    if not yes:
        typer.confirm(f"Delete secret '{name}'?", abort=True)
    delete_secret(name)
    logger.success("Secret '{}' deleted.", name)

