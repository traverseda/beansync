#!/usr/bin/env python3
"""Verify an interrupted ingest can neither lose transactions nor wedge the app.

Covers the three failure modes an interrupted run used to hit:
  * a question nobody answers, which discarded the email it was asked about
  * a lock file naming a dead (or reused) pid, which blocked every later run
  * a single bad message aborting the rest of the scan

Runs entirely offline — IMAP and the LLM are stubbed. Everything happens in a
temporary directory; the real ledger is never touched.

Usage: test_reliability.py
Exits non-zero on any failure.
"""
from __future__ import annotations

import datetime
import email
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
WORK = Path(tempfile.mkdtemp(prefix="beansync-reliability-"))
os.chdir(WORK)

from typer.testing import CliRunner  # noqa: E402

from beansync import cli, llm, sync_email  # noqa: E402
from beansync import questions as questions_store  # noqa: E402
from beansync.cli import _parse_when, _sidecars_since  # noqa: E402
from beansync.config import EmailSource, InboxSource  # noqa: E402
from beansync.questions import QuestionDeferred  # noqa: E402
from beansync.scheduler import _LOCK_FILE, ingest_lock  # noqa: E402

results: list[tuple[str, bool]] = []


def check(name: str, passed: object) -> None:
    results.append((name, bool(passed)))
    print(f"{'PASS' if passed else 'FAIL'}  {name}")


# --- the ingest lock ---------------------------------------------------------

holder = subprocess.Popen(
    [sys.executable, "-c", textwrap.dedent(f"""
        import os, sys, time
        os.chdir({str(WORK)!r}); sys.path.insert(0, {str(REPO)!r})
        from beansync.scheduler import ingest_lock
        with ingest_lock():
            print("held", flush=True)
            time.sleep(120)
    """)],
    stdout=subprocess.PIPE, text=True,
)
assert holder.stdout is not None and holder.stdout.readline().strip() == "held"

try:
    with ingest_lock():
        check("a second ingest is refused while the first is alive", False)
except RuntimeError as exc:
    check("a second ingest is refused while the first is alive", "already running" in str(exc))
    check("the refusal names the holding process", f"pid {holder.pid}" in str(exc))

holder.kill()
holder.wait()
try:
    with ingest_lock():
        check("the lock is released when its holder is killed", True)
except RuntimeError:
    check("the lock is released when its holder is killed", False)

# The container-restart case: pids restart from 1, so a leftover lock file
# routinely names a pid that now belongs to something unrelated.
_LOCK_FILE.write_text(f"pid {os.getpid()}, started whenever")
try:
    with ingest_lock():
        check("a lock file naming an unrelated live pid does not block", True)
except RuntimeError:
    check("a lock file naming an unrelated live pid does not block", False)


# --- sync bookkeeping --------------------------------------------------------

sync_email.save_state({"inbox": {
    "last_sync": "2026-07-30",
    "non_receipt_uids": {"1": "2026-05-01", "2": "2026-06-15", "3": ""},
}})
sync_email.reset_state("inbox", datetime.date(2026, 6, 1))
state = sync_email.load_state()["inbox"]
check("reset_state rewinds last_sync", state["last_sync"] == "2026-06-01")
check("reset_state keeps skip records from before the date", "1" in state["non_receipt_uids"])
check("reset_state drops skip records on/after the date", "2" not in state["non_receipt_uids"])
check("reset_state drops undated skip records", "3" not in state["non_receipt_uids"])

sync_email.save_state({"inbox": {"last_sync": "2026-07-30", "non_receipt_uids": ["7", "8"]}})
legacy = sync_email._load_non_receipts(sync_email.load_state()["inbox"])
check("state files predating dated skip records still load", legacy == {"7": "", "8": ""})

fresh: dict = {}
sync_email._advance_last_sync(fresh)
overlap = datetime.date.today() - datetime.timedelta(days=sync_email.SYNC_OVERLAP_DAYS)
check("last_sync is saved with an overlap window", fresh["last_sync"] == overlap.isoformat())


# --- a question nobody answers -----------------------------------------------

RAW = b"<html><body>Coffee Shop charged you $4.50</body></html>"
MESSAGE = email.message_from_string(
    "From: shop@example.com\nSubject: Your receipt\nDate: Mon, 15 Jun 2026 10:00:00 +0000\n\n"
)
IMAP_STUBS = dict(_connect=mock.DEFAULT, _select_mailbox=mock.DEFAULT,
                  _fetch_uids=mock.DEFAULT, _download_html=mock.DEFAULT)


def stub_imap(stubs: dict, uid: str) -> None:
    stubs["_select_mailbox"].return_value = True
    stubs["_fetch_uids"].return_value = [(b"1", uid)]
    stubs["_download_html"].return_value = (RAW, MESSAGE)


inbox = InboxSource(name="inbox", source_dir=Path("sources/inbox"), hint="", enrichment=False)
with mock.patch.multiple(sync_email, **IMAP_STUBS) as stubs:
    stub_imap(stubs, "42")
    with mock.patch.object(llm, "parse_text", side_effect=QuestionDeferred("Which account?", ["Expenses:Food"])):
        sync_email.ingest_receipt(inbox, "prompt", [inbox.source_dir])

saved = list(Path("sources/inbox").rglob("*_uid42.html"))
check("the email body is kept when its question goes unanswered", len(saved) == 1)
check("no sidecar is written, so it still counts as unparsed", not saved[0].with_suffix(".bean").exists())
check("the question is queued for the Questions page", len(questions_store.pending()) == 1)
check("the uid is not recorded as a non-receipt",
      "42" not in (sync_email.load_state()["inbox"].get("non_receipt_uids") or {}))
# last_sync moving past the email's date is what made this unrecoverable before:
# no later IMAP search would return uid 42 again. The on-disk body is the fix.
check("last_sync still advances past the email's date",
      sync_email.load_state()["inbox"]["last_sync"] > "2026-06-15")

questions_store.answer(questions_store.pending()[0]["id"], "Expenses:Food")
ENTRY = '2026-06-15 * "Coffee Shop" "coffee"\n  Expenses:Food  4.50 CAD\n  Assets:Checking  -4.50 CAD'
context_seen: dict[str, str] = {}


def answered_parse(text, label, prompt, *args, **kwargs):
    context_seen["extra"] = kwargs.get("extra_context", "")
    return ENTRY


with mock.patch.object(llm, "parse_text", side_effect=answered_parse):
    llm.parse_unprocessed(inbox, "prompt", [inbox.source_dir])

check("answering recovers the transaction from the kept file",
      saved[0].with_suffix(".bean").read_text().strip() == ENTRY)
check("the answer is replayed into the re-parse", "Expenses:Food" in context_seen["extra"])
check("the resolved question is cleared", not questions_store.pending())


# --- one bad message must not cost the others --------------------------------

broken = InboxSource(name="broken", source_dir=Path("sources/broken"), hint="", enrichment=False)
with mock.patch.multiple(sync_email, **IMAP_STUBS) as stubs:
    stub_imap(stubs, "99")
    with mock.patch.object(llm, "parse_text", side_effect=RuntimeError("LLM exploded")):
        found = sync_email.ingest_receipt(broken, "prompt", [broken.source_dir])

check("the body is kept when the LLM fails outright", len(list(Path("sources/broken").rglob("*_uid99.html"))) == 1)
check("a failing message does not abort the scan", found == 0)

batch = Path("sources/batch")
batch.mkdir(parents=True)
for name in ("2026-06-01_a.html", "2026-06-02_b.html", "2026-06-03_c.html"):
    (batch / name).write_text("raw")


def flaky_parse(text, label, *args, **kwargs):
    if "_b" in label.name:
        raise RuntimeError("boom")
    return ENTRY


with mock.patch.object(llm, "parse_text", side_effect=flaky_parse):
    llm.parse_unprocessed(InboxSource(name="batch", source_dir=batch, hint="", enrichment=False), "prompt", [batch])

check("files after a broken one are still parsed", (batch / "2026-06-03_c.bean").exists())
check("the broken file is left unparsed for retry", not (batch / "2026-06-02_b.bean").exists())


# --- what a re-import is allowed to delete -----------------------------------

card = Path("sources/mycard")
card.mkdir(parents=True)
(card / "2026-06-15_a_uid1.html").write_text("raw")
(card / "2026-06-15_a_uid1.bean").write_text("tx")
(card / "2026-05-01_b_uid2.html").write_text("raw")
(card / "2026-05-01_b_uid2.bean").write_text("tx")
(card / "2026-06-20_orphan.bean").write_text("hand-written, no raw file")
(card / "init.bean").write_text("; placeholder")

targets = {p.name for p in _sidecars_since(
    [EmailSource(name="mycard", source_dir=card, hint="")], datetime.date(2026, 6, 1))}
check("re-import targets regenerable sidecars in the window", targets == {"2026-06-15_a_uid1.bean"})
check("a plain date is accepted", _parse_when("2026-06-01") == datetime.date(2026, 6, 1))
check("a date and time is accepted, rounded to the day",
      _parse_when("2026-06-01T14:30") == datetime.date(2026, 6, 1))


# --- reimport-from, driven through the real CLI ------------------------------

# `init` scaffolds a config whose "mycard" source points at sources/mycard,
# the directory populated just above.
runner = CliRunner()
runner.invoke(cli.app, ["init", "."])
sync_email.save_state({"mycard": {"last_sync": "2026-07-30", "non_receipt_uids": {"5": "2026-06-20"}}})

with mock.patch.object(cli, "_run_ingest") as ingest_call:
    run = runner.invoke(cli.app, ["reimport-from", "2026-06-01T09:00", "mycard", "--reparse", "--yes"])

check("reimport-from succeeds", run.exit_code == 0)
check("it ingests the named source", ingest_call.call_args.args[0] == ["mycard"])
check("it ingests from the rewound date", ingest_call.call_args.args[2] == "2026-06-01")
check("--reparse deletes the in-window sidecar", not (card / "2026-06-15_a_uid1.bean").exists())
check("--reparse keeps the raw file it will re-parse", (card / "2026-06-15_a_uid1.html").exists())
check("--reparse leaves out-of-window sidecars alone", (card / "2026-05-01_b_uid2.bean").exists())
check("--reparse never touches a sidecar with no raw file", (card / "2026-06-20_orphan.bean").exists())
rewound = sync_email.load_state()["mycard"]
check("reimport-from rewinds the sync state", rewound["last_sync"] == "2026-06-01")
check("reimport-from clears in-window skip records", rewound["non_receipt_uids"] == {})

(card / "2026-06-15_a_uid1.bean").write_text("restored")
with mock.patch.object(cli, "_run_ingest"):
    runner.invoke(cli.app, ["reimport-from", "2026-06-01", "mycard"])
check("nothing is deleted without --reparse", (card / "2026-06-15_a_uid1.bean").exists())

with mock.patch.object(cli, "_run_ingest") as refused_call:
    runner.invoke(cli.app, ["reimport-from", "2026-06-01", "mycard", "--reparse"], input="n\n")
check("declining the delete prompt keeps the sidecar", (card / "2026-06-15_a_uid1.bean").exists())
check("declining the delete prompt skips the ingest", not refused_call.called)

check("an unknown source name is an error",
      runner.invoke(cli.app, ["reimport-from", "2026-06-01", "nosuchsource"]).exit_code != 0)
check("an unparseable date is an error",
      runner.invoke(cli.app, ["reimport-from", "june first"]).exit_code != 0)


failures = [name for name, passed in results if not passed]
print(f"\n{len(results) - len(failures)}/{len(results)} passed")
sys.exit(1 if failures else 0)
