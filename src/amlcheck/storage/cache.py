"""A short-lived cache of successful API answers (PRD §11). Errors are never stored."""

import json
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from amlcheck.core.clock import from_iso, iso, utcnow


class ResponseCache:
    def __init__(
        self, conn: sqlite3.Connection, ttl_seconds: int, now: Callable[[], datetime] = utcnow
    ) -> None:
        self._conn = conn
        self._ttl = ttl_seconds
        self._now = now

    def get(self, key: str) -> Any:
        """The value stored under `key` if it is younger than the TTL, else None."""
        if self._ttl <= 0:
            return None
        row = self._conn.execute(
            "SELECT response_json, fetched_at, ttl_s FROM http_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None or from_iso(row[1]) + timedelta(seconds=row[2]) <= self._now():
            return None
        return json.loads(row[0])

    def put(self, key: str, source: str, value: Any) -> None:
        if self._ttl <= 0:
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO http_cache (key, source, response_json, fetched_at, ttl_s)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (key) DO UPDATE SET"
                " source = excluded.source, response_json = excluded.response_json,"
                " fetched_at = excluded.fetched_at, ttl_s = excluded.ttl_s",
                (key, source, json.dumps(value), iso(self._now()), self._ttl),
            )
