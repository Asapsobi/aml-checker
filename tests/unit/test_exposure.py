import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import BLACKLISTED, FIXTURES, FUNNEL, USDT_TRON, TronGridMock, load, transfer_row

from amlcheck.adapters import ofac, tron
from amlcheck.adapters.exposure import ExposureAdapter, LookupFailed
from amlcheck.config import Config
from amlcheck.core.address import parse
from amlcheck.core.models import Chain, Severity, SourceResult, SourceStatus, Verdict
from amlcheck.core.verdict import decide
from amlcheck.exposure.history import TronHistory
from amlcheck.labels import Label, replace
from amlcheck.storage import db
from amlcheck.storage.cache import ResponseCache

BASE = "https://api.trongrid.io"
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
ME = "TJwwz9NR37hjXdAV5gowj7src4avMuZZNW"
CHEIL = "TA3941uFAvmVibSkQ6fMJXxmaSNovX86mz"  # on the OFAC sample list
OLD = NOW - timedelta(days=400)
FROZEN_SENDER_TX = next(
    row["transaction_id"]
    for row in load("tron/transfers_TAjoXR.json")["data"]
    if row["from"] == BLACKLISTED
)


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


@pytest.fixture
def grid_mock(network: respx.MockRouter) -> TronGridMock:
    return TronGridMock(network)


async def build_index(conn: sqlite3.Connection) -> None:
    async with httpx.AsyncClient() as http:
        grid = tron.TronGrid(http, BASE, None, max_retry_after=10, sleep=no_sleep)
        await tron.sync_index(conn, grid, USDT_TRON, now=lambda: NOW)


def store_ofac(conn: sqlite3.Connection) -> None:
    with (FIXTURES / "ofac" / "sdn_sample.xml").open("rb") as file:
        ofac.store(conn, ofac.parse_list(file), "sha", NOW)


async def exposure(
    conn: sqlite3.Connection,
    address: str,
    config: Config | None = None,
    **kwargs: Any,
) -> SourceResult:
    config = config or Config()
    async with httpx.AsyncClient() as http:
        grid = tron.TronGrid(http, BASE, None, max_retry_after=10, sleep=no_sleep)
        adapter = ExposureAdapter(
            conn,
            Chain.tron,
            TronHistory(grid, USDT_TRON),
            config.exposure,
            config.heuristics,
            ResponseCache(conn, 900, lambda: NOW),
            now=lambda: NOW,
            **kwargs,
        )
        return await adapter.check(parse(address))


def rules_of(result: SourceResult) -> dict[str, list[Any]]:
    found: dict[str, list[Any]] = {}
    for finding in result.findings:
        found.setdefault(finding.rule_id, []).append(finding)
    return found


def row(n: int, sender: str, recipient: str, usdt: str, days_ago: float = 3) -> dict[str, Any]:
    return transfer_row(
        f"tx{n}", NOW - timedelta(days=days_ago, minutes=n), sender, recipient, usdt
    )


async def test_known_frozen_sender_gives_review_with_its_transaction(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    """Phase 2 exit criterion, on the real history of TAjoXRso…"""
    await build_index(conn)
    result = await exposure(conn, FUNNEL)
    assert result.status is SourceStatus.ok
    found = rules_of(result)

    [direct] = found["R-EXP-01"]
    assert direct.severity is Severity.REVIEW
    assert direct.evidence["counterparty"] == BLACKLISTED
    assert [t["tx_hash"] for t in direct.evidence["transfers"]] == [FROZEN_SENDER_TX]
    assert FROZEN_SENDER_TX.startswith("f94a6ad33f18")
    assert direct.summary == (
        f"received 500,000.00 USDT from {BLACKLISTED} (blacklisted by Tether since 2026-09-27"
        " (tx c743e8f463596d2803c9240f903302821fc178bfd8c6652c8eb9590365c2cbfe))"
    )
    [share] = found["R-EXP-02"]
    assert share.evidence["share"] == "0.1667"
    [new] = found["R-HEU-01"]
    assert new.evidence["priority"] == "low"
    assert new.summary == "new address: first active on 2026-09-24, 4 days ago"
    [passing] = found["R-HEU-02"]
    assert passing.evidence["share"] == "1.0000"
    assert decide(result.findings) is Verdict.REVIEW


async def test_sanctioned_counterparty_and_its_share_of_inflow(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    store_ofac(conn)
    rows = [row(1, CHEIL, ME, "1000"), row(2, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", ME, "9000")]
    grid_mock.add_history(ME, rows, created=OLD)
    found = rules_of(await exposure(conn, ME))
    [direct] = found["R-EXP-01"]
    assert "on the OFAC SDN list as CHEIL CREDIT BANK (entry 22985)" in direct.summary
    [share] = found["R-EXP-02"]
    assert share.summary.startswith("10.0% of the USDT received in the last 180 days")
    assert set(found) == {"R-EXP-01", "R-EXP-02"}


async def test_small_share_from_a_flagged_sender_is_only_r_exp_01(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    store_ofac(conn)
    rows = [row(1, CHEIL, ME, "10"), row(2, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", ME, "9990")]
    grid_mock.add_history(ME, rows, created=OLD)
    assert set(rules_of(await exposure(conn, ME))) == {"R-EXP-01"}


async def test_an_established_address_with_ordinary_dealings_has_no_findings(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    rows = [
        row(1, "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", ME, "1000", days_ago=10),
        row(2, ME, "TAjoXRsomrsDDCXsxD1ELFQu4wHfF9HZSv", "100", days_ago=5),
    ]
    grid_mock.add_history(ME, rows, created=OLD)
    result = await exposure(conn, ME)
    assert (result.status, result.findings) == (SourceStatus.ok, ())
    assert result.summary == "2 transfers with 2 counterparties; none flagged"
    assert result.as_of_text == "last 180 d, 2 transfers"


async def test_a_never_used_address_is_new(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    [new] = (await exposure(conn, ME)).findings
    assert (new.rule_id, new.summary) == ("R-HEU-01", "new address: no activity on chain yet")


@pytest.mark.parametrize(("senders", "fires"), [(50, False), (51, True)])
async def test_fan_in_of_small_amounts(
    conn: sqlite3.Connection, grid_mock: TronGridMock, senders: int, fires: bool
) -> None:
    rows = [row(n, f"TSender{n:027d}", ME, "10") for n in range(senders)]
    grid_mock.add_history(ME, rows, created=OLD)
    found = rules_of(await exposure(conn, ME))
    assert ("R-HEU-03" in found) is fires


async def test_fan_out_leaves_out_allowlisted_recipients(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    recipients = [f"TRecipient{n:024d}" for n in range(51)]
    rows = [row(n, ME, who, "10") for n, who in enumerate(recipients)]
    grid_mock.add_history(ME, rows, created=OLD)
    assert "R-HEU-04" in rules_of(await exposure(conn, ME))

    replace(conn, [Label(recipients[0], "tron", "allowlist", "our own wallet", None)])
    conn.execute("DELETE FROM http_cache")
    conn.commit()
    assert "R-HEU-04" not in rules_of(await exposure(conn, ME))


async def test_a_counterparty_labelled_mixer_is_r_heu_05(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    mixer = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
    grid_mock.add_history(ME, [row(1, mixer, ME, "250")], created=OLD)
    replace(conn, [Label(mixer, "tron", "mixer", "known mixer", "analyst")])
    [labelled] = rules_of(await exposure(conn, ME))["R-HEU-05"]
    assert labelled.summary == (
        f"received 250.00 USDT from {mixer} (labelled mixer in labels.csv (known mixer))"
    )


async def test_more_transfers_than_the_limit_is_stale(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    config = Config.model_validate({"exposure": {"max_transfers": 5}})
    result = await exposure(conn, FUNNEL, config)
    assert result.status is SourceStatus.stale
    assert result.summary.startswith("more than 5 transfers in the last 180 days")


async def test_unreadable_history_is_an_error(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    grid_mock.failing.add("transfers")
    result = await exposure(conn, FUNNEL)
    assert result.status is SourceStatus.error
    assert "could not be read" in result.summary


async def test_a_chain_without_a_history_source_is_an_error(conn: sqlite3.Connection) -> None:
    config = Config()
    adapter = ExposureAdapter(
        conn,
        Chain.bsc,
        None,
        config.exposure,
        config.heuristics,
        ResponseCache(conn, 900),
        unavailable="no BSC source yet",
    )
    result = await adapter.check(parse("0x7a3f9c2e8b1d4f6a0c5e9b2d7f1a3c8e6b4d2f90"))
    assert (result.status, result.summary) == (SourceStatus.error, "no BSC source yet")
    assert (await adapter.health()).label == "Exposure (BSC)"


async def test_remote_lookups_ask_about_the_largest_unflagged_counterparties(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    asked: list[str] = []

    async def lookup(address: str) -> str | None:
        asked.append(address)
        if len(asked) == 1:
            return "FROZEN"
        raise LookupFailed("down")

    result = await exposure(conn, FUNNEL, remote=lookup, max_remote=2)
    assert len(asked) == 2
    [direct] = rules_of(result)["R-EXP-01"]
    assert direct.evidence["counterparty"] == asked[0]
    assert "FROZEN according to Eagle Virtual" in direct.summary
    assert result.evidence_meta["remote_lookups_failed"] == [asked[1]]


async def test_history_is_read_once_within_the_ttl(
    conn: sqlite3.Connection, network: respx.MockRouter, grid_mock: TronGridMock
) -> None:
    await exposure(conn, FUNNEL)
    calls = network.calls.call_count
    await exposure(conn, FUNNEL)
    assert network.calls.call_count == calls


async def test_a_thousand_transfers_are_screened_quickly(
    conn: sqlite3.Connection, grid_mock: TronGridMock
) -> None:
    """PRD §11 and the Phase 2 exit criterion: p95 under 60 s with 1,000 transfers."""
    await build_index(conn)
    store_ofac(conn)
    parties = [f"TParty{n:028d}" for n in range(299)] + [BLACKLISTED, CHEIL]
    rows = [row(n, parties[n % len(parties)], ME, "12.5", days_ago=n / 10) for n in range(1000)]
    grid_mock.add_history(ME, rows, created=OLD)
    started = time.perf_counter()
    result = await exposure(conn, ME)
    elapsed = time.perf_counter() - started
    assert result.evidence_meta["transfers"] == 1000
    assert {f.evidence["counterparty"] for f in rules_of(result)["R-EXP-01"]} == {
        BLACKLISTED,
        CHEIL,
    }
    assert elapsed < 5, f"took {elapsed:.1f} s"
