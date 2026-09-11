"""Structured logging. API keys and secrets must never be logged."""

from __future__ import annotations

import logging
import re

_SENSITIVE_KEYS = re.compile(r"(api_?key|token|secret|password)", re.IGNORECASE)


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = str(record.getMessage())
        except Exception:
            return True
        if _SENSITIVE_KEYS.search(msg):
            record.msg = "[filtered: message contained sensitive keyword]"
        return True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers and not logger.propagate:
        logger.addHandler(logging.NullHandler())
    return logger


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(SecretFilter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
