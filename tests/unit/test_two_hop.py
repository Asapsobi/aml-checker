import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FIXTURES
from pydantic import SecretStr

from amlcheck import adapters
from amlcheck.adapters import ofac
from amlcheck.adapters.hypersync import HyperSyncError
from amlcheck.adapters.two_hop import TwoHopAdapter
from amlcheck.config import Config, Secrets, TwoHop
from amlcheck.core.address import parse
from amlcheck.core.engine import run_source
from amlcheck.core.models import Chain, SourceResult, SourceStatus
from amlcheck.exposure.history import History, Transfer
from amlcheck.exposure.reader import HistoryReader
from amlcheck.storage import db
from amlcheck.storage.cache import ResponseCache

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
ME = "0x7a3f9c2e8b1d4f6a0c5e9b2d7f1a3c8e6b4d2f90"
MIDDLE = "0x1111111111111111111111111111111111111111"
OTHER = "0x2222222222222222222222222222222222222222"
LAZARUS = "0x098b716b8aaf21512996dc57eb0615e2383e2f96"  # on the OFAC sample list, as ETH


class Chain_:
    """Transfers on a mock BSC chain: each one is in both parties' histories, as on a real chain."""

    chain = Chain.bsc

    def __init__(self) -> None:
        self.histories: dict[str, list[Transfer]] = {}
        self.calls: list[str] = []
        self.slow: set[str] = set()
        self.broken: set[str] = set()

    def pay(self, sender: str, recipient: str, usdt: str, days_ago: float = 10) -> Transfer:
        n = sum(len(h) for h in self.histories.values())
        t = Transfer(
            f"0x{n:064x}", NOW - timedelta(days=days_ago), sender, recipient, Decimal(usdt)
        )
        for party in {sender, recipient}:
            self.histories.setdefault(party, []).append(t)
        return t

    async def fetch(
        self, address: str, since: datetime, limit: int, *, first_activity: bool = True
    ) -> History:
        self.calls.append(address if first_activity else f"{address} (transfers only)")
        if address in self.broken:
            raise HyperSyncError("HyperSync answered HTTP 503")
        if address in self.slow:
            await asyncio.sleep(10)
        rows = sorted(
            (t for t in self.histories.get(address, []) if t.time >= since),
            key=lambda t: t.time,
            reverse=True,
        )
        return History(tuple(rows[:limit]), since, len(rows) <= limit, NOW - timedelta(days=400))


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        with (FIXTURES / "ofac" / "sdn_sample.xml").open("rb") as file:
            ofac.store(connection, ofac.parse_list(file), "sha", NOW)
        yield connection


@pytest.fixture
def chain() -> Chain_:
    return Chain_()


def walk(conn: sqlite3.Connection, chain: Chain_, **settings: Any) -> TwoHopAdapter:
    config = Config()
    reader = HistoryReader(chain, ResponseCache(conn, 0, lambda: NOW), 180, lambda: NOW)
    return TwoHopAdapter(conn, reader, config.exposure, TwoHop(**settings), now=lambda: NOW)


async def check(conn: sqlite3.Connection, chain: Chain_, **settings: Any) -> SourceResult:
    return await walk(conn, chain, **settings).check(parse(ME))


async def test_a_counterparty_paid_by_a_sanctioned_wallet_raises_r_exp_03(
    conn: sqlite3.Connection, chain: Chain_
) -> None:
    dirty = chain.pay(LAZARUS, MIDDLE, "5000", days_ago=20)
    chain.pay(MIDDLE, ME, "1000")
    chain.pay(ME, OTHER, "300")
    result = await check(conn, chain)
    assert result.status is SourceStatus.ok
    (finding,) = result.findings
    assert finding.rule_id == "R-EXP-03"
    assert finding.summary == (
        f"received 1,000.00 USDT from {MIDDLE}, which received 5,000.00 USDT from {LAZARUS}"
        " (on the OFAC SDN list as LAZARUS GROUP (entry 27307))"
    )
    assert finding.evidence["flagged_transfers"][0]["tx_hash"] == dirty.tx_hash
    assert "none received" not in result.summary
    assert result.summary == "2 counterparties read; 1 received USDT from a flagged wallet"
    graph = result.evidence_meta["graph"]
    assert [(n["id"], n["ring"], n["state"]) for n in graph["nodes"]] == [
        (ME, 0, "target"),
        (MIDDLE, 1, "read"),
        (LAZARUS, 2, "flagged"),
        (OTHER, 1, "read"),
    ]
    assert {"from": LAZARUS, "to": MIDDLE, "received_usdt": "5000", "sent_usdt": "0"} in graph[
        "edges"
    ]


async def test_less_than_the_threshold_is_not_exposure(
    conn: sqlite3.Connection, chain: Chain_
) -> None:
    chain.pay(LAZARUS, MIDDLE, "999.99")
    chain.pay(MIDDLE, ME, "1000")
    result = await check(conn, chain)
    assert (result.status, result.findings) == (SourceStatus.ok, ())
    assert result.summary.endswith("none received USDT from a flagged wallet")


async def test_a_flagged_counterparty_is_left_to_r_exp_01(
    conn: sqlite3.Connection, chain: Chain_
) -> None:
    chain.pay(LAZARUS, ME, "5000")
    result = await check(conn, chain)
    assert result.findings == ()
    assert chain.calls == [ME]  # the flagged counterparty's history is not read
    assert result.evidence_meta["reached"] == {LAZARUS: "flagged"}


async def test_a_hub_is_listed_not_read(conn: sqlite3.Connection, chain: Chain_) -> None:
    for n in range(5):
        chain.pay(f"0x{n + 3:040x}", MIDDLE, "10")
    chain.pay(LAZARUS, MIDDLE, "5000")
    chain.pay(MIDDLE, ME, "1000")
    result = await check(conn, chain, max_transfers=3)
    assert (result.status, result.findings) == (SourceStatus.ok, ())
    assert result.summary.startswith("0 counterparties read (1 hub left out)")
    assert result.evidence_meta["reached"][MIDDLE].startswith("hub: more than 3 transfers")


async def test_only_the_largest_counterparties_are_read(
    conn: sqlite3.Connection, chain: Chain_
) -> None:
    for n, amount in enumerate(("50", "5000", "10", "700", "1")):
        chain.pay(f"0x{n + 3:040x}", ME, amount)
    result = await check(conn, chain, counterparties=2)
    # The 2-hop reads skip the first-activity lookup: the walk needs only the transfers.
    assert chain.calls == [ME, f"0x{4:040x} (transfers only)", f"0x{6:040x} (transfers only)"]
    assert (result.evidence_meta["scope"], result.evidence_meta["counterparties"]) == (2, 5)
    assert result.as_of_text == "2 of 5 counterparties"


async def test_a_counterparty_that_cannot_be_read_leaves_it_incomplete(
    conn: sqlite3.Connection, chain: Chain_
) -> None:
    chain.pay(MIDDLE, ME, "1000")
    chain.pay(OTHER, ME, "10")
    chain.broken.add(MIDDLE)
    result = await check(conn, chain)
    assert result.status is SourceStatus.stale
    assert result.summary == (
        "1 of the 2 largest counterparties could not be read (HyperSync answered HTTP 503),"
        " so this result is not complete"
    )


async def test_the_time_budget_is_kept(conn: sqlite3.Connection, chain: Chain_) -> None:
    chain.pay(MIDDLE, ME, "1000")
    chain.slow.add(MIDDLE)
    result = await asyncio.wait_for(check(conn, chain, time_budget_seconds=0.05), 5)
    assert result.status is SourceStatus.stale
    assert "the time budget ran out" in result.summary


async def test_the_screened_address_is_never_its_own_flagged_sender(
    conn: sqlite3.Connection, chain: Chain_
) -> None:
    chain.pay(LAZARUS, MIDDLE, "5000")
    chain.pay(MIDDLE, LAZARUS, "2000")
    walker = walk(conn, chain)
    result = await walker.check(parse(LAZARUS))
    assert result.findings == ()


async def test_the_reader_fetches_once_for_everyone_waiting(
    conn: sqlite3.Connection, chain: Chain_
) -> None:
    chain.pay(MIDDLE, ME, "1000")
    reader = HistoryReader(chain, ResponseCache(conn, 900, lambda: NOW), 180, lambda: NOW)
    first, second = await asyncio.gather(reader.read(ME, 100), reader.read(ME, 100))
    assert first == second
    assert chain.calls == [ME]
    await reader.read(ME, 100)  # from the cache now
    assert chain.calls == [ME]


async def test_a_reader_that_gives_up_does_not_stop_the_others(
    conn: sqlite3.Connection, chain: Chain_
) -> None:
    chain.pay(MIDDLE, ME, "1000")
    started = asyncio.Event()
    original = chain.fetch

    async def slow_fetch(
        address: str, since: datetime, limit: int, *, first_activity: bool = True
    ) -> History:
        started.set()
        await asyncio.sleep(0.05)
        return await original(address, since, limit, first_activity=first_activity)

    chain.fetch = slow_fetch  # type: ignore[method-assign]
    reader = HistoryReader(chain, ResponseCache(conn, 0, lambda: NOW), 180, lambda: NOW)
    patient = asyncio.ensure_future(reader.read(ME, 100))
    await started.wait()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(reader.read(ME, 100), 0.01)
    assert len((await patient).transfers) == 1


async def test_a_source_can_ask_the_engine_for_more_time() -> None:
    class Slow:
        source, label, required, timeout = "slow", "Slow", True, 0.01

        async def check(self, _: Any) -> SourceResult:
            await asyncio.sleep(1)
            raise AssertionError("not reached")

        async def health(self) -> Any:
            raise NotImplementedError

    result = await run_source(Slow(), parse(ME), timeout=60)
    assert (result.status, result.summary) == (SourceStatus.error, "no answer within 0.01 seconds")


async def test_the_walk_runs_only_when_asked(conn: sqlite3.Connection) -> None:
    keys = Secrets(hypersync_api_token=SecretStr("hs-test"))
    async with httpx.AsyncClient() as http:
        plain = adapters.build(Chain.bsc, conn=conn, http=http, config=Config(), secrets=keys)
        deep = adapters.build(
            Chain.bsc, conn=conn, http=http, config=Config(), secrets=keys, two_hop=True
        )
    assert "exposure_2hop" not in [s.source for s in plain]
    assert [s.source for s in deep][-2:] == ["exposure", "exposure_2hop"]
