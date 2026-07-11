import json
import time
import uuid
from pathlib import Path

from loguru import logger  # type: ignore[import-not-found]

QUESTIONS_FILE = Path("sources/state/questions.json")


class QuestionDeferred(Exception):
    """Raised by llm.ask_user() when called with no interactive terminal watching.

    Carries the question so the caller can persist it for the user to answer
    later from the Questions page, instead of blocking forever on stdin.
    """

    def __init__(self, question: str, options: list[str] | None = None) -> None:
        super().__init__(question)
        self.question = question
        self.options = options


def _load() -> list[dict]:
    if QUESTIONS_FILE.exists():
        return json.loads(QUESTIONS_FILE.read_text())
    return []


def _save(items: list[dict]) -> None:
    QUESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    QUESTIONS_FILE.write_text(json.dumps(items, indent=2))


def queue_question(source_name: str, source_file: Path, question: str, options: list[str] | None) -> None:
    """Persist a deferred question. Skips if an identical one is already pending."""
    items = _load()
    key = (source_name, str(source_file), question)
    if any((q["source_name"], q["source_file"], q["question"]) == key and q.get("answer") is None for q in items):
        return
    items.append({
        "id": uuid.uuid4().hex[:8],
        "source_name": source_name,
        "source_file": str(source_file),
        "question": question,
        "options": options,
        "asked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "answer": None,
        "answered_at": None,
    })
    _save(items)
    logger.warning("   Question deferred (no one watching): {}", question)


def pending() -> list[dict]:
    return [q for q in _load() if q.get("answer") is None]


def answer(question_id: str, answer_text: str) -> None:
    items = _load()
    for q in items:
        if q["id"] == question_id:
            q["answer"] = answer_text
            q["answered_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _save(items)


def discard(question_id: str) -> None:
    _save([q for q in _load() if q["id"] != question_id])


def answered_context_for(source_file: Path) -> str:
    """Previously-answered Q&A for this exact file, to inject into a re-parse prompt."""
    matches = [q for q in _load() if q["source_file"] == str(source_file) and q.get("answer") is not None]
    if not matches:
        return ""
    body = "\n".join(f"- Q: {q['question']}\n  A: {q['answer']}" for q in matches)
    return (
        "The user has already answered the following question(s) about this exact "
        "transaction in a previous run — use these answers directly, do not ask again:\n"
        f"{body}\n\n"
    )


def clear_answered_for(source_file: Path) -> None:
    """Drop resolved Q&A for a file once it has been successfully parsed."""
    _save([q for q in _load() if not (q["source_file"] == str(source_file) and q.get("answer") is not None)])
