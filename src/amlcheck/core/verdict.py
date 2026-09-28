"""The verdict (PRD §5.1): BLOCK > INCOMPLETE > REVIEW > NO_HITS.

A sanctions hit still blocks when another source failed, and NO_HITS needs no finding at all, which
R-SYS-01 guarantees whenever a required source is missing or out of date.
"""

from collections.abc import Iterable

from amlcheck.core.models import Finding, Severity, Verdict

_PRECEDENCE = (Severity.BLOCK, Severity.INCOMPLETE, Severity.REVIEW)


def decide(findings: Iterable[Finding]) -> Verdict:
    severities = {f.severity for f in findings}
    for severity in _PRECEDENCE:
        if severity in severities:
            return Verdict(severity.value)
    return Verdict.NO_HITS
