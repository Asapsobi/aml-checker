"""The rules and their severities (PRD §5.2).

Sources report findings under a rule ID with its default severity; the engine applies the severity
overrides from config.toml and adds R-SYS-01 for every required source that failed or is stale.
"""

from collections.abc import Iterable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from amlcheck.core.models import Finding, Severity, SourceResult, SourceStatus

SAN_01 = "R-SAN-01"  # the address is on a sanctions list
FRZ_01 = "R-FRZ-01"  # an issuer freezes the address now, or has seized its funds
FRZ_02 = "R-FRZ-02"  # the address was frozen and later released
SYS_01 = "R-SYS-01"  # a required source errored, timed out or is out of date
EXP_01 = "R-EXP-01"  # a direct counterparty is sanctioned or frozen now
EXP_02 = "R-EXP-02"  # a large share of the USDT received came from flagged counterparties
EXP_03 = "R-EXP-03"  # a direct counterparty received USDT from a sanctioned or frozen wallet
HEU_01 = "R-HEU-01"  # the address is new (a low-priority REVIEW, Q7)
HEU_02 = "R-HEU-02"  # most of what arrives leaves again quickly (pass-through)
HEU_03 = "R-HEU-03"  # many senders of small amounts in a short window (fan-in)
HEU_04 = "R-HEU-04"  # many recipients in a short window (fan-out)
HEU_05 = "R-HEU-05"  # a counterparty the user labelled mixer, bridge or high-risk

DEFAULT_SEVERITY = {
    SAN_01: Severity.BLOCK,
    FRZ_01: Severity.BLOCK,
    FRZ_02: Severity.REVIEW,
    SYS_01: Severity.INCOMPLETE,
    EXP_01: Severity.REVIEW,  # PRD open question Q1: REVIEW, configurable to BLOCK
    EXP_02: Severity.REVIEW,
    EXP_03: Severity.REVIEW,
    HEU_01: Severity.REVIEW,
    HEU_02: Severity.REVIEW,
    HEU_03: Severity.REVIEW,
    HEU_04: Severity.REVIEW,
    HEU_05: Severity.REVIEW,
}

# "low" marks a finding as low priority without changing its severity or the verdict (Q7).
LOW_PRIORITY = "low"

# Downgrading a data gap would allow a clean result over missing data (PRD §0 rule 4).
FIXED = frozenset({SYS_01})


def finding(
    rule_id: str, source: str, summary: str, evidence: dict[str, Any], observed_at: datetime
) -> Finding:
    return Finding(rule_id, DEFAULT_SEVERITY[rule_id], source, summary, evidence, observed_at)


def with_overrides(findings: Iterable[Finding], overrides: Mapping[str, str]) -> list[Finding]:
    return [
        f
        if f.rule_id in FIXED or f.rule_id not in overrides
        else replace(f, severity=Severity(overrides[f.rule_id]))
        for f in findings
    ]


def data_gaps(results: Iterable[SourceResult], observed_at: datetime) -> list[Finding]:
    """R-SYS-01 for every required source that errored or is stale. A skipped source is not a gap:
    it has nothing to check (BEP20 USDT, docs/verification.md Q1)."""
    return [
        finding(
            SYS_01,
            r.source,
            f"{r.label} {r.status.value}: {r.summary}",
            {"status": r.status.value, "reason": r.summary},
            observed_at,
        )
        for r in results
        if r.required and r.status in (SourceStatus.error, SourceStatus.stale)
    ]
