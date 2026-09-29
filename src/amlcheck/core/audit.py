"""The audit log: append-only and hash-chained (PRD §9 "Audit integrity").

record_hash = sha256(prev_hash + canonical_json(check + sources + findings)). Nothing in the code
updates or deletes a record. A check's client (Phase 3, Q13) is hashed only when it is set, so the
records written before it existed hash exactly as they did.

`verify` recomputes every hash in order and names the first record that no longer matches, which
catches a changed, removed or inserted record. Cutting records off the end cannot be seen from
inside the file, so `verify` also reports the latest hash to keep.
"""

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from amlcheck.core.clock import iso
from amlcheck.core.models import CheckResult

GENESIS = "0" * 64

CHECK_FIELDS = (
    "check_id",
    "created_at",
    "address_norm",
    "chain",
    "verdict",
    "amount_hint",
    "operator_note",
    "tool_version",
    "config_hash",
)
SOURCE_FIELDS = ("source", "required", "status", "as_of", "summary", "evidence_meta_json")
FINDING_FIELDS = ("rule_id", "severity", "source", "summary", "evidence_json", "observed_at")

Row = dict[str, Any]


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def record_hash(prev_hash: str, check: Row, sources: list[Row], findings: list[Row]) -> str:
    body = canonical_json(
        {
            "check": check,
            "sources": sorted(sources, key=canonical_json),
            "findings": sorted(findings, key=canonical_json),
        }
    )
    return hashlib.sha256((prev_hash + body).encode()).hexdigest()


def to_rows(result: CheckResult) -> tuple[Row, list[Row], list[Row]]:
    """The values exactly as they are stored, so that `verify` can hash them again."""
    check = {
        "check_id": result.check_id,
        "created_at": iso(result.created_at),
        "address_norm": result.address.normalized,
        "chain": result.address.chain.value,
        "verdict": result.verdict.value,
        "amount_hint": result.amount_hint,
        "operator_note": result.operator_note,
        "tool_version": result.tool_version,
        "config_hash": result.config_hash,
    }
    if result.client is not None:
        check["client"] = result.client
    sources = [
        {
            "source": s.source,
            "required": int(s.required),
            "status": s.status.value,
            "as_of": iso(s.as_of) if s.as_of else None,
            "summary": s.summary,
            "evidence_meta_json": canonical_json(s.evidence_meta),
        }
        for s in result.sources
    ]
    findings = [
        {
            "rule_id": f.rule_id,
            "severity": f.severity.value,
            "source": f.source,
            "summary": f.summary,
            "evidence_json": canonical_json(f.evidence),
            "observed_at": iso(f.observed_at),
        }
        for f in result.findings
    ]
    return check, sources, findings


def append(conn: sqlite3.Connection, result: CheckResult) -> str:
    """Write the check with its sources and findings as one record; return its hash."""
    check, sources, findings = to_rows(result)
    conn.execute("BEGIN IMMEDIATE")  # take the write lock before reading the previous hash
    try:
        last = conn.execute("SELECT record_hash FROM checks ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = last[0] if last else GENESIS
        new_hash = record_hash(prev_hash, check, sources, findings)
        conn.execute(
            "INSERT INTO checks (check_id, created_at, address_norm, chain, verdict, amount_hint,"
            " operator_note, tool_version, config_hash, client, prev_hash, record_hash)"
            " VALUES (:check_id, :created_at, :address_norm, :chain, :verdict, :amount_hint,"
            " :operator_note, :tool_version, :config_hash, :client, :prev_hash, :record_hash)",
            {**check, "client": result.client, "prev_hash": prev_hash, "record_hash": new_hash},
        )
        conn.executemany(
            "INSERT INTO check_sources (check_id, source, required, status, as_of, summary,"
            " evidence_meta_json) VALUES (:check_id, :source, :required, :status, :as_of,"
            " :summary, :evidence_meta_json)",
            [{**s, "check_id": result.check_id} for s in sources],
        )
        conn.executemany(
            "INSERT INTO check_findings (check_id, rule_id, severity, source, summary,"
            " evidence_json, observed_at) VALUES (:check_id, :rule_id, :severity, :source,"
            " :summary, :evidence_json, :observed_at)",
            [{**f, "check_id": result.check_id} for f in findings],
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return new_hash


@dataclass(frozen=True)
class Stored:
    """One record exactly as stored, in the form its hash covers."""

    seq: int
    check: Row
    sources: list[Row]
    findings: list[Row]
    prev_hash: str
    record_hash: str

    def recomputed(self) -> str:
        return record_hash(self.prev_hash, self.check, self.sources, self.findings)


def records(
    conn: sqlite3.Connection,
    *,
    start: str | None = None,
    end: str | None = None,
    address: str | None = None,
    verdict: str | None = None,
    client: str | None = None,
) -> Iterator[Stored]:
    """Stored records, oldest first: all of them, or those created in [start, end) (ISO times)
    for an address, a verdict or a client (ignoring case)."""
    query = conn.execute(
        "SELECT seq, check_id, created_at, address_norm, chain, verdict, amount_hint,"
        " operator_note, tool_version, config_hash, client, prev_hash, record_hash FROM checks"
        " WHERE (:start IS NULL OR created_at >= :start) AND (:end IS NULL OR created_at < :end)"
        " AND (:address IS NULL OR address_norm = :address)"
        " AND (:verdict IS NULL OR verdict = :verdict)"
        " AND (:client IS NULL OR client = :client COLLATE NOCASE) ORDER BY seq",
        {"start": start, "end": end, "address": address, "verdict": verdict, "client": client},
    )
    for seq, *values, client_name, prev_hash, stored_hash in query.fetchall():
        check = dict(zip(CHECK_FIELDS, values, strict=True))
        if client_name is not None:
            check["client"] = client_name
        sources = [
            dict(zip(SOURCE_FIELDS, row, strict=True))
            for row in conn.execute(
                "SELECT source, required, status, as_of, summary, evidence_meta_json"
                " FROM check_sources WHERE check_id = ?",
                (check["check_id"],),
            )
        ]
        findings = [
            dict(zip(FINDING_FIELDS, row, strict=True))
            for row in conn.execute(
                "SELECT rule_id, severity, source, summary, evidence_json, observed_at"
                " FROM check_findings WHERE check_id = ?",
                (check["check_id"],),
            )
        ]
        yield Stored(seq, check, sources, findings, prev_hash, stored_hash)


@dataclass(frozen=True)
class Verification:
    records: int
    head: str
    broken_seq: int | None = None
    broken_check_id: str | None = None
    reason: str | None = None

    @property
    def intact(self) -> bool:
        return self.broken_seq is None


def verify(conn: sqlite3.Connection) -> Verification:
    expected_prev = GENESIS
    count = 0
    for record in records(conn):
        check_id = record.check["check_id"]
        if record.prev_hash != expected_prev:
            reason = "its link to the record before it is broken: a record was removed or inserted"
            return Verification(count, expected_prev, record.seq, check_id, reason)
        if record.recomputed() != record.record_hash:
            reason = "its contents changed after it was written"
            return Verification(count, expected_prev, record.seq, check_id, reason)
        expected_prev = record.record_hash
        count += 1
    return Verification(count, expected_prev)
