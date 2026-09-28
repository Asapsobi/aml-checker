import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import BLACKLISTED, TronGridMock, load

from amlcheck.adapters import tron
from amlcheck.core.address import parse
from amlcheck.core.clock import from_timestamp
from amlcheck.core.models import Severity, SourceStatus
from amlcheck.storage import db

BASE = "https://api.trongrid.io"
CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
FROZEN = BLACKLISTED
FREEZE_TX = "c743e8f463596d2803c9240f903302821fc178bfd8c6652c8eb9590365c2cbfe"
CLEAN = "TJwwz9NR37hjXdAV5gowj7src4avMuZZNW"
HEAD_TIME = from_timestamp(
    load("tron/solidity_nowblock.json")["block_header"]["raw_data"]["timestamp"] / 1000
)
NOW = HEAD_TIME + timedelta(minutes=5)


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


@pytest.fixture
def grid_mock(network: respx.MockRouter) -> TronGridMock:
    return TronGridMock(network)


async def sync(conn: sqlite3.Connection) -> tron.IndexSync:
    async with httpx.AsyncClient() as http:
        grid = tron.TronGrid(http, BASE, None, max_retry_after=10, sleep=no_sleep)
        return await tron.sync_index(conn, grid, CONTRACT, now=lambda: NOW)


async def check(conn: sqlite3.Connection, address: str, now: datetime = NOW) -> Any:
    async with httpx.AsyncClient() as http:
        grid = tron.TronGrid(http, BASE, None, max_retry_after=10, sleep=no_sleep)
        adapter = tron.TronUsdtAdapter(conn, grid, CONTRACT, timedelta(hours=1), lambda: now)
        return await adapter.check(parse(address))


async def test_first_sync_indexes_every_event(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    report = await sync(conn)
    assert (report.new_events, report.first_sync, report.head_time) == (6, True, HEAD_TIME)
    assert all("min_block_timestamp" not in p for p in grid_mock.event_params)
    [event] = tron.history(conn, FROZEN)
    assert (event.kind, event.tx_hash, event.block) == ("AddedBlackList", FREEZE_TX, 86613172)


async def test_next_sync_reads_from_the_last_block_and_stores_nothing_twice(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    await sync(conn)
    grid_mock.event_params.clear()
    report = await sync(conn)
    assert (report.new_events, report.first_sync) == (0, False)
    since = int((HEAD_TIME - tron.OVERLAP).timestamp() * 1000)
    assert {p["min_block_timestamp"] for p in grid_mock.event_params} == {str(since)}
    assert tron.index_state(conn) == tron.IndexState(report.head, HEAD_TIME, NOW, 6)


async def test_a_deprecated_contract_stops_the_sync(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    grid_mock.deprecated = True
    with pytest.raises(tron.ContractDeprecated):
        await sync(conn)
    assert tron.index_state(conn) is None


async def test_blacklisted_address_is_frozen_with_its_transaction(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    await sync(conn)
    result = await check(conn, FROZEN)
    assert (result.status, result.summary) == (SourceStatus.ok, "blacklisted")
    [finding] = result.findings
    assert (finding.rule_id, finding.severity) == ("R-FRZ-01", Severity.BLOCK)
    assert finding.summary == f"FROZEN: blacklisted by Tether since 2026-09-27 (tx {FREEZE_TX})"
    assert result.as_of_text == "block 86,640,510"


async def test_clean_address_has_no_findings(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    await sync(conn)
    result = await check(conn, CLEAN)
    assert (result.status, result.summary, result.findings) == (
        SourceStatus.ok,
        "not blacklisted",
        (),
    )


def event(kind: str, block: int, amount: str | None = None) -> tron.Event:
    return tron.Event(kind, f"tx{block}", block, f"2026-01-{block:02d}T00:00:00Z", amount)


def test_blacklist_history_reads_as_released_or_seized() -> None:
    [released] = tron.findings_for(
        False, [event("AddedBlackList", 1), event("RemovedBlackList", 2)], NOW
    )
    assert (released.rule_id, released.severity) == ("R-FRZ-02", Severity.REVIEW)
    assert "released on 2026-01-02 (tx tx2)" in released.summary

    [seized] = tron.findings_for(
        True, [event("AddedBlackList", 3), event("DestroyedBlackFunds", 4, "17534303971")], NOW
    )
    assert seized.rule_id == "R-FRZ-01"
    assert seized.summary.endswith("17,534.303971 USDT destroyed (tx tx4)")

    [unindexed] = tron.findings_for(True, [], NOW)
    assert "holds no AddedBlackList event" in unindexed.summary
    assert tron.findings_for(False, [], NOW) == ()


async def test_without_an_index_the_check_is_an_error(conn: sqlite3.Connection) -> None:
    result = await check(conn, FROZEN)
    assert result.status is SourceStatus.error
    assert "amlcheck sync tron-index" in result.summary


async def test_a_failed_live_check_is_an_error(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    await sync(conn)
    grid_mock.failing.add("isBlackListed(address)")
    result = await check(conn, FROZEN)
    assert result.status is SourceStatus.error
    assert "isBlackListed" in result.summary


async def test_an_index_that_cannot_catch_up_goes_stale(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    await sync(conn)
    grid_mock.failing.add("head")
    result = await check(conn, FROZEN, now=HEAD_TIME + timedelta(hours=2))
    assert result.status is SourceStatus.stale
    assert "120 minutes behind" in result.summary
    assert "could not be refreshed" in result.summary
    assert result.findings[0].rule_id == "R-FRZ-01"


async def test_deprecation_found_at_check_time_is_an_error(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    await sync(conn)
    grid_mock.deprecated = True
    result = await check(conn, FROZEN)
    assert result.status is SourceStatus.error
    assert "deprecated" in result.summary


async def test_health_reports_the_lag(conn: sqlite3.Connection, grid_mock: TronGridMock) -> None:
    async with httpx.AsyncClient() as http:
        grid = tron.TronGrid(http, BASE, None, max_retry_after=10, sleep=no_sleep)
        adapter = tron.TronUsdtAdapter(conn, grid, CONTRACT, timedelta(hours=1), lambda: NOW)
        assert (await adapter.health()).status is SourceStatus.error
        await sync(conn)
        health = await adapter.health()
    assert health.status is SourceStatus.ok
    assert "6 blacklist events" in health.detail
    assert "5 minutes ago" in health.detail


def test_head_time_is_what_the_fixture_says() -> None:
    assert datetime(2026, 9, 28, 11, 12, 57, tzinfo=UTC) == HEAD_TIME
