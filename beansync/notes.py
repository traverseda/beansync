import json
import re
from pathlib import Path

from loguru import logger

NOTES_FILE = Path(".llm_notes.json")
_notes: dict[str, str] = json.loads(NOTES_FILE.read_text()) if NOTES_FILE.exists() else {}


def save_note(key: str, value: str) -> str:
    try:
        re.compile(key, re.IGNORECASE)
    except re.error as e:
        return f"Invalid regex pattern: {e}"
    action = "Updated" if key in _notes else "Saved"
    _notes[key] = value
    NOTES_FILE.write_text(json.dumps(_notes, indent=2))
    logger.info("   Note {}: {}", action.lower(), key)
    return f"{action}."


def delete_note(key: str) -> str:
    if key not in _notes:
        return f"No note found for key: {key!r}"
    del _notes[key]
    NOTES_FILE.write_text(json.dumps(_notes, indent=2))
    logger.info("   Note deleted: {}", key)
    return "Deleted."


def match_notes(text: str) -> dict[str, str]:
    matched = {}
    for pattern, value in _notes.items():
        try:
            if re.search(pattern, text, re.IGNORECASE):
                matched[pattern] = value
        except re.error:
            pass
    return matched


def get_notes_context(matched: dict[str, str]) -> str:
    if not matched:
        return ""
    body = "\n".join(f"- {k}: {v}" for k, v in matched.items())
    return f"Relevant notes (use these directly — do not search for merchants listed here):\n{body}\n\n"
