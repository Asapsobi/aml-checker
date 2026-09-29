"""Structured JSON logs in ~/.amlcheck/logs/ (PRD §11): rotated, local only. Nothing logs a key:
keys only ever travel in request headers, which are never logged."""

import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from amlcheck.core.clock import iso


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": iso(datetime.fromtimestamp(record.created, UTC)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **getattr(record, "fields", {}),
        }
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup(home: Path) -> None:
    """Send the `amlcheck` loggers to home/logs/amlcheck.log, replacing an earlier destination."""
    path = (home / "logs" / "amlcheck.log").resolve()
    logger = logging.getLogger("amlcheck")
    for handler in list(logger.handlers):
        if isinstance(handler, RotatingFileHandler) and Path(handler.baseFilename) == path:
            return
        logger.removeHandler(handler)
        handler.close()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    # httpx logs every request URL at INFO, and Etherscan's URLs carry the key: keep it quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)
