import datetime
import email
import email.header
import email.message
import email.utils
import imaplib
import re
from pathlib import Path

import yaml  # type: ignore[import-not-found]
from loguru import logger  # type: ignore[import-not-found]

from beansync.config import AnySource, EmailSource

DEFAULT_IMAP_HOST = "imap.gmail.com"
STATE_FILE = Path("sources/state/downloads.yaml")


def load_state() -> dict:
    if STATE_FILE.exists():
        return yaml.safe_load(STATE_FILE.read_text()) or {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(yaml.dump(state, default_flow_style=False))



def safe_filename(s: str) -> str:
    return re.sub(r"[^\w\-.]", "_", s)[:80].strip("_")


def quarter_subdir(base: Path, dt: datetime.datetime) -> Path:
    q = (dt.month - 1) // 3 + 1
    return base / f"{dt.year}-Q{q}"


def parse_date(msg: email.message.Message) -> datetime.datetime:
    try:
        return email.utils.parsedate_to_datetime(msg.get("Date", ""))
    except Exception:
        return datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)


def decode_subject(msg: email.message.Message) -> str:
    raw, charset = email.header.decode_header(msg.get("Subject", "no-subject"))[0]
    if isinstance(raw, bytes):
        return raw.decode(charset or "utf-8", errors="replace")
    return raw


def _decode_header_field(msg: email.message.Message, field: str) -> str:
    raw = msg.get(field, "")
    parts = email.header.decode_header(raw)
    decoded = []
    for chunk, charset in parts:
        if isinstance(chunk, bytes):
            decoded.append(chunk.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(chunk)
    return " ".join(decoded).strip()


def _inject_email_headers(html: bytes, msg: email.message.Message) -> bytes:
    """Prepend a header bar with From/To/Subject/Date/Message-ID into the HTML body."""
    import html as htmllib

    fields = [
        ("From", _decode_header_field(msg, "From")),
        ("To", _decode_header_field(msg, "To")),
        ("Subject", _decode_header_field(msg, "Subject")),
        ("Date", msg.get("Date", "")),
        ("Message-ID", msg.get("Message-ID", "")),
    ]
    rows = "".join(
        f'<tr><td style="padding:2px 8px 2px 0;font-weight:bold;white-space:nowrap;vertical-align:top">'
        f'{htmllib.escape(label)}</td>'
        f'<td style="padding:2px 0;word-break:break-all">{htmllib.escape(value)}</td></tr>'
        for label, value in fields if value
    )
    header_block = (
        '<div style="font-family:monospace;font-size:12px;border:1px solid #ccc;'
        'background:#f8f8f8;padding:8px 12px;margin:8px;border-radius:4px">'
        f'<table style="border-collapse:collapse">{rows}</table></div>'
    ).encode()

    # Insert after <body ...> if present, otherwise prepend
    import re as _re
    body_match = _re.search(rb"<body[^>]*>", html, _re.IGNORECASE)
    if body_match:
        pos = body_match.end()
        return html[:pos] + header_block + html[pos:]
    return header_block + html


def extract_html(msg: email.message.Message) -> bytes | None:
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                return payload
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                return f"<pre>{text}</pre>".encode()
    return None


def _connect(host: str, user: str, password: str) -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(host)
    imap.login(user, password)
    return imap


def _select_mailbox(imap: imaplib.IMAP4_SSL) -> bool:
    for folder in ('"[Gmail]/All Mail"', "INBOX"):
        status, _ = imap.select(folder, readonly=True)
        if status == "OK":
            return True
    return False


def _fetch_uids(imap: imaplib.IMAP4_SSL, criteria: list[str]) -> list[tuple[bytes, str]]:
    _, data = imap.search(None, *criteria, 'X-GM-RAW "-in:spam -in:trash"')
    msg_ids: list[bytes] = data[0].split()
    uids = []
    for msg_id in msg_ids:
        _, uid_data = imap.fetch(msg_id, "(UID)")  # type: ignore[arg-type]
        uid_line = uid_data[0]
        if not isinstance(uid_line, bytes):
            continue
        uid_match = re.search(rb"UID (\d+)", uid_line)
        if uid_match:
            uids.append((msg_id, uid_match.group(1).decode()))
    return uids


def _download_html(imap: imaplib.IMAP4_SSL, msg_id: bytes) -> tuple[bytes | None, email.message.Message | None]:
    _, msg_data = imap.fetch(msg_id, "(RFC822)")  # type: ignore[arg-type]
    envelope = msg_data[0]
    if not isinstance(envelope, tuple):
        return None, None
    raw = envelope[1]
    if not isinstance(raw, bytes):
        return None, None
    msg = email.message_from_bytes(raw)
    html = extract_html(msg)
    return html, msg


# --- email plugin ---

def fetch(source: EmailSource, since: datetime.date | None = None) -> int:
    """Download new emails from source.sender into source.source_dir.

    since overrides the saved last_sync date. Returns number of new files downloaded.
    """
    if not source.sender:
        raise ValueError(f"Source '{source.name}' (plugin: email) requires a 'sender' field")
    senders: list[str] = source.sender

    state = load_state()
    if since is None:
        since_str: str | None = state.get(source.name, {}).get("last_sync")
        since = datetime.date.fromisoformat(since_str) if since_str else datetime.date.today()

    source.source_dir.mkdir(parents=True, exist_ok=True)

    from beansync.config import SecretRef
    password = source.imap_password.resolve() if isinstance(source.imap_password, SecretRef) else source.imap_password
    imap = _connect(source.imap_host, source.imap_user, password)
    logger.info("Connecting as {} — fetching {} sender(s) since {}", source.imap_user, len(senders), since)

    downloaded = 0
    try:
        if not _select_mailbox(imap):
            logger.error("Could not select a mailbox for {}", senders)
            return 0

        for sender in senders:
            criteria = [f'FROM "{sender.lower()}"', f'SINCE {since.strftime("%d-%b-%Y")}']
            uid_pairs = _fetch_uids(imap, criteria)
            if not uid_pairs:
                logger.info("No new emails from {}", sender)
                continue
            for msg_id, uid in uid_pairs:
                if list(source.source_dir.rglob(f"*_uid{uid}.html")):
                    continue
                html, msg = _download_html(imap, msg_id)
                if html is None or msg is None:
                    logger.warning("No body for uid {} — skipping", uid)
                    continue
                dt = parse_date(msg)
                dest = quarter_subdir(source.source_dir, dt)
                dest.mkdir(parents=True, exist_ok=True)
                filename = f"{dt.strftime('%Y-%m-%d')}_{safe_filename(decode_subject(msg))}_uid{uid}.html"
                (dest / filename).write_bytes(_inject_email_headers(html, msg))
                logger.info("Saved {}/{}", dest.name, filename)
                downloaded += 1
    finally:
        imap.logout()

    state.setdefault(source.name, {})["last_sync"] = datetime.date.today().isoformat()
    save_state(state)
    logger.info("{} new email(s) downloaded for source '{}'", downloaded, source.name)
    return downloaded


# --- email-receipt plugin ---

def ingest_receipt(source: AnySource, system_prompt: str, all_source_dirs: list[Path], since: datetime.date | None = None, excluded_senders: list[str] | None = None) -> int:
    """Scan whole inbox, run LLM on each unseen email, save only receipts.

    since overrides the saved last_sync date.
    excluded_senders: email addresses handled by dedicated sources — skipped at search time.
    Tracks non-receipt UIDs in the state file to avoid re-processing.
    Returns number of receipts found.
    """
    from beansync.llm import html_to_text, parse_text
    from beansync.questions import QuestionDeferred, answered_context_for, clear_answered_for, queue_question

    enrichment_dirs = [d for d in all_source_dirs if d != source.source_dir]
    state = load_state()
    source_state = state.setdefault(source.name, {})
    if since is None:
        since_str: str | None = source_state.get("last_sync")
        since = datetime.date.fromisoformat(since_str) if since_str else datetime.date.today()
    non_receipt_uids: set[str] = set(source_state.get("non_receipt_uids", []))

    source.source_dir.mkdir(parents=True, exist_ok=True)

    from beansync.config import SecretRef
    imap_host = getattr(source, "imap_host", DEFAULT_IMAP_HOST)
    imap_user = getattr(source, "imap_user", "")
    raw_pw = getattr(source, "imap_password", "")
    password = raw_pw.resolve() if isinstance(raw_pw, SecretRef) else raw_pw
    imap = _connect(imap_host, imap_user, password)
    logger.info("Connecting as {} — scanning inbox since {}", imap_user, since)
    if excluded_senders:
        logger.info("Excluding senders already handled by dedicated sources: {}", excluded_senders)

    receipts = 0
    try:
        if not _select_mailbox(imap):
            logger.error("Could not select inbox mailbox")
            return 0

        criteria = [f'SINCE {since.strftime("%d-%b-%Y")}']
        for sender in (excluded_senders or []):
            criteria.extend(["NOT", f'FROM "{sender.lower()}"'])
        uid_pairs = _fetch_uids(imap, criteria)
        logger.info("Found {} email(s) to check", len(uid_pairs))

        for msg_id, uid in uid_pairs:
            # Already saved as a receipt
            if list(source.source_dir.rglob(f"*_uid{uid}.html")):
                continue
            # Already decided not a receipt
            if uid in non_receipt_uids:
                continue

            html, msg = _download_html(imap, msg_id)
            if html is None or msg is None:
                non_receipt_uids.add(uid)
                continue

            dt = parse_date(msg)
            subject = decode_subject(msg)
            logger.info("── uid:{} {}", uid, subject)

            text = html_to_text(html.decode("utf-8", errors="replace"))
            date_hint = dt.strftime("%Y-%m-%d")
            dest = quarter_subdir(source.source_dir, dt)
            filename = f"{date_hint}_{safe_filename(subject)}_uid{uid}.html"
            label = dest / filename
            try:
                entry = parse_text(
                    text, label, system_prompt, enrichment_dirs or None,
                    is_enrichment=getattr(source, "enrichment", False),
                    extra_context=answered_context_for(label),
                )
            except QuestionDeferred as exc:
                # Leave the uid out of non_receipt_uids so it's re-checked (with the
                # answer, once given) on the next run instead of being skipped forever.
                queue_question(source.name, label, exc.question, exc.options)
                continue
            clear_answered_for(label)

            if entry:
                dest.mkdir(parents=True, exist_ok=True)
                html_path = dest / filename
                html_path.write_bytes(_inject_email_headers(html, msg))
                sidecar = html_path.with_suffix(".bean")
                sidecar.write_text(entry + "\n")
                logger.success("   ✓ receipt saved: {}", filename)
                receipts += 1
            else:
                logger.info("   ✗ not a receipt")
                non_receipt_uids.add(uid)
    finally:
        imap.logout()

    source_state["last_sync"] = datetime.date.today().isoformat()
    source_state["non_receipt_uids"] = sorted(non_receipt_uids)
    save_state(state)
    logger.info("{} receipt(s) found in inbox", receipts)
    return receipts
