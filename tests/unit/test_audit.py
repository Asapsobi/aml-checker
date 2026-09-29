import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from amlcheck.core import audit, rules
from amlcheck.core.models import (
    Address,
    Chain,
    CheckResult,
    SourceResult,
    SourceStatus,
    Verdict,
)
from amlcheck.storage import db

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
MakeResult = Callable[..., CheckResult]


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


@pytest.fixture
def make_result() -> MakeResult:
    counter = iter(range(1000))

    def make(verdict: Verdict = Verdict.NO_HITS) -> CheckResult:
        n = next(counter)
        found = rules.finding(rules.SAN_01, "ofac_sdn", "listed", {"list_entry_id": "27307"}, NOW)
        ofac = SourceResult(
            "ofac_sdn", "OFAC SDN", True, SourceStatus.ok, "match", NOW, None, (found,), {"a": 1}
        )
        return CheckResult(
            check_id=f"check-{n}",
            created_at=NOW,
            address=Address(Chain.tron, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "same"),
            verdict=verdict,
            sources=(ofac,),
            findings=(found,),
            tool_version="0.1.0",
            config_hash="c" * 64,
            amount_hint="50000",
            operator_note=f"note {n}",
        )

    return make


def test_records_chain_from_genesis(conn: sqlite3.Connection, make_result: MakeResult) -> None:
    first = audit.append(conn, make_result())
    second = audit.append(conn, make_result())
    rows = conn.execute("SELECT prev_hash, record_hash FROM checks ORDER BY seq").fetchall()
    assert rows == [(audit.GENESIS, first), (first, second)]
    report = audit.verify(conn)
    assert report.intact
    assert (report.records, report.head) == (2, second)


def test_a_client_is_hashed_only_when_set(
    conn: sqlite3.Connection, make_result: MakeResult
) -> None:
    without = make_result()
    assert "client" not in audit.to_rows(without)[0]
    audit.append(conn, without)
    audit.append(conn, replace(make_result(), client="ACME Ltd"))
    assert audit.verify(conn).intact
    conn.execute("UPDATE checks SET client = 'Other' WHERE check_id = 'check-1'")
    assert audit.verify(conn).broken_check_id == "check-1"
    conn.execute("UPDATE checks SET client = 'ACME Ltd' WHERE check_id = 'check-1'")
    conn.execute("UPDATE checks SET client = 'ACME Ltd' WHERE check_id = 'check-0'")
    assert audit.verify(conn).broken_check_id == "check-0"


def test_records_written_before_the_client_column_still_verify(
    tmp_path: Path, make_result: MakeResult
) -> None:
    """A log from before Phase 3 (schema v2) keeps verifying once migrated."""
    with closing(sqlite3.connect(tmp_path / "old.db")) as old:
        db.migrate(old, db.migrations()[:2])
        result = make_result()
        check, sources, findings = audit.to_rows(result)
        record = audit.record_hash(audit.GENESIS, check, sources, findings)
        old.execute(
            "INSERT INTO checks (check_id, created_at, address_norm, chain, verdict, amount_hint,"
            " operator_note, tool_version, config_hash, prev_hash, record_hash)"
            " VALUES (:check_id, :created_at, :address_norm, :chain, :verdict, :amount_hint,"
            " :operator_note, :tool_version, :config_hash, :prev_hash, :record_hash)",
            {**check, "prev_hash": audit.GENESIS, "record_hash": record},
        )
        old.executemany(
            "INSERT INTO check_sources (check_id, source, required, status, as_of, summary,"
            " evidence_meta_json) VALUES (:check_id, :source, :required, :status, :as_of,"
            " :summary, :evidence_meta_json)",
            [{**s, "check_id": result.check_id} for s in sources],
        )
        old.executemany(
            "INSERT INTO check_findings (check_id, rule_id, severity, source, summary,"
            " evidence_json, observed_at) VALUES (:check_id, :rule_id, :severity, :source,"
            " :summary, :evidence_json, :observed_at)",
            [{**f, "check_id": result.check_id} for f in findings],
        )
        old.commit()
    with closing(db.connect(tmp_path / "old.db")) as upgraded:
        assert db.schema_version(upgraded) == len(db.migrations())
        audit.append(upgraded, replace(make_result(), client="ACME Ltd"))
        assert audit.verify(upgraded) == audit.Verification(
            2, upgraded.execute("SELECT record_hash FROM checks WHERE seq = 2").fetchone()[0]
        )


def test_empty_log_verifies() -> None:
    with closing(sqlite3.connect(":memory:")) as memory:
        db.migrate(memory)
        assert audit.verify(memory) == audit.Verification(0, audit.GENESIS)


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE checks SET verdict = 'NO_HITS' WHERE seq = 2",
        "UPDATE check_findings SET severity = 'REVIEW' WHERE check_id = 'check-1'",
        "UPDATE check_sources SET summary = 'no match' WHERE check_id = 'check-1'",
        "DELETE FROM check_findings WHERE check_id = 'check-1'",
    ],
)
def test_tampering_is_reported_at_that_record(
    conn: sqlite3.Connection, make_result: MakeResult, tamper: str
) -> None:
    """AT-11."""
    for _ in range(3):
        audit.append(conn, make_result(Verdict.BLOCK))
    conn.execute(tamper)
    conn.commit()
    report = audit.verify(conn)
    assert (report.broken_seq, report.broken_check_id) == (2, "check-1")
    assert "changed" in str(report.reason)


def test_removed_record_breaks_the_link(conn: sqlite3.Connection, make_result: MakeResult) -> None:
    for _ in range(3):
        audit.append(conn, make_result())
    # Foreign keys refuse to orphan a record's sources and findings, so remove those first.
    conn.execute("DELETE FROM check_sources WHERE check_id = 'check-1'")
    conn.execute("DELETE FROM check_findings WHERE check_id = 'check-1'")
    conn.execute("DELETE FROM checks WHERE seq = 2")
    conn.commit()
    report = audit.verify(conn)
    assert (report.broken_seq, report.records) == (3, 1)
    assert "removed or inserted" in str(report.reason)


def test_failed_write_leaves_no_partial_record(
    conn: sqlite3.Connection, make_result: MakeResult
) -> None:
    result = make_result()
    audit.append(conn, result)
    with pytest.raises(sqlite3.IntegrityError):
        audit.append(conn, result)  # same check_id again
    assert conn.execute("SELECT COUNT(*) FROM check_sources").fetchone()[0] == 1
    assert audit.verify(conn).intact
