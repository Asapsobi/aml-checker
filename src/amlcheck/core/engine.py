"""One check from start to finish (PRD §7): every source at once, the rules, the verdict, and the
audit record, which is written before anything is shown (PRD §0 rule 5)."""

import asyncio
import logging
import sqlite3
import uuid
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import datetime

from amlcheck import __version__
from amlcheck.adapters.base import SourceAdapter, failed
from amlcheck.config import Config
from amlcheck.core import audit, rules
from amlcheck.core.clock import utcnow
from amlcheck.core.models import Address, CheckResult, Finding, SourceResult, Verdict
from amlcheck.core.verdict import decide

log = logging.getLogger(__name__)


async def run_source(source: SourceAdapter, address: Address, timeout: float) -> SourceResult:
    """A source that breaks or hangs becomes an `error`, and so an INCOMPLETE verdict: a failure
    must never crash the check or pass as a clean result. A source that needs longer than the
    default says so in its own `timeout` (the 2-hop walk)."""
    limit = getattr(source, "timeout", None) or timeout
    try:
        return await asyncio.wait_for(source.check(address), limit)
    except TimeoutError:
        return failed(source, f"no answer within {limit:g} seconds")
    except Exception as e:
        log.exception("source %s failed", source.source)
        return failed(source, f"{type(e).__name__}: {e}")


async def screen(
    address: Address,
    sources: Sequence[SourceAdapter],
    *,
    conn: sqlite3.Connection,
    config: Config,
    amount: str | None = None,
    note: str | None = None,
    client: str | None = None,
    timeout: float = 60.0,
    now: Callable[[], datetime] = utcnow,
    then: Callable[[Verdict], Sequence[SourceAdapter]] | None = None,
) -> CheckResult:
    """Run every source at once, apply the rules, and write the record. `then` names sources to
    run once the verdict is known: the paid vendor, asked only for some verdicts (PRD Phase 4)."""
    results = tuple(await asyncio.gather(*(run_source(s, address, timeout) for s in sources)))
    overrides = {str(rule): str(severity) for rule, severity in config.rules.severity.items()}

    def judged(results: tuple[SourceResult, ...]) -> tuple[list[Finding], Verdict, datetime]:
        observed = now()
        findings = rules.with_overrides((f for r in results for f in r.findings), overrides)
        findings += rules.data_gaps(results, observed)
        return findings, decide(findings), observed

    findings, verdict, observed = judged(results)
    later = then(verdict) if then else ()
    if later:
        results += tuple(await asyncio.gather(*(run_source(s, address, timeout) for s in later)))
        findings, verdict, observed = judged(results)
    result = CheckResult(
        check_id=str(uuid.uuid4()),
        created_at=observed,
        address=address,
        verdict=verdict,
        sources=results,
        findings=tuple(findings),
        tool_version=__version__,
        config_hash=config.hash(),
        amount_hint=amount,
        operator_note=note,
        client=client,
    )
    record_hash = audit.append(conn, result)
    log.info(
        "check",
        extra={
            "fields": {
                "check_id": result.check_id,
                "chain": address.chain.value,
                "verdict": result.verdict.value,
                "sources": {r.source: r.status.value for r in results},
            }
        },
    )
    return replace(result, record_hash=record_hash)
