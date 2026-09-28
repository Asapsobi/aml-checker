"""The rules and their severities (PRD §5.2). Phase 1: sanctions, issuer freezes and data gaps.

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

DEFAULT_SEVERITY = {
    SAN_01: Severity.BLOCK,
    FRZ_01: Severity.BLOCK,
    FRZ_02: Severity.REVIEW,
    SYS_01: Severity.INCOMPLETE,
}

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
