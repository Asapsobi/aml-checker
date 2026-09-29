import csv
import hashlib
import io
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pypdf import PdfReader

from amlcheck import export
from amlcheck.adapters import eagle_virtual
from amlcheck.core import audit, rules
from amlcheck.core.address import parse
from amlcheck.core.models import CheckResult, SourceResult, SourceStatus, Verdict
from amlcheck.storage import db

NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
TRON = parse("TJwwz9NR37hjXdAV5gowj7src4avMuZZNW")
SCOPE = export.Scope("2026-09-01", "2026-09-29", None, None, "ACME Ltd")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


def make(
    n: int, verdict: Verdict = Verdict.REVIEW, credit: str | None = "Data from EV"
) -> CheckResult:
    found = rules.finding(
        rules.EXP_01, "exposure", f"received 5 USDT, check {n}", {"tx": "0xab"}, NOW
    )
    ofac = SourceResult("ofac_sdn", "OFAC SDN", True, SourceStatus.ok, "no match", NOW)
    eagle = SourceResult(
        "eagle_virtual",
        "Eagle Virtual",
        True,
        SourceStatus.ok,
        "CLEAR",
        NOW,
        evidence_meta={"verdict": "CLEAR"} | ({"credit_line": credit} if credit else {}),
    )
    return CheckResult(
        check_id=f"check-{n}",
        created_at=NOW + timedelta(minutes=n),
        address=TRON,
        verdict=verdict,
        sources=(ofac, eagle),
        findings=(found,),
        tool_version="0.1.0",
        config_hash="c" * 64,
        amount_hint="1000",
        operator_note="=cmd|' /C calc'!A0" if n == 0 else f"note {n}",
        client="ACME Ltd",
    )


def stored(conn: sqlite3.Connection, count: int = 3) -> list[audit.Stored]:
    for n in range(count):
        audit.append(conn, make(n))
    return list(audit.records(conn))


def test_json_records_can_be_hashed_again_by_anyone(conn: sqlite3.Connection) -> None:
    records = stored(conn)
    data = json.loads(json.dumps(export.to_json(records, SCOPE, audit.verify(conn), NOW)))
    assert data["audit_log"]["intact"]
    assert data["scope"]["client"] == "ACME Ltd"
    for record in data["records"]:
        # The rule in data["hash_rule"], written out without amlcheck's own code.
        def canonical(value: object) -> str:
            return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

        body = canonical(
            {
                "check": record["check"],
                "sources": sorted(record["sources"], key=canonical),
                "findings": sorted(record["findings"], key=canonical),
            }
        )
        digest = hashlib.sha256((record["prev_hash"] + body).encode()).hexdigest()
        assert digest == record["record_hash"]
    assert data["attribution"] == ["Data from EV"]


def test_csv_has_a_row_per_check_and_no_formulas(conn: sqlite3.Connection) -> None:
    out = io.StringIO()
    export.write_csv(stored(conn), out)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert [r["check_id"] for r in rows] == ["check-0", "check-1", "check-2"]
    assert rows[0]["note"] == "'=cmd|' /C calc'!A0"
    assert rows[1]["findings"] == "R-EXP-01"
    assert rows[1]["sources"] == "ofac_sdn ok; eagle_virtual ok"
    assert rows[1]["client"] == "ACME Ltd"


def test_older_records_are_credited_with_the_known_line(conn: sqlite3.Connection) -> None:
    audit.append(conn, make(0, credit=None))
    assert export.credits(list(audit.records(conn))) == [eagle_virtual.CREDIT_LINE]


def test_the_pdf_opens_and_reads_back(conn: sqlite3.Connection) -> None:
    """Phase 3 exit criterion: the export opens cleanly."""
    records = stored(conn, 40)
    pdf = export.to_pdf(records, SCOPE, audit.verify(conn), NOW)
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) > 1
    text = " ".join(page.extract_text() for page in reader.pages)
    assert "amlcheck audit export" in text
    assert "client ACME Ltd" in text
    assert "Audit log intact at export: 40 records" in text
    assert all(f"check check-{n}" in text for n in range(40))
    assert "Data from EV" in text
    assert reader.metadata is not None
    assert reader.metadata.title == "amlcheck audit export"


def test_a_broken_log_is_reported_in_the_export(conn: sqlite3.Connection) -> None:
    records = stored(conn)
    conn.execute("UPDATE checks SET verdict = 'NO_HITS' WHERE check_id = 'check-1'")
    conn.commit()
    verification = audit.verify(conn)
    assert export.to_json(records, SCOPE, verification, NOW)["audit_log"]["intact"] is False
    reader = PdfReader(io.BytesIO(export.to_pdf(records, SCOPE, verification, NOW)))
    assert "AUDIT LOG BROKEN at check check-1" in reader.pages[0].extract_text()


def test_an_empty_export_still_opens(conn: sqlite3.Connection) -> None:
    pdf = export.to_pdf([], export.Scope(None, None, None, None, None), audit.verify(conn), NOW)
    text = PdfReader(io.BytesIO(pdf)).pages[0].extract_text()
    assert "Scope: every check. 0 checks." in text


def test_scope_says_what_was_asked() -> None:
    assert SCOPE.describe() == "from 2026-09-01 to 2026-09-29, client ACME Ltd"
    only_end = replace(SCOPE, start=None, client=None, verdict="BLOCK")
    assert only_end.describe() == "from the first check to 2026-09-29, verdict BLOCK"
