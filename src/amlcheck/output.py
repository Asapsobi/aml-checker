"""A check result for people (PRD §10.2) and as the stable JSON contract (§10.3)."""

from datetime import datetime
from typing import Any

from rich.console import Console
from rich.table import Table
from rich.text import Text

from amlcheck import graph
from amlcheck.core.clock import iso
from amlcheck.core.models import CheckResult, Finding, Severity, SourceStatus, Verdict

VERDICT_STYLE = {
    Verdict.BLOCK: "bold white on red",
    Verdict.INCOMPLETE: "bold black on yellow",
    Verdict.REVIEW: "bold black on bright_yellow",
    Verdict.NO_HITS: "bold white on green",
}
MEANING = {
    Verdict.BLOCK: "Do not transact. Escalate.",
    Verdict.INCOMPLETE: (
        "A required source failed or is out of date, so this result cannot be trusted."
        " Retry, or treat it as REVIEW."
    ),
    Verdict.REVIEW: "Review manually before transacting.",
    Verdict.NO_HITS: (
        "Nothing was found in the sources checked, as of the times shown. This is not a clearance."
    ),
}
STATUS_STYLE = {
    SourceStatus.ok: "green",
    SourceStatus.error: "red",
    SourceStatus.stale: "yellow",
    SourceStatus.skipped: "dim",
}
SEVERITY_STYLE = {
    Severity.BLOCK: "bold red",
    Severity.INCOMPLETE: "yellow",
    Severity.REVIEW: "yellow",
}


def local(moment: datetime) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %z")


def severity_text(finding: Finding) -> str:
    """The severity, with "(low)" for a low-priority finding (Q7)."""
    priority = finding.evidence.get("priority")
    return f"{finding.severity.value} ({priority})" if priority else finding.severity.value


def attributions(result: CheckResult) -> list[str]:
    return sorted({s.attribution for s in result.sources if s.attribution})


def render(result: CheckResult, console: Console) -> None:
    address = result.address
    console.print(
        Text.assemble(("Address   ", "bold"), f"{address.display}  ({address.chain.upper()})"),
        soft_wrap=True,
    )
    console.print(
        Text.assemble(("Check     ", "bold"), f"{result.check_id}  {local(result.created_at)}"),
        soft_wrap=True,
    )
    if result.client:
        console.print(Text.assemble(("Client    ", "bold"), result.client), soft_wrap=True)
    console.print(
        Text.assemble(
            ("VERDICT   ", "bold"), (f" {result.verdict} ", VERDICT_STYLE[result.verdict])
        )
    )
    console.print(MEANING[result.verdict], soft_wrap=True)

    table = Table(box=None, pad_edge=False, header_style="bold", padding=(0, 2, 0, 0))
    for column in ("Source", "Status", "As of", "Result"):
        table.add_column(column, overflow="fold")
    for s in result.sources:
        as_of = s.as_of_text or (local(s.as_of) if s.as_of else "-")
        table.add_row(s.label, Text(s.status.value, STATUS_STYLE[s.status]), as_of, s.summary)
    console.print()
    console.print(table)

    if result.findings:
        console.print()
        console.print(Text("Findings", "bold"))
        for f in result.findings:
            console.print(
                Text.assemble(
                    (f" {severity_text(f):<17}", SEVERITY_STYLE[f.severity]),
                    f"{f.rule_id}  {f.summary}",
                ),
                soft_wrap=True,
            )
    for line in attributions(result):
        console.print()
        console.print(Text(line, "dim"), soft_wrap=True)


def render_walk(result: CheckResult, console: Console) -> None:
    """The 2-hop walk, when the check had one: each counterparty in scope, and the sanctioned or
    frozen wallets that paid it (R-EXP-03)."""
    network = graph.of(result)
    if network is None:
        return
    table = Table(box=None, pad_edge=False, header_style="bold", padding=(0, 2, 0, 0))
    for column in ("Counterparty", "Received from it", "Sent to it", "Walk", "Paid by flagged"):
        table.add_column(column, overflow="fold")
    for row in graph.walk_rows(network):
        paid = "; ".join(f"{sender} {amount}" for sender, amount in row["paid_by"])
        table.add_row(
            row["address"],
            row["received"],
            row["sent"],
            row["state"],
            Text(paid or "-", "bold red" if paid else ""),
        )
    console.print()
    console.print(Text("2-hop walk", "bold"))
    console.print(table)


def to_json(result: CheckResult) -> dict[str, Any]:
    return {
        "check_id": result.check_id,
        "created_at": iso(result.created_at),
        "address": result.address.normalized,
        "chain": result.address.chain.value,
        "verdict": result.verdict.value,
        "sources": [
            {
                "source": s.source,
                "label": s.label,
                "required": s.required,
                "status": s.status.value,
                "as_of": iso(s.as_of) if s.as_of else None,
                "summary": s.summary,
                "meta": s.evidence_meta,
            }
            for s in result.sources
        ],
        "findings": [
            {
                "rule_id": f.rule_id,
                "severity": f.severity.value,
                "priority": f.evidence.get("priority"),
                "source": f.source,
                "summary": f.summary,
                "evidence": f.evidence,
                "observed_at": iso(f.observed_at),
            }
            for f in result.findings
        ],
        "amount_hint": result.amount_hint,
        "operator_note": result.operator_note,
        "client": result.client,
        "tool_version": result.tool_version,
        "config_hash": result.config_hash,
        "record_hash": result.record_hash,
        "attribution": attributions(result),
    }
