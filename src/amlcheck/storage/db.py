"""One local SQLite file (PRD §8), its schema managed by numbered SQL migrations."""

import re
import sqlite3
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

_MIGRATION_FILE = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def connect(path: Path) -> sqlite3.Connection:
    """Open the database, creating it if needed, and bring its schema up to date."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrations() -> list[tuple[int, str]]:
    """Every bundled migration as (version, sql), numbered 1, 2, 3... without gaps."""
    found = []
    for item in resources.files("amlcheck.storage").joinpath("migrations").iterdir():
        match = _MIGRATION_FILE.match(item.name)
        if match:
            found.append((int(match.group(1)), item.read_text(encoding="utf-8")))
    found.sort()
    versions = [version for version, _ in found]
    if versions != list(range(1, len(found) + 1)):
        raise RuntimeError(f"migrations must be numbered 0001, 0002, ... without gaps: {versions}")
    return found


def schema_version(conn: sqlite3.Connection) -> int:
    version: int = conn.execute("PRAGMA user_version").fetchone()[0]
    return version


def migrate(conn: sqlite3.Connection, available: Sequence[tuple[int, str]] | None = None) -> int:
    """Apply pending migrations, each in its own transaction, and return the schema version."""
    available = migrations() if available is None else available
    current = schema_version(conn)
    latest = available[-1][0] if available else 0
    if current > latest:
        raise RuntimeError(
            f"database schema v{current} is newer than this amlcheck understands (v{latest})"
        )
    for version, sql in available:
        if version <= current:
            continue
        try:
            conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error:
            conn.rollback()
            raise
        current = version
    return current
