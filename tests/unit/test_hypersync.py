import sqlite3
from collections.abc import Callable, Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import (
    FIXTURES,
    HYPERSYNC,
    HYPERSYNC_TOKEN,
    USDT_BSC,
    BscTransfer,
    HyperSyncMock,
    bsc_transfer,
    load,
)
from pydantic import SecretStr

from amlcheck.adapters import ofac
from amlcheck.adapters.exposure import ExposureAdapter
from amlcheck.adapters.hypersync import HyperSync, HyperSyncError, transfers_in
from amlcheck.config import Config
from amlcheck.core.address import parse
from amlcheck.core.clock import from_timestamp
from amlcheck.core.models import Chain, SourceStatus
from amlcheck.exposure.history import BscHistory, History
from amlcheck.storage import db
from amlcheck.storage.cache import ResponseCache

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
SINCE = NOW - timedelta(days=180)
ME = "0x7a3f9c2e8b1d4f6a0c5e9b2d7f1a3c8e6b4d2f90"
LAZARUS = "0x098b716b8aaf21512996dc57eb0615e2383e2f96"  # on the OFAC sample list, as ETH
BUSY = "0x8894e0a0c962cb723c1976a4421c95949be2d4e3"  # the wallet in hypersync/transfers.json
QUIET = "0x6b0123519aa81b05b93c48e29e86ad4006abc4eb"  # the wallet in hypersync/first_activity.json


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def chain(network: respx.MockRouter) -> HyperSyncMock:
    return HyperSyncMock(network, int(NOW.timestamp()))


def client(
    http: httpx.AsyncClient, sleep: Sleeps | None = None, token: str = HYPERSYNC_TOKEN
) -> HyperSync:
    return HyperSync(http, HYPERSYNC, SecretStr(token), max_retry_after=10, sleep=sleep or Sleeps())


async def fetch(address: str, limit: int = 5000, sleep: Sleeps | None = None) -> History:
    async with httpx.AsyncClient() as http:
        return await BscHistory(client(http, sleep), USDT_BSC).fetch(address, SINCE, limit)


def received(
    count: int, start: datetime, every: timedelta, per_block: int = 1
) -> list[BscTransfer]:
    """`count` transfers of 2 USDT from LAZARUS to ME, `per_block` to a block, oldest first."""
    return [
        bsc_transfer(
            f"0x{n:064x}", start + every * (n // per_block), LAZARUS, ME, "2", n % per_block
        )
        for n in range(count)
    ]


def test_the_recorded_transfers_parse() -> None:
    page = load("hypersync/transfers.json")
    rows = transfers_in(page)
    assert len(rows) == 20
    assert rows == sorted(rows, key=lambda r: (r.block, r.log_index))
    assert all(BUSY in (r.sender, r.recipient) for r in rows)
    assert all(r.tx_hash.startswith("0x") and len(r.tx_hash) == 66 for r in rows)
    assert all(len(r.sender) == len(r.recipient) == 42 and r.value > 0 for r in rows)
    first = page["data"][0]["blocks"][0]
    assert rows[0].block == first["number"]
    assert rows[0].time == from_timestamp(int(first["timestamp"], 16))  # hex in the answer
    assert transfers_in(load("hypersync/transfers_none.json")) == []


async def test_the_recorded_first_activity_parses(network: respx.MockRouter) -> None:
    network.post(f"{HYPERSYNC}/query").respond(200, json=load("hypersync/first_activity.json"))
    async with httpx.AsyncClient() as http:
        first = await client(http).first_activity(QUIET)
    assert first == datetime(2025, 10, 6, 14, 36, 54, tzinfo=UTC)


async def test_the_recorded_height_and_block_time_parse(network: respx.MockRouter) -> None:
    network.get(f"{HYPERSYNC}/height").respond(200, json=load("hypersync/height.json"))
    answer = load("hypersync/block_time.json")
    network.post(f"{HYPERSYNC}/query").respond(200, json=answer)
    recorded = answer["data"][0]["blocks"][0]
    async with httpx.AsyncClient() as http:
        hypersync = client(http)
        assert await hypersync.height() == load("hypersync/height.json")["height"]
        assert await hypersync.block_time(recorded["number"]) == int(recorded["timestamp"], 16)


async def test_a_quiet_address_is_read_whole(chain: HyperSyncMock) -> None:
    chain.transfers = [
        *received(3, NOW - timedelta(days=100), timedelta(days=30)),
        bsc_transfer("0xold", SINCE - timedelta(hours=1), LAZARUS, ME, "5"),  # before the window
        BscTransfer(
            "0xother", int((NOW - timedelta(days=1)).timestamp()), LAZARUS, ME, 10**18, 0, "0xtoken"
        ),
    ]
    history = await fetch(ME)
    assert (len(history.transfers), history.complete) == (3, True)
    assert [t.tx_hash for t in history.transfers] == [
        f"0x{n:064x}" for n in (2, 1, 0)
    ]  # newest first
    assert history.transfers[0].amount == Decimal(2)
    assert (history.transfers[0].sender, history.transfers[0].recipient) == (LAZARUS, ME)


async def test_paging_follows_next_block(chain: HyperSyncMock) -> None:
    chain.scan_blocks = 1_000_000  # about 11.6 days a page on this chain
    chain.transfers = received(50, SINCE + timedelta(days=1), timedelta(days=3))
    history = await fetch(ME)
    hashes = [t.tx_hash for t in history.transfers]
    assert len(hashes) == len(set(hashes)) == 50
    assert history.complete
    assert sum("logs" in q and "transactions" not in q for q in chain.queries) > 10


async def test_more_transfers_than_the_limit_keeps_the_newest(chain: HyperSyncMock) -> None:
    chain.transfers = received(1200, NOW - timedelta(days=30), timedelta(seconds=10))
    history = await fetch(ME, limit=1000)
    assert (len(history.transfers), history.complete) == (1000, False)
    assert [t.tx_hash for t in history.transfers] == [f"0x{n:064x}" for n in range(1199, 199, -1)]


async def test_a_burst_at_the_newest_end_shrinks_the_windows(chain: HyperSyncMock) -> None:
    """Sparse old transfers make the first window too wide for a recent burst."""
    sparse = received(150, SINCE + timedelta(hours=1), timedelta(days=1))
    burst = [
        bsc_transfer(
            f"0xb{n:063x}", NOW - timedelta(hours=1) + timedelta(seconds=7 * n), LAZARUS, ME, "1"
        )
        for n in range(500)
    ]
    chain.transfers = sparse + burst
    history = await fetch(ME, limit=100)
    assert (len(history.transfers), history.complete) == (100, False)
    assert [t.tx_hash for t in history.transfers] == [f"0xb{n:063x}" for n in range(499, 399, -1)]


async def test_a_busy_address_is_read_from_the_newest_end_early(chain: HyperSyncMock) -> None:
    """The first answer shows far more than the limit: the oldest are not read any further."""
    chain.logs_per_answer = 1000
    chain.transfers = received(20_000, SINCE + timedelta(minutes=30), timedelta(seconds=777))
    history = await fetch(ME, limit=5000)
    assert (len(history.transfers), history.complete) == (5000, False)
    assert [t.tx_hash for t in history.transfers] == [
        f"0x{n:064x}" for n in range(19_999, 14_999, -1)
    ]
    reads = [q for q in chain.queries if "logs" in q and "transactions" not in q]
    oldest_first = [
        q for q in reads if q["from_block"] < int((SINCE + timedelta(days=1)).timestamp())
    ]
    assert len(oldest_first) == 1
    assert len(reads) <= 12


async def test_a_burst_at_the_oldest_end_is_still_read_whole(chain: HyperSyncMock) -> None:
    """A burst looks busy at first, but the newest-first reading reaches it and finds no more."""
    chain.logs_per_answer = 500
    chain.transfers = received(900, SINCE + timedelta(minutes=30), timedelta(seconds=2))
    history = await fetch(ME, limit=5000)
    assert (len(history.transfers), history.complete) == (900, True)
    assert [t.tx_hash for t in history.transfers] == [f"0x{n:064x}" for n in range(899, -1, -1)]


async def test_one_block_with_more_transfers_than_the_limit(chain: HyperSyncMock) -> None:
    chain.transfers = received(30, NOW - timedelta(days=2), timedelta(0), per_block=30)
    history = await fetch(ME, limit=10)
    assert (len(history.transfers), history.complete) == (10, False)
    assert {t.time for t in history.transfers} == {NOW - timedelta(days=2)}


async def test_windows_narrow_down_to_a_single_busy_block(chain: HyperSyncMock) -> None:
    """Three blocks of 8 transfers after sparse old ones: the newest 10 are the newest block's 8
    and the 2 last of the block before it."""
    sparse = received(20, SINCE + timedelta(hours=1), timedelta(days=1))
    busy = [
        bsc_transfer(f"0x{block:03x}{n:061x}", NOW - timedelta(seconds=block), LAZARUS, ME, "1", n)
        for block in (100, 50, 10)
        for n in range(8)
    ]
    chain.transfers = sparse + busy
    history = await fetch(ME, limit=10)
    assert (len(history.transfers), history.complete) == (10, False)
    expected = [f"0x{10:03x}{n:061x}" for n in range(7, -1, -1)] + [
        f"0x{50:03x}{n:061x}" for n in (7, 6)
    ]
    assert [t.tx_hash for t in history.transfers] == expected


async def test_an_address_without_transfers(chain: HyperSyncMock) -> None:
    history = await fetch(ME)
    assert (history.transfers, history.complete, history.first_activity) == ((), True, None)


async def test_first_activity_is_the_earliest_transaction_or_transfer(chain: HyperSyncMock) -> None:
    chain.transfers = received(3, NOW - timedelta(days=30), timedelta(days=1))
    history = await fetch(ME)
    assert history.first_activity == NOW - timedelta(days=30)
    chain.first_tx[ME] = int((NOW - timedelta(days=400)).timestamp())
    history = await fetch(ME)
    assert history.first_activity == NOW - timedelta(days=400)


async def test_first_activity_goes_on_past_a_page(chain: HyperSyncMock) -> None:
    chain.scan_blocks = 200_000_000  # about 6 years a page: the scan from block 0 takes 9
    chain.first_tx[ME] = int((NOW - timedelta(days=3)).timestamp())
    history = await fetch(ME)
    assert history.first_activity == NOW - timedelta(days=3)
    assert sum("transactions" in q for q in chain.queries) >= 9


async def test_zero_value_transfers_are_left_out(chain: HyperSyncMock) -> None:
    spam = bsc_transfer("0xpoison", NOW - timedelta(days=1), LAZARUS, ME, "0")
    chain.transfers = [*received(2, NOW - timedelta(days=5), timedelta(days=1)), spam]
    history = await fetch(ME)
    assert (len(history.transfers), history.zero_value) == (2, 1)


async def test_the_start_block_steps_back_when_the_chain_used_to_be_faster(
    chain: HyperSyncMock,
) -> None:
    """Before `turn`, two blocks came each second: the recent pace alone would start too late."""
    turn = int((NOW - timedelta(days=90)).timestamp())
    chain.time_of = lambda n: n if n >= turn else turn - (turn - n) // 2
    async with httpx.AsyncClient() as http:
        start = await client(http).start_block(SINCE, chain.head)
    assert SINCE.timestamp() - 3600 < chain.time_of(start) <= SINCE.timestamp()


async def test_the_start_block_steps_forward_when_the_chain_used_to_be_slower(
    chain: HyperSyncMock,
) -> None:
    """Before `turn`, a block came every two seconds: the recent pace alone would start weeks
    early, and read transfers that do not count."""
    turn = int((NOW - timedelta(days=90)).timestamp())
    chain.time_of = lambda n: n if n >= turn else turn - (turn - n) * 2
    async with httpx.AsyncClient() as http:
        start = await client(http).start_block(SINCE, chain.head)
    assert SINCE.timestamp() - 3600 < chain.time_of(start) <= SINCE.timestamp()


async def test_a_refused_token_is_reported_without_the_token(chain: HyperSyncMock) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(HyperSyncError, match="HTTP 401: Your token is malformed") as refused:
            await BscHistory(client(http, token="hs-wrong-token"), USDT_BSC).fetch(ME, SINCE, 10)
    assert "hs-wrong-token" not in str(refused.value)


async def test_a_network_failure_is_reported(network: respx.MockRouter) -> None:
    network.get(f"{HYPERSYNC}/height").mock(side_effect=httpx.ConnectError("no route"))
    network.post(f"{HYPERSYNC}/query").mock(side_effect=httpx.ConnectError("no route"))
    with pytest.raises(HyperSyncError) as failed:
        await fetch(ME)
    assert str(failed.value) == "could not reach HyperSync (ConnectError)"
    assert failed.value.__cause__ is None


async def test_a_rate_limit_is_waited_out(chain: HyperSyncMock) -> None:
    chain.rate_limited = 2
    sleeps = Sleeps()
    history = await fetch(ME, sleep=sleeps)
    assert history.complete
    assert len(sleeps.calls) == 2  # one wait per refused query


async def test_an_answer_that_makes_no_progress_is_an_error(network: respx.MockRouter) -> None:
    network.post(f"{HYPERSYNC}/query").respond(
        200, json={"data": [], "archive_height": 500, "next_block": 100, "total_execution_time": 1}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(HyperSyncError, match="no progress past block 100"):
            await client(http).first_activity(ME)


@pytest.mark.parametrize(
    "damage",
    [
        lambda log: log.pop("topic2"),
        lambda log: log.update(topic1="0x1234"),
        lambda log: log.update(data="not hex"),
    ],
    ids=["missing topic", "short topic", "bad amount"],
)
def test_an_unreadable_transfer_is_an_error(damage: Callable[[dict[str, Any]], object]) -> None:
    page = load("hypersync/transfers.json")
    damage(page["data"][0]["logs"][0])
    with pytest.raises(HyperSyncError, match="unreadable transfer"):
        transfers_in(page)


async def test_an_answer_that_is_not_json_is_an_error(network: respx.MockRouter) -> None:
    network.get(f"{HYPERSYNC}/height").respond(200, text="<html>maintenance</html>")
    async with httpx.AsyncClient() as http:
        with pytest.raises(HyperSyncError, match="answer is not JSON"):
            await client(http).height()


async def test_an_error_page_is_reported_after_retries(network: respx.MockRouter) -> None:
    route = network.get(f"{HYPERSYNC}/height").respond(502, text="<html>bad gateway</html>")
    async with httpx.AsyncClient() as http:
        with pytest.raises(HyperSyncError) as failed:
            await client(http).height()
    assert str(failed.value) == "HyperSync answered HTTP 502"
    assert route.call_count == 3


async def test_an_answer_without_next_block_is_an_error(network: respx.MockRouter) -> None:
    network.post(f"{HYPERSYNC}/query").respond(200, json={"data": []})
    async with httpx.AsyncClient() as http:
        with pytest.raises(HyperSyncError, match="no next_block"):
            await client(http).first_activity(ME)


async def test_a_block_that_does_not_come_back_is_an_error(network: respx.MockRouter) -> None:
    network.post(f"{HYPERSYNC}/query").respond(
        200, json={"data": [], "archive_height": 500, "next_block": 101}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(HyperSyncError, match="did not return block 100"):
            await client(http).block_time(100)


async def test_a_window_older_than_the_chain_starts_at_block_0(chain: HyperSyncMock) -> None:
    chain.time_of = lambda n: n + 1000  # block 0 was made 1,000 seconds after the epoch
    async with httpx.AsyncClient() as http:
        start = await client(http).start_block(from_timestamp(500), chain.head)
    assert start == 0


async def test_first_activity_looks_up_a_block_time_left_out(network: respx.MockRouter) -> None:
    """HyperSync joins the block to a match (hypersync/first_activity.json); if it ever did not,
    the time is read separately."""
    page = {"archive_height": 500, "rollback_guard": None, "total_execution_time": 1}
    network.post(f"{HYPERSYNC}/query").mock(
        side_effect=[
            httpx.Response(
                200, json={**page, "data": [{"logs": [{"block_number": 42}]}], "next_block": 43}
            ),
            httpx.Response(
                200,
                json={
                    **page,
                    "data": [{"blocks": [{"number": 42, "timestamp": "0x7b"}]}],
                    "next_block": 43,
                },
            ),
        ]
    )
    async with httpx.AsyncClient() as http:
        assert await client(http).first_activity(ME) == from_timestamp(123)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


async def test_bsc_exposure_flags_an_ofac_listed_counterparty(
    conn: sqlite3.Connection, chain: HyperSyncMock
) -> None:
    """OFAC lists Lazarus under ETH; on BSC the same address is the same key (V1)."""
    with (FIXTURES / "ofac" / "sdn_sample.xml").open("rb") as file:
        ofac.store(conn, ofac.parse_list(file), "sha", NOW)
    chain.transfers = received(3, NOW - timedelta(days=30), timedelta(days=1))
    chain.first_tx[ME] = int((NOW - timedelta(days=400)).timestamp())
    config = Config()
    async with httpx.AsyncClient() as http:
        adapter = ExposureAdapter(
            conn,
            Chain.bsc,
            BscHistory(client(http), USDT_BSC),
            config.exposure,
            config.heuristics,
            ResponseCache(conn, 900, lambda: NOW),
            now=lambda: NOW,
        )
        result = await adapter.check(parse(ME))
    assert result.status is SourceStatus.ok
    assert {f.rule_id for f in result.findings} == {"R-EXP-01", "R-EXP-02"}
    direct = next(f for f in result.findings if f.rule_id == "R-EXP-01")
    assert "LAZARUS GROUP (entry 27307)" in direct.summary
