import base64
import contextvars
import html as html_module
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

_BEAN_QUERY = str(Path(sys.executable).parent / "bean-query")
from typing import Callable

import litellm  # type: ignore[import-not-found]
import questionary  # type: ignore[import-not-found]
import requests  # type: ignore[import-not-found]
from litellm import completion  # type: ignore[import-not-found]
from loguru import logger  # type: ignore[import-not-found]
from pydantic import BaseModel, ValidationError  # type: ignore[import-not-found]

from beansync.config import LEDGER, MAX_RETRIES, MAX_SEARCHES, MAX_TOOL_ROUNDS, MODEL, VISION_MODEL, AnySource
from beansync.notes import delete_note, get_notes_context, match_notes, save_note
from beansync.questions import QuestionDeferred, answered_context_for, clear_answered_for, queue_question

# Set True (by scheduler._run_ingest_once, and by the Questions page's targeted
# re-parse) whenever ask_user() would otherwise block with no one watching a
# terminal. ask_user() then raises QuestionDeferred instead of prompting.
UNATTENDED: contextvars.ContextVar[bool] = contextvars.ContextVar("UNATTENDED", default=False)

litellm.suppress_debug_info = True

TAVILY_CACHE_FILE = Path(".tavily_cache.json")

_tavily_cache: dict[str, str] = (
    json.loads(TAVILY_CACHE_FILE.read_text()) if TAVILY_CACHE_FILE.exists() else {}
)


def tavily_search(merchant: str) -> str:
    if merchant in _tavily_cache:
        logger.info("Tavily cache hit: {}", merchant)
        return _tavily_cache[merchant]
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return "Tavily unavailable (no TAVILY_API_KEY)"
    resp = requests.post(
        "https://api.tavily.com/search",
        json={"api_key": api_key, "query": merchant, "max_results": 3, "search_depth": "advanced", "include_answer": True},
        timeout=15,
    )
    data = resp.json()
    result = data.get("answer") or "No results."
    _tavily_cache[merchant] = result
    TAVILY_CACHE_FILE.write_text(json.dumps(_tavily_cache, indent=2))
    return result


def query_ledger(query: str) -> str:
    result = subprocess.run(
        [_BEAN_QUERY, str(LEDGER), query],
        capture_output=True, text=True, timeout=15,
    )
    output = (result.stdout + result.stderr).strip()
    return output or "(no results)"


def _all_ledger_accounts() -> set[str]:
    """Every account actually in use in the ledger (open directives, explicit or auto-opened)."""
    try:
        result = subprocess.run(
            [_BEAN_QUERY, "-f", "csv", str(LEDGER), "SELECT DISTINCT account"],
            capture_output=True, text=True, timeout=15,
        )
    except (subprocess.SubprocessError, OSError):
        return set()
    lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    if lines and lines[0].lower() == "account":
        lines = lines[1:]
    return set(lines)


def list_accounts(search: str = "") -> str:
    """List accounts currently in use in the ledger, optionally filtered by a regex."""
    accounts = sorted(_all_ledger_accounts())
    if search:
        try:
            pattern = re.compile(search, re.IGNORECASE)
        except re.error as e:
            return f"Invalid regex pattern: {e}"
        accounts = [a for a in accounts if pattern.search(a)]
    return "\n".join(accounts) if accounts else "(no matching accounts)"


def annotate_accounts(curated: str) -> str:
    """Append a count of in-use accounts not covered by the curated list.

    Deliberately doesn't list them by name: a live full account dump would make the
    prompt balloon and drift over years (old telcos, closed loans, etc.) baked into
    every request. The count plus list_accounts is enough for the model to check
    before inventing something new, without permanently listing every account ever used.
    """
    curated_names = set(re.findall(r"\bopen\s+(\S+)", curated))
    extra = len(_all_ledger_accounts() - curated_names)
    if not extra:
        return curated
    return (
        f"{curated}\n\n"
        f"(and {extra} more account(s) currently in use in the ledger, not listed above — "
        f"call list_accounts to search for one by name before creating a new account)"
    )


def ask_user(question: str, options: list[str] | None = None) -> str:
    if UNATTENDED.get():
        raise QuestionDeferred(question, options)
    logger.warning("   AI question: {}", question)
    if options:
        OTHER = "Other (type my own)..."
        choice = questionary.select(f"[AI question] {question}", choices=[*options, OTHER]).ask()
        if choice and choice != OTHER:
            return choice
    answer = questionary.text(f"[AI question] {question}").ask()
    return answer or ""


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "tavily_search",
            "description": (
                "Look up an unknown merchant by name. "
                "Pass the raw merchant name exactly as it appears in the source. "
                "Only call this for merchants NOT already covered by a note in the source context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "merchant": {"type": "string", "description": "Raw merchant name from the source, e.g. 'SDFPUBNIX' or 'NGDIRECT SC 29611'"},
                },
                "required": ["merchant"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_note",
            "description": (
                "Persist a note about a merchant or payee so it is available in future sources. "
                "The key is a regex pattern matched case-insensitively against source text. "
                "Overwrites any existing note with the same key. "
                "Do NOT save notes for merchants/payees already covered by an existing note unless you are correcting it. "
                "IMPORTANT: After calling ask_user to clarify a payee or individual (e.g. a person receiving an e-Transfer), "
                "always call save_note to record what the user said so you do not ask again for the same payee."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Regex pattern to match this merchant or payee in future sources (e.g. 'NGDIRECT' or 'Wendy Cat')"},
                    "value": {"type": "string", "description": "What to remember: who the payee is, what the payment is for, and which account to use"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_note",
            "description": "Delete a note that is wrong or no longer needed. The key must match exactly.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Exact key of the note to delete"},
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_accounts",
            "description": (
                "List accounts currently in use in the ledger, optionally filtered by a case-insensitive "
                "regex on the account name (e.g. 'Loans' or 'Wendy'). The Accounts list in this prompt only "
                "shows the curated core taxonomy — call this before creating any account not already in that "
                "list, to check whether it (or something close to it) already exists."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Regex to filter account names; omit to list everything"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_ledger",
            "description": (
                "Run a BQL (beancount query language) query against the ledger. "
                "Use this to look up past transactions for a payee, check account balances, "
                "or find how similar merchants were previously classified."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "A BQL SELECT query, e.g. \"SELECT date, payee, narration WHERE payee ~ 'Steam'\""},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a clarifying question when you are uncertain about the correct account, payee, narration, "
                "or how to split a transaction across multiple categories. "
                "Call this instead of guessing whenever certainty is below 80. "
                "Write the question as a complete sentence that includes enough context to answer without re-reading the email: "
                "merchant name, amount, and why you are unsure. "
                "Example: 'What account should be used for NGDIRECT $47.23? This looks like a bank fee but could be a transfer.' "
                "For cash withdrawals or mixed-category purchases, ask how to split: "
                "'How should the $100.00 ATM withdrawal be split? e.g. groceries + gas.' "
                "Always provide 'options' when there are plausible candidates — format each option as "
                "'Account:SubAccount — brief reason it fits', e.g. 'Expenses:Financial — bank service fee' or "
                "'Assets:Savings:CUA — internal transfer'. The user cannot see the source email."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": (
                            "Full-sentence question with merchant name, amount, and reason for uncertainty. "
                            "Must be understandable without the source email."
                        ),
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Plausible account choices, each formatted as 'Account:SubAccount — why it fits'. "
                            "Include 2-4 candidates. Omit only for genuinely free-form answers like payee names."
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    },
]

TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    "tavily_search": tavily_search,
    "save_note": save_note,
    "delete_note": delete_note,
    "query_ledger": query_ledger,
    "list_accounts": list_accounts,
    "ask_user": ask_user,
}


class Posting(BaseModel):
    account: str
    amount: str
    currency: str = "CAD"


class Transaction(BaseModel):
    reasoning: str = ""
    date: str
    payee: str
    narration: str = ""
    certainty: int = 100
    postings: list[Posting]


NULL_INSTRUCTION = "If the email is a denial, a decline, a verification code, or contains no actual charge, respond with exactly: null\n\nOtherwise respond"
NO_NULL_INSTRUCTION = "Respond"

ENRICHMENT_NOTE = (
    "- ENRICHMENT SOURCE: this file is not an authoritative record, only context used to help "
    "classify other transactions (e.g. attaching a receipt's payee to a bank alert). ask_user is "
    "not available here — always make your best guess for account, payee, narration, and any "
    "split, even at low certainty. Never leave a field blank.\n"
)

SYSTEM_PROMPT_TEMPLATE = """You extract personal credit card transactions from financial notification emails.

Respond with raw JSON only — no markdown fences, no beancount syntax, no prose.

{null_instruction} with JSON in this exact shape:
{{
  "reasoning": "brief explanation of account choice and any uncertainty — omit or leave empty if the classification is obvious",
  "date": "YYYY-MM-DD",
  "payee": "Vendor Name",
  "narration": "what was purchased",
  "certainty": 95,
  "postings": [
    {{"account": "Expenses:Category", "amount": "12.34", "currency": "CAD"}},
    {{"account": "Liabilities:CreditCard:Collabria", "amount": "-12.34", "currency": "CAD"}}
  ]
}}

Split transaction example (multiple expense postings):
{{
  "postings": [
    {{"account": "Expenses:Food:Groceries", "amount": "35.00", "currency": "CAD"}},
    {{"account": "Expenses:Household", "amount": "15.00", "currency": "CAD"}},
    {{"account": "Assets:Cash", "amount": "-50.00", "currency": "CAD"}}
  ]
}}

IMPORTANT: "amount" must be a plain decimal string like "57.49" or "-57.49". Never include the currency symbol or unit inside "amount". "currency" is always a separate field.

Rules:
{enrichment_note}- This is personal household spending, not business accounting
- payee is the merchant name only (e.g. "Steam", "Canada Computers"), not the email subject
- narration is what was bought (e.g. "game purchase", "electronics"), not boilerplate like "Pending charge"
- account should follow the taxonomy in the Accounts list below. Prefer an existing account. If nothing fits, you may add a sub-account under an existing top-level category (e.g. Expenses:Entertainment:NewService) — call list_accounts first to check whether it (or something close) already exists. Never invent a new top-level category, and never create a per-person account (e.g. Expenses:Loans:SomeName) — for a payment to/from a specific person (loan, reimbursement, e-transfer), use a single shared account such as Expenses:Loans or Assets:Receivable and put the person's name in the narration instead; ask_user if you're unsure which shared account applies
- for recurring subscription services (streaming, software, memberships), use a dedicated sub-account under the appropriate category, e.g. Expenses:Entertainment:Spotify, Expenses:BillsAndUtilities:Youtube — not the bare parent account
- certainty is your confidence (0-100) in the account classification; if certainty would be below 80, call ask_user — include merchant name, amount, and your uncertainty reason in the question, and provide account options formatted as "Account — why it fits"
- amounts must balance to zero; credit card charges are: expense positive, liability negative (e.g. Expenses:X +38.69, Liabilities:CreditCard:Collabria -38.69)
- SPLIT TRANSACTIONS: use multiple expense postings when a single charge covers distinct categories (e.g. a cash withdrawal split between groceries and gas, a store where you bought both food and household items); call ask_user to find out how to split if the breakdown is not evident from the source

Notes (persistent memory):
- The source context includes a "Relevant notes" section listing previously saved facts about merchants and payees seen in this source.
- Notes are authoritative: if a merchant or payee is covered by a note, use that classification directly. Do not search for it.
- Use tavily_search only for merchants with no existing note.
- Use save_note to record genuinely new merchants and payees so they are remembered in future sources. Do not re-save those already covered by a note unless correcting a mistake.
- After calling ask_user to clarify any payee (including individuals receiving e-Transfers), always follow up with save_note so you do not ask the same question again.
- Use delete_note to remove notes that are wrong.

{hint}

Accounts:
{accounts}"""


def html_to_text(html_content: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html_content, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = html_module.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_json(text: str) -> object:
    text = re.sub(r"```(?:json)?", "", text).strip()
    match = re.search(r'(\{.*\}|null)', text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON found in: {text!r}")
    return json.loads(match.group(1))


def transaction_to_beancount(tx: Transaction, source: Path) -> str:
    lines = []
    if tx.reasoning:
        lines.append(f"; {tx.reasoning}")
    lines.append(f'{tx.date} * "{tx.payee}" "{tx.narration}"')
    lines.append(f'  source: "{source}"')
    lines.append(f'  certainty: {tx.certainty}')
    for p in tx.postings:
        lines.append(f"  {p.account}  {p.amount} {p.currency}")
    return "\n".join(lines)


def find_enrichment(html_file: Path, source_text: str, enrichment_dirs: list[Path], window_days: int = 3) -> list[str]:
    try:
        base_date = datetime.strptime(html_file.stem[:10], "%Y-%m-%d").date()
    except ValueError:
        return []
    amounts = set(re.findall(r'\$(\d+\.\d{2})', source_text))
    if not amounts:
        return []
    results = []
    for enrich_dir in enrichment_dirs:
        for candidate in sorted(enrich_dir.rglob("*.bean")):
            if not candidate.stat().st_size:
                continue
            try:
                candidate_date = datetime.strptime(candidate.stem[:10], "%Y-%m-%d").date()
            except ValueError:
                continue
            if abs((candidate_date - base_date).days) > window_days:
                continue
            bean_text = candidate.read_text(encoding="utf-8", errors="replace").strip()
            if any(amt in bean_text for amt in amounts):
                results.append(bean_text)
    return results


def parse_text(
    text: str,
    source_label: Path,
    system_prompt: str,
    enrichment_dirs: list[Path] | None = None,
    nullable: bool = True,
    is_enrichment: bool = False,
    extra_context: str = "",
) -> str | None:
    """Run the LLM on already-extracted plain text. source_label is used for the beancount source field.

    When nullable=False, null responses are retried and a RuntimeError is raised on exhaustion.
    is_enrichment=True means this source itself is enrichment-only context (not an authoritative
    record), so ask_user is withheld and the LLM is instructed to always guess instead.
    extra_context is prepended to the user content verbatim (e.g. previously-answered questions).
    Raises QuestionDeferred if ask_user is called while UNATTENDED is set.
    """
    date_hint = source_label.stem[:10]
    user_content = f"Date: {date_hint}\n\n{text}"

    if enrichment_dirs:
        related = find_enrichment(source_label, text, enrichment_dirs)
        if related:
            logger.info("   Enrichment: {} related source(s) found", len(related))
            user_content += "\n\n---\nRelated sources (use for payee/narration context):\n\n" + "\n\n---\n".join(related)

    matched = match_notes(text)
    if matched:
        logger.info("   Notes matched: {}", list(matched.keys()))

    notes_context = get_notes_context(matched)
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": extra_context + notes_context + user_content},
    ]

    last_error: Exception | None = None
    error_retries = 0
    tool_rounds = 0
    searches_done = 0
    while error_retries < MAX_RETRIES:
        available = [
            t for t in TOOLS
            if (t["function"]["name"] != "tavily_search" or searches_done < MAX_SEARCHES)
            and (t["function"]["name"] != "ask_user" or not is_enrichment)
        ]
        tools = available if tool_rounds < MAX_TOOL_ROUNDS else None
        response = completion(
            model=MODEL,
            messages=messages,
            tools=tools,
            request_timeout=120,
            extra_body={"provider": {"data_collection": "deny", "sort": "price"}},
        )
        headers = response._hidden_params.get("additional_headers", {})
        generation_id = headers.get("llm_provider-x-generation-id", "unknown")
        logger.debug("   model={} gen={}", response.model, generation_id)

        message = response.choices[0].message

        if message.tool_calls:
            messages.append(message)
            for tc in message.tool_calls:
                fn = TOOL_HANDLERS.get(tc.function.name)
                args = json.loads(tc.function.arguments)
                logger.warning("Tool call: {}({})", tc.function.name, args)
                if tc.function.name == "tavily_search":
                    searches_done += 1
                try:
                    result = fn(**args) if fn else f"Unknown tool: {tc.function.name}"
                except TypeError as e:
                    result = f"Tool call error: {e}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            tool_rounds += 1
            continue

        raw = (message.content or "").strip()
        try:
            data = extract_json(raw)
            if data is None:
                if nullable:
                    return None
                error_retries += 1
                logger.warning("LLM returned null for authoritative source {}, retry {}/{}", source_label.name, error_retries, MAX_RETRIES)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "This source contains a real transaction. Do not respond with null. Provide the JSON transaction data."})
                last_error = ValueError("LLM returned null")
                continue
            tx = Transaction.model_validate(data)
            return transaction_to_beancount(tx, source_label)
        except (ValueError, ValidationError) as exc:
            last_error = exc
            error_retries += 1
            logger.warning("Parse attempt {}/{} failed: {}", error_retries, MAX_RETRIES, exc)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"Invalid output: {exc}. Try again, output only JSON."})

    raise RuntimeError(f"LLM failed for {source_label.name} after {MAX_RETRIES} retries: {last_error}")


def parse_source(
    html_file: Path,
    system_prompt: str,
    enrichment_dirs: list[Path] | None = None,
    nullable: bool = True,
    is_enrichment: bool = False,
    extra_context: str = "",
) -> str | None:
    """Parse an on-disk HTML/text source file."""
    text = html_to_text(html_file.read_text(encoding="utf-8", errors="replace"))
    return parse_text(text, html_file, system_prompt, enrichment_dirs, nullable=nullable, is_enrichment=is_enrichment, extra_context=extra_context)


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
IMAGE_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

RECEIPT_SYSTEM_PROMPT_TEMPLATE = """You extract transactions from photos of paper or digital receipts.

Respond with raw JSON only — no markdown fences, no beancount syntax, no prose.

{null_instruction} with JSON in this exact shape:
{{
  "reasoning": "brief explanation of account choice and any uncertainty — omit if obvious",
  "date": "YYYY-MM-DD",
  "payee": "Vendor Name",
  "narration": "what was purchased",
  "certainty": 95,
  "postings": [
    {{"account": "Expenses:Category", "amount": "12.34", "currency": "CAD"}},
    {{"account": "Assets:Checking:CUA", "amount": "-12.34", "currency": "CAD"}}
  ]
}}

Split transaction example (multiple expense postings that sum to the total):
{{
  "postings": [
    {{"account": "Expenses:Food:Groceries", "amount": "42.00", "currency": "CAD"}},
    {{"account": "Expenses:Household", "amount": "18.00", "currency": "CAD"}},
    {{"account": "Assets:Checking:CUA", "amount": "-60.00", "currency": "CAD"}}
  ]
}}

IMPORTANT: "amount" must be a plain decimal string like "57.49" or "-57.49". Never include the currency symbol inside "amount". "currency" is always a separate field.

Rules:
{enrichment_note}- This is personal household spending, not business accounting
- payee is the store or vendor name only
- narration is a brief description of what was purchased
- Use the total/grand-total amount from the receipt, not subtotals
- If the payment method is visible (e.g. Visa ending 1234), use the matching liability account; otherwise default to Assets:Checking:CUA
- account should follow the taxonomy in the Accounts list below. Prefer an existing account; call list_accounts before creating a sub-account not already listed. Never invent a new top-level category or a per-person account
- certainty is your confidence (0-100); if below 80, call ask_user
- amounts must balance to zero
- SPLIT TRANSACTIONS: if the receipt shows items from clearly distinct categories (e.g. groceries and household supplies at a superstore), use multiple expense postings; call ask_user to clarify the split if line-item details are unclear

Notes (persistent memory):
- Check "Relevant notes" for previously saved merchant facts before classifying.
- Use save_note to record genuinely new merchants.

{hint}

Accounts:
{accounts}"""


def parse_image(
    image_file: Path,
    system_prompt: str,
    nullable: bool = True,
    is_enrichment: bool = False,
    extra_context: str = "",
) -> str | None:
    """Send a receipt image to a vision LLM and return a beancount entry or None.

    Raises QuestionDeferred if ask_user is called while UNATTENDED is set.
    """
    mime = IMAGE_MIME.get(image_file.suffix.lower(), "image/jpeg")
    image_data = base64.b64encode(image_file.read_bytes()).decode()
    date_hint = image_file.stem[:10]

    notes_context = get_notes_context({})
    user_text = extra_context + notes_context + f"Date hint (use if no date visible on receipt): {date_hint}\n\nExtract the transaction from this receipt."

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": [
            {"type": "text", "text": user_text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_data}"}},
        ]},
    ]

    last_error: Exception | None = None
    error_retries = 0
    tool_rounds = 0
    searches_done = 0
    while error_retries < MAX_RETRIES:
        available = [
            t for t in TOOLS
            if (t["function"]["name"] != "tavily_search" or searches_done < MAX_SEARCHES)
            and (t["function"]["name"] != "ask_user" or not is_enrichment)
        ]
        tools = available if tool_rounds < MAX_TOOL_ROUNDS else None
        response = completion(
            model=VISION_MODEL,
            messages=messages,
            tools=tools,
            request_timeout=120,
            extra_body={"provider": {"data_collection": "deny", "sort": "price"}},
        )
        message = response.choices[0].message

        if message.tool_calls:
            messages.append(message)
            for tc in message.tool_calls:
                fn = TOOL_HANDLERS.get(tc.function.name)
                args = json.loads(tc.function.arguments)
                logger.warning("Tool call: {}({})", tc.function.name, args)
                if tc.function.name == "tavily_search":
                    searches_done += 1
                try:
                    result = fn(**args) if fn else f"Unknown tool: {tc.function.name}"
                except TypeError as e:
                    result = f"Tool call error: {e}"
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            tool_rounds += 1
            continue

        raw = (message.content or "").strip()
        try:
            data = extract_json(raw)
            if data is None:
                if nullable:
                    return None
                error_retries += 1
                logger.warning("Vision LLM returned null for {}, retry {}/{}", image_file.name, error_retries, MAX_RETRIES)
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content": "This image contains a real receipt. Do not respond with null. Provide the JSON transaction data."})
                last_error = ValueError("LLM returned null")
                continue
            tx = Transaction.model_validate(data)
            return transaction_to_beancount(tx, image_file)
        except (ValueError, ValidationError) as exc:
            last_error = exc
            error_retries += 1
            logger.warning("Parse attempt {}/{} failed: {}", error_retries, MAX_RETRIES, exc)
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": f"Invalid output: {exc}. Try again, output only JSON."})

    raise RuntimeError(f"Vision LLM failed for {image_file.name} after {MAX_RETRIES} retries: {last_error}")


def parse_unprocessed_images(source: AnySource, system_prompt: str, nullable: bool = True) -> int:
    """Parse all image files in source.source_dir that don't have .bean sidecars yet."""
    source.source_dir.mkdir(parents=True, exist_ok=True)
    init = source.source_dir / "init.bean"
    if not init.exists():
        init.write_text("; Placeholder so glob includes in main.bean always match.\n")

    raw_files = sorted(
        f for f in source.source_dir.rglob("*")
        if f.suffix.lower() in IMAGE_SUFFIXES and f.is_file()
    )
    new_count = 0
    for raw_file in raw_files:
        sidecar = raw_file.with_suffix(".bean")
        if sidecar.exists():
            continue
        logger.info("── {}", raw_file)
        try:
            entry = parse_image(
                raw_file, system_prompt, nullable=nullable,
                is_enrichment=getattr(source, "enrichment", False),
                extra_context=answered_context_for(raw_file),
            )
        except QuestionDeferred as exc:
            queue_question(source.name, raw_file, exc.question, exc.options)
            continue
        clear_answered_for(raw_file)
        sidecar.write_text(entry + "\n" if entry else "")
        if entry:
            logger.success("   ✓ {}\n{}", sidecar, entry)
        else:
            logger.info("   ✗ skipped (no transaction)")
        new_count += 1
    logger.info("Processed {} new image(s) from {}", new_count, source.source_dir)
    return new_count


def parse_unprocessed(source: AnySource, system_prompt: str, all_source_dirs: list[Path], nullable: bool = True) -> int:
    """Parse all raw files in source.source_dir that don't have .bean sidecars yet.

    Returns number of files processed.
    """
    source.source_dir.mkdir(parents=True, exist_ok=True)
    init = source.source_dir / "init.bean"
    if not init.exists():
        init.write_text("; Placeholder so glob includes in main.bean always match.\n")

    enrichment_dirs = [d for d in all_source_dirs if d != source.source_dir]
    raw_files = sorted(
        f for f in source.source_dir.rglob("*")
        if f.suffix in (".html", ".txt") and f.is_file()
    )
    new_count = 0
    for raw_file in raw_files:
        sidecar = raw_file.with_suffix(".bean")
        if sidecar.exists():
            continue
        logger.info("── {}", raw_file)
        try:
            entry = parse_source(
                raw_file, system_prompt, enrichment_dirs or None, nullable=nullable,
                is_enrichment=getattr(source, "enrichment", False),
                extra_context=answered_context_for(raw_file),
            )
        except QuestionDeferred as exc:
            queue_question(source.name, raw_file, exc.question, exc.options)
            continue
        clear_answered_for(raw_file)
        sidecar.write_text(entry + "\n" if entry else "")
        if entry:
            logger.success("   ✓ {}\n{}", sidecar, entry)
        else:
            logger.info("   ✗ skipped (no transaction)")
        new_count += 1
    logger.info("Processed {} new file(s) from {}", new_count, source.source_dir)
    return new_count
