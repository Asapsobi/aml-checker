import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from amlcheck.storage import db

PRD_TABLES = {
    "list_snapshots",
    "sanctioned_addresses",
    "issuer_events",
    "index_state",
    "labels",
    "http_cache",
    "checks",
    "check_sources",
    "check_findings",
    "watchlist",
}
# Beyond PRD §9: the local API's idempotency keys (Phase 5).
LATER_TABLES = {"api_requests"}


def tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_new_database_gets_the_prd_schema(tmp_path: Path) -> None:
    with closing(db.connect(tmp_path / "new" / "amlcheck.db")) as conn:
        assert tables(conn) == PRD_TABLES | LATER_TABLES
        assert db.schema_version(conn) == len(db.migrations())
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_reopening_migrates_nothing(tmp_path: Path) -> None:
    path = tmp_path / "amlcheck.db"
    with closing(db.connect(path)) as conn:
        version = db.schema_version(conn)
    with closing(db.connect(path)) as conn:
        assert db.migrate(conn) == version


def test_failed_migration_leaves_no_trace(tmp_path: Path) -> None:
    broken = [(1, "CREATE TABLE half_done (x INTEGER);\nINSERT INTO no_such_table VALUES (1);")]
    with closing(sqlite3.connect(tmp_path / "amlcheck.db")) as conn:
        with pytest.raises(sqlite3.OperationalError):
            db.migrate(conn, broken)
        assert db.schema_version(conn) == 0
        assert "half_done" not in tables(conn)


def test_database_from_a_newer_amlcheck_is_refused(tmp_path: Path) -> None:
    with closing(sqlite3.connect(tmp_path / "amlcheck.db")) as conn:
        conn.execute("PRAGMA user_version = 99")
        with pytest.raises(RuntimeError, match="newer"):
            db.migrate(conn)


def test_checks_only_accept_prd_verdicts(tmp_path: Path) -> None:
    with (
        closing(db.connect(tmp_path / "amlcheck.db")) as conn,
        pytest.raises(sqlite3.IntegrityError),
    ):
        conn.execute(
            "INSERT INTO checks (check_id, created_at, address_norm, chain, verdict,"
            " tool_version, config_hash, prev_hash, record_hash)"
            " VALUES ('c1', '2026-09-28T00:00:00Z', 'T1', 'tron', 'CLEAR', '0.1.0', 'h', '', 'r')"
        )


def test_findings_must_belong_to_a_check(tmp_path: Path) -> None:
    with (
        closing(db.connect(tmp_path / "amlcheck.db")) as conn,
        pytest.raises(sqlite3.IntegrityError),
    ):
        conn.execute(
            "INSERT INTO check_findings (check_id, rule_id, severity, source, evidence_json,"
            " observed_at) VALUES ('missing', 'R-SAN-01', 'BLOCK', 'ofac', '{}', '2026-09-28')"
        )
