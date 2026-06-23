import base64
import html as html_module
import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Callable

import litellm  # type: ignore[import-not-found]
import questionary  # type: ignore[import-not-found]
import requests  # type: ignore[import-not-found]
from litellm import completion  # type: ignore[import-not-found]
from loguru import logger  # type: ignore[import-not-found]
from pydantic import BaseModel, ValidationError  # type: ignore[import-not-found]

from beancountio.config import LEDGER, MAX_RETRIES, MAX_SEARCHES, MAX_TOOL_ROUNDS, MODEL, VISION_MODEL, AnySource
from beancountio.notes import delete_note, get_notes_context, match_notes, save_note

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
        ["bean-query", str(LEDGER), query],
        capture_output=True, text=True, timeout=15,
    )
    output = (result.stdout + result.stderr).strip()
    return output or "(no results)"


def ask_user(question: str, options: list[str] | None = None) -> str:
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
                "Ask the user a clarifying question when you are uncertain about the correct account, payee, or narration. "
                "Call this instead of guessing whenever certainty is below 80. "
                "Write the question as a complete sentence that includes enough context to answer without re-reading the email: "
                "merchant name, amount, and why you are unsure. "
                "Example: 'What account should be used for NGDIRECT $47.23? This looks like a bank fee but could be a transfer.' "
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

IMPORTANT: "amount" must be a plain decimal string like "57.49" or "-57.49". Never include the currency symbol or unit inside "amount". "currency" is always a separate field.

Rules:
- This is personal household spending, not business accounting
- payee is the merchant name only (e.g. "Steam", "Canada Computers"), not the email subject
- narration is what was bought (e.g. "game purchase", "electronics"), not boilerplate like "Pending charge"
- account should follow the taxonomy in the Accounts list below; you may create sub-accounts or new accounts not in the list when the situation clearly calls for it — the list is a guide, not a hard constraint
- for recurring subscription services (streaming, software, memberships), use a dedicated sub-account under the appropriate category, e.g. Expenses:Entertainment:Spotify, Expenses:BillsAndUtilities:Youtube — not the bare parent account
- certainty is your confidence (0-100) in the account classification; if certainty would be below 80, call ask_user — include merchant name, amount, and your uncertainty reason in the question, and provide account options formatted as "Account — why it fits"
- amounts must balance to zero; credit card charges are: expense positive, liability negative (e.g. Expenses:X +38.69, Liabilities:CreditCard:Collabria -38.69)

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
) -> str | None:
    """Run the LLM on already-extracted plain text. source_label is used for the beancount source field.

    When nullable=False, null responses are retried and a RuntimeError is raised on exhaustion.
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
        {"role": "user", "content": notes_context + user_content},
    ]

    last_error: Exception | None = None
    error_retries = 0
    tool_rounds = 0
    searches_done = 0
    while error_retries < MAX_RETRIES:
        available = [t for t in TOOLS if t["function"]["name"] != "tavily_search" or searches_done < MAX_SEARCHES]
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


def parse_source(html_file: Path, system_prompt: str, enrichment_dirs: list[Path] | None = None, nullable: bool = True) -> str | None:
    """Parse an on-disk HTML/text source file."""
    text = html_to_text(html_file.read_text(encoding="utf-8", errors="replace"))
    return parse_text(text, html_file, system_prompt, enrichment_dirs, nullable=nullable)


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

IMPORTANT: "amount" must be a plain decimal string like "57.49" or "-57.49". Never include the currency symbol inside "amount". "currency" is always a separate field.

Rules:
- This is personal household spending, not business accounting
- payee is the store or vendor name only
- narration is a brief description of what was purchased
- Use the total/grand-total amount from the receipt, not subtotals
- If the payment method is visible (e.g. Visa ending 1234), use the matching liability account; otherwise default to Assets:Checking:CUA
- account should follow the taxonomy in the Accounts list below
- certainty is your confidence (0-100); if below 80, call ask_user
- amounts must balance to zero

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
) -> str | None:
    """Send a receipt image to a vision LLM and return a beancount entry or None."""
    mime = IMAGE_MIME.get(image_file.suffix.lower(), "image/jpeg")
    image_data = base64.b64encode(image_file.read_bytes()).decode()
    date_hint = image_file.stem[:10]

    notes_context = get_notes_context({})
    user_text = notes_context + f"Date hint (use if no date visible on receipt): {date_hint}\n\nExtract the transaction from this receipt."

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
        available = [t for t in TOOLS if t["function"]["name"] != "tavily_search" or searches_done < MAX_SEARCHES]
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
        entry = parse_image(raw_file, system_prompt, nullable=nullable)
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
        entry = parse_source(raw_file, system_prompt, enrichment_dirs or None, nullable=nullable)
        sidecar.write_text(entry + "\n" if entry else "")
        if entry:
            logger.success("   ✓ {}\n{}", sidecar, entry)
        else:
            logger.info("   ✗ skipped (no transaction)")
        new_count += 1
    logger.info("Processed {} new file(s) from {}", new_count, source.source_dir)
    return new_count
