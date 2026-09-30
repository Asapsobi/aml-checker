"""Audit export (PRD §10.1 `amlcheck audit export`, U5): the checks in a date range, for a
client, an address or a verdict, as CSV, JSON or PDF.

JSON holds every record exactly as stored, with its hashes, so anyone can recompute a record's
hash: sha256(prev_hash + canonical JSON of {"check", "sources", "findings"}), with keys sorted, no
spaces, and the sources and findings each sorted by their own canonical JSON (core/audit.py). CSV
has one row per check, and PDF a readable report. Both JSON and PDF say whether the whole audit log
verified at export time.
"""

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from amlcheck import __version__, vendor
from amlcheck.adapters import bsc, eagle_virtual, exposure, ofac, tron, two_hop
from amlcheck.core.audit import Stored, Verification
from amlcheck.core.clock import iso
from amlcheck.inputs import safe_cell

SOURCE_LABELS = {
    ofac.SOURCE: ofac.LABEL,
    eagle_virtual.SOURCE: eagle_virtual.LABEL,
    tron.SOURCE: tron.LABEL,
    "bsc_usdt": bsc.BscUsdtAdapter.label,
    exposure.SOURCE: exposure.ExposureAdapter.label,
    two_hop.SOURCE: two_hop.LABEL,
}
SEVERITY_RANK = {"BLOCK": 0, "INCOMPLETE": 1, "REVIEW": 2}
CSV_COLUMNS = (
    "created_at",
    "check_id",
    "address",
    "chain",
    "verdict",
    "client",
    "amount",
    "note",
    "findings",
    "summary",
    "sources",
    "tool_version",
    "config_hash",
    "record_hash",
)
HASH_RULE = (
    "record_hash = sha256(prev_hash + canonical JSON of {check, sources, findings}): keys sorted,"
    " no spaces, sources and findings each sorted by their own canonical JSON"
)


@dataclass(frozen=True)
class Scope:
    """What an export covers, as the operator asked for it."""

    start: str | None  # YYYY-MM-DD, inclusive
    end: str | None  # YYYY-MM-DD, inclusive
    address: str | None
    verdict: str | None
    client: str | None

    def describe(self) -> str:
        parts = []
        if self.start or self.end:
            parts.append(f"from {self.start or 'the first check'} to {self.end or 'today'}")
        parts += [f"{name} {value}" for name, value in self._named() if value]
        return ", ".join(parts) or "every check"

    def _named(self) -> list[tuple[str, str | None]]:
        return [("address", self.address), ("verdict", self.verdict), ("client", self.client)]

    def to_json(self) -> dict[str, str | None]:
        return {"from": self.start, "to": self.end, **dict(self._named())}


def credits(records: Sequence[Stored]) -> list[str]:
    """The credit lines the data in these records calls for (V4)."""
    lines = set()
    for record in records:
        for source in record.sources:
            if source["source"] == eagle_virtual.SOURCE and source["status"] != "error":
                line = eagle_virtual.credit_line(json.loads(source["evidence_meta_json"] or "{}"))
                if line:
                    lines.add(line)
    return sorted(lines)


def source_label(source: str, meta: Any) -> str:
    """How a stored source is named, as it was when the check was made."""
    if source == vendor.SOURCE:
        return vendor.label(meta.get("vendor") if isinstance(meta, dict) else None)
    return SOURCE_LABELS.get(source) or source


def _top(record: Stored) -> dict[str, Any] | None:
    return min(record.findings, key=lambda f: SEVERITY_RANK.get(f["severity"], 9), default=None)


def _rule_ids(record: Stored) -> str:
    ordered = sorted(record.findings, key=lambda f: SEVERITY_RANK.get(f["severity"], 9))
    return " ".join(dict.fromkeys(f["rule_id"] for f in ordered))


def write_csv(records: Sequence[Stored], out: IO[str]) -> None:
    writer = csv.DictWriter(out, CSV_COLUMNS)
    writer.writeheader()
    for record in records:
        check = record.check
        top = _top(record)
        row = {
            "created_at": check["created_at"],
            "check_id": check["check_id"],
            "address": check["address_norm"],
            "chain": check["chain"],
            "verdict": check["verdict"],
            "client": check.get("client") or "",
            "amount": check["amount_hint"] or "",
            "note": check["operator_note"] or "",
            "findings": _rule_ids(record),
            "summary": top["summary"] if top else "",
            "sources": "; ".join(f"{s['source']} {s['status']}" for s in record.sources),
            "tool_version": check["tool_version"],
            "config_hash": check["config_hash"],
            "record_hash": record.record_hash,
        }
        writer.writerow({key: safe_cell(value or "") for key, value in row.items()})


def to_json(
    records: Sequence[Stored], scope: Scope, verification: Verification, now: datetime
) -> dict[str, Any]:
    return {
        "exported_at": iso(now),
        "tool_version": __version__,
        "scope": scope.to_json(),
        "audit_log": {
            "intact": verification.intact,
            "records": verification.records,
            "head": verification.head,
            "broken_check_id": verification.broken_check_id,
        },
        "hash_rule": HASH_RULE,
        "records": [
            {
                "seq": r.seq,
                "check": r.check,
                "sources": r.sources,
                "findings": r.findings,
                "prev_hash": r.prev_hash,
                "record_hash": r.record_hash,
            }
            for r in records
        ],
        "attribution": credits(records),
    }


def _local(text: str | None) -> str:
    if not text:
        return "-"
    return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M %z")


def to_pdf(
    records: Sequence[Stored], scope: Scope, verification: Verification, now: datetime
) -> bytes:
    """A report for people. Text is set in Helvetica, which covers Latin scripts only."""
    styles = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=styles["BodyText"])
    small = ParagraphStyle("small", parent=body, fontSize=7.5, leading=9.5)
    cell = ParagraphStyle("cell", parent=body, fontSize=8, leading=10)

    def para(text: str, style: ParagraphStyle = body) -> Paragraph:
        return Paragraph(escape(text), style)

    if verification.intact:
        integrity = (
            f"Audit log intact at export: {verification.records:,} records, latest hash"
            f" {verification.head}."
        )
    else:
        integrity = (
            f"AUDIT LOG BROKEN at check {verification.broken_check_id}: its records after that"
            " point cannot be trusted."
        )
    story: list[Any] = [
        Paragraph("amlcheck audit export", styles["Title"]),
        para(f"Scope: {scope.describe()}. {len(records):,} checks."),
        para(f"Exported {now.astimezone():%Y-%m-%d %H:%M %z} by amlcheck {__version__}."),
        para(integrity),
        para(
            "Decision support, not a legal determination: NO_HITS means nothing was found in"
            " the sources checked, as of their times, and is not a clearance."
        ),
        Spacer(1, 4 * mm),
    ]
    for record in records:
        check = record.check
        head = (
            f"{_local(check['created_at'])}  ·  {check['verdict']}  ·  {check['chain'].upper()}"
            f"  ·  {check['address_norm']}"
        )
        details = [
            f"{name} {value}"
            for name, value in (
                ("client", check.get("client")),
                ("amount", check["amount_hint"]),
                ("note:", check["operator_note"]),
            )
            if value
        ]
        rows = [["Source", "Status", "As of", "Result"]] + [
            [
                para(SOURCE_LABELS.get(s["source"]) or str(s["source"]), cell),
                para(s["status"], cell),
                para(_local(s["as_of"]), cell),
                para(s["summary"] or "", cell),
            ]
            for s in record.sources
        ]
        table = Table(rows, colWidths=[30 * mm, 15 * mm, 37 * mm, 88 * mm], repeatRows=1)
        table.setStyle(
            TableStyle(
                [
                    ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey),
                ]
            )
        )
        block: list[Any] = [Paragraph(f"<b>{escape(head)}</b>", body)]
        if details:
            block.append(para("  ·  ".join(details)))
        block.append(table)
        for f in sorted(record.findings, key=lambda f: SEVERITY_RANK.get(f["severity"], 9)):
            block.append(para(f"{f['severity']}  {f['rule_id']}  {f['summary']}", cell))
        block.append(para(f"check {check['check_id']}  ·  record hash {record.record_hash}", small))
        block.append(Spacer(1, 4 * mm))
        story.append(KeepTogether(block))
    story += [para(line, small) for line in credits(records)]

    def footer(canvas: Any, document: Any) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.drawString(20 * mm, 10 * mm, f"amlcheck audit export  ·  page {document.page}")
        canvas.restoreState()

    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="amlcheck audit export",
        author=f"amlcheck {__version__}",
    )
    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()
