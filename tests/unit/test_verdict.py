import itertools
from datetime import UTC, datetime

import pytest

from amlcheck.core import rules
from amlcheck.core.models import Finding, Severity, SourceResult, SourceStatus, Verdict
from amlcheck.core.verdict import decide

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def hit(rule_id: str) -> Finding:
    return rules.finding(rule_id, "test", "a hit", {}, NOW)


def source(
    status: SourceStatus, *, required: bool = True, found: tuple[str, ...] = ()
) -> SourceResult:
    findings = tuple(hit(r) for r in found)
    return SourceResult(f"src-{status}", "Source", required, status, "because", findings=findings)


def verdict_of(*results: SourceResult) -> Verdict:
    findings = [f for r in results for f in r.findings] + rules.data_gaps(results, NOW)
    return decide(findings)


def test_nothing_found_is_no_hits() -> None:
    assert decide([]) is Verdict.NO_HITS


@pytest.mark.parametrize(
    ("found", "expected"),
    [
        ([rules.FRZ_02], Verdict.REVIEW),
        ([rules.FRZ_02, rules.SYS_01], Verdict.INCOMPLETE),
        ([rules.FRZ_02, rules.SYS_01, rules.SAN_01], Verdict.BLOCK),
        ([rules.FRZ_01], Verdict.BLOCK),
    ],
)
def test_precedence_block_incomplete_review(found: list[str], expected: Verdict) -> None:
    assert decide(hit(r) for r in found) is expected


def test_sanctions_hit_blocks_although_another_source_is_down() -> None:
    """AT-09."""
    ofac = source(SourceStatus.ok, found=(rules.SAN_01,))
    eagle = source(SourceStatus.error)
    assert verdict_of(ofac, eagle) is Verdict.BLOCK


def test_stale_list_is_incomplete_unless_it_blocks() -> None:
    """AT-08."""
    assert verdict_of(source(SourceStatus.stale)) is Verdict.INCOMPLETE
    assert verdict_of(source(SourceStatus.stale, found=(rules.SAN_01,))) is Verdict.BLOCK


def test_skipped_source_and_optional_failures_are_not_gaps() -> None:
    assert verdict_of(source(SourceStatus.ok), source(SourceStatus.skipped)) is Verdict.NO_HITS
    assert verdict_of(source(SourceStatus.error, required=False)) is Verdict.NO_HITS


def test_no_hits_only_when_every_required_source_answered() -> None:
    """PRD §11: NO_HITS is impossible while any required source is not ok (or skipped)."""
    statuses = list(SourceStatus)
    for combination in itertools.product(statuses, repeat=3):
        results = [source(status) for status in combination]
        if verdict_of(*results) is Verdict.NO_HITS:
            assert all(s in (SourceStatus.ok, SourceStatus.skipped) for s in combination)


def test_overrides_change_severity_but_never_for_a_data_gap() -> None:
    findings = [hit(rules.FRZ_02), hit(rules.SYS_01)]
    changed = rules.with_overrides(findings, {rules.FRZ_02: "BLOCK", rules.SYS_01: "REVIEW"})
    assert [f.severity for f in changed] == [Severity.BLOCK, Severity.INCOMPLETE]
