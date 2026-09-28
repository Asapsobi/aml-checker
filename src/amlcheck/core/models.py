"""Types shared by the engine, the rules and every source (PRD §5 and §7)."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Chain(StrEnum):
    tron = "tron"
    bsc = "bsc"


class Verdict(StrEnum):
    """PRD §5.1, listed in precedence order: the first one that applies wins."""

    BLOCK = "BLOCK"
    INCOMPLETE = "INCOMPLETE"
    REVIEW = "REVIEW"
    NO_HITS = "NO_HITS"


class Severity(StrEnum):
    BLOCK = "BLOCK"
    INCOMPLETE = "INCOMPLETE"
    REVIEW = "REVIEW"


class SourceStatus(StrEnum):
    ok = "ok"
    error = "error"
    stale = "stale"
    skipped = "skipped"


@dataclass(frozen=True)
class Address:
    chain: Chain
    normalized: str
    display: str


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: Severity
    source: str
    summary: str
    evidence: dict[str, Any]
    observed_at: datetime


@dataclass(frozen=True)
class SourceResult:
    source: str
    label: str
    required: bool
    status: SourceStatus
    summary: str
    as_of: datetime | None = None
    as_of_text: str | None = None
    findings: tuple[Finding, ...] = ()
    evidence_meta: dict[str, Any] = field(default_factory=dict)
    attribution: str | None = None


@dataclass(frozen=True)
class SourceHealth:
    source: str
    label: str
    status: SourceStatus
    detail: str
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    created_at: datetime
    address: Address
    verdict: Verdict
    sources: tuple[SourceResult, ...]
    findings: tuple[Finding, ...]
    tool_version: str
    config_hash: str
    amount_hint: str | None = None
    operator_note: str | None = None
    record_hash: str | None = None
