import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from amlcheck.config import Config
from amlcheck.core import audit, engine, rules
from amlcheck.core.address import parse
from amlcheck.core.models import (
    Address,
    CheckResult,
    Finding,
    Severity,
    SourceHealth,
    SourceResult,
    SourceStatus,
    Verdict,
)
from amlcheck.storage import db

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
ADDRESS = parse("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t")


class Source:
    """A stand-in source: answers with a status and findings, raises, or hangs."""

    required = True

    def __init__(
        self,
        name: str,
        status: SourceStatus = SourceStatus.ok,
        found: tuple[str, ...] = (),
        raises: Exception | None = None,
        hangs: bool = False,
    ) -> None:
        self.source = self.label = name
        self._status = status
        self._found = found
        self._raises = raises
        self._hangs = hangs

    async def check(self, address: Address) -> SourceResult:
        if self._hangs:
            await asyncio.sleep(10)
        if self._raises:
            raise self._raises
        findings: tuple[Finding, ...] = tuple(
            rules.finding(rule, self.source, "hit", {}, NOW) for rule in self._found
        )
        return SourceResult(
            self.source, self.label, True, self._status, "answer", findings=findings
        )

    async def health(self) -> SourceHealth:
        return SourceHealth(self.source, self.label, self._status, "fine")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


async def screen(
    conn: sqlite3.Connection, *sources: Source, config: Config | None = None
) -> CheckResult:
    return await engine.screen(
        ADDRESS, sources, conn=conn, config=config or Config(), timeout=0.05, now=lambda: NOW
    )


async def test_clean_sources_give_no_hits_and_the_record_is_written(
    conn: sqlite3.Connection,
) -> None:
    """AT-02 (engine side): the audit record exists before the result is returned."""
    result = await screen(conn, Source("ofac"), Source("eagle"), Source("tron"))
    assert result.verdict is Verdict.NO_HITS
    report = audit.verify(conn)
    assert (report.records, report.head) == (1, result.record_hash)


async def test_a_source_that_breaks_makes_it_incomplete(conn: sqlite3.Connection) -> None:
    """Phase 1 exit criterion: a mocked source outage gives INCOMPLETE."""
    result = await screen(conn, Source("ofac"), Source("eagle", raises=RuntimeError("down")))
    assert result.verdict is Verdict.INCOMPLETE
    [gap] = result.findings
    assert (gap.rule_id, gap.source) == ("R-SYS-01", "eagle")
    assert "RuntimeError: down" in gap.summary


async def test_a_source_that_hangs_makes_it_incomplete(conn: sqlite3.Connection) -> None:
    result = await screen(conn, Source("ofac"), Source("tron", hangs=True))
    assert result.verdict is Verdict.INCOMPLETE
    assert "no answer within 0.05 seconds" in result.findings[0].summary


async def test_sanctions_hit_blocks_while_another_source_is_down(
    conn: sqlite3.Connection,
) -> None:
    """AT-09."""
    result = await screen(
        conn, Source("ofac", found=(rules.SAN_01,)), Source("eagle", SourceStatus.error)
    )
    assert result.verdict is Verdict.BLOCK
    assert {f.rule_id for f in result.findings} == {"R-SAN-01", "R-SYS-01"}


async def test_config_overrides_change_the_verdict(conn: sqlite3.Connection) -> None:
    config = Config.model_validate({"rules": {"severity": {"R-FRZ-02": "BLOCK"}}})
    result = await screen(conn, Source("tron", found=(rules.FRZ_02,)), config=config)
    assert result.verdict is Verdict.BLOCK
    assert result.findings[0].severity is Severity.BLOCK
    assert result.config_hash == config.hash()


@pytest.mark.parametrize("rule", sorted(set(rules.DEFAULT_SEVERITY) - rules.FIXED))
def test_every_rule_but_the_data_gap_can_be_overridden(rule: str) -> None:
    Config.model_validate({"rules": {"severity": {rule: "REVIEW"}}})


def test_the_data_gap_cannot_be_overridden() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"rules": {"severity": {rules.SYS_01: "REVIEW"}}})
