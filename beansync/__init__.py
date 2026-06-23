import sys
from loguru import logger


def _formatter(record: dict) -> str:
    source = record["extra"].get("source", "")
    source_part = f"[{source}] " if source else ""
    return f"<green>{{time:HH:mm:ss}}</green> <level>{{level:<7}}</level> {source_part}{{message}}\n"


logger.remove()
logger.add(sys.stderr, format=_formatter)
