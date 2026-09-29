import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import ETHERSCAN, FIXTURES, USDT_BSC, EtherscanMock, load, token_row
from pydantic import SecretStr

from amlcheck.adapters import ofac
from amlcheck.adapters.etherscan import Etherscan, EtherscanError
from amlcheck.adapters.exposure import ExposureAdapter
from amlcheck.config import Config
from amlcheck.core.address import parse
from amlcheck.core.models import Chain, SourceStatus
from amlcheck.exposure.history import BscHistory, History
from amlcheck.storage import db
from amlcheck.storage.cache import ResponseCache

KEY = "TESTKEY1234567890ABCDEFGHIJKLMNOPQ"
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
SINCE = NOW - timedelta(days=180)
LISTED = "0x2f389ce8bd8ff92de3402ffce4691d17fc4f6535"  # the recorded page's address, OFAC-listed
USDT_ETHEREUM = "0xdac17f958d2ee523a2206206994597c13d831ec7"
ME = "0x7a3f9c2e8b1d4f6a0c5e9b2d7f1a3c8e6b4d2f90"
LAZARUS = "0x098b716b8aaf21512996dc57eb0615e2383e2f96"  # on the OFAC sample list, as ETH


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def scan(network: respx.MockRouter) -> EtherscanMock:
    return EtherscanMock(network)


def client(http: httpx.AsyncClient, sleep: Sleeps | None = None) -> Etherscan:
    return Etherscan(
        http,
        ETHERSCAN,
        SecretStr(KEY),
        56,
        per_second=100,
        max_retry_after=10,
        sleep=sleep or Sleeps(),
    )


async def fetch(
    address: str, limit: int = 5000, sleep: Sleeps | None = None, contract: str = USDT_BSC
) -> History:
    async with httpx.AsyncClient() as http:
        return await BscHistory(client(http, sleep), contract).fetch(address, SINCE, limit)


def rows(count: int, per_block: int = 1) -> list[dict[str, Any]]:
    """`count` transfers to ME, `per_block` of them in each block, newest last."""
    start = NOW - timedelta(days=30)
    return [
        token_row(f"0x{n:064x}", start + timedelta(seconds=10 * (n // per_block)), LAZARUS, ME, "2")
        for n in range(count)
    ]


async def test_the_recorded_page_parses(scan: EtherscanMock) -> None:
    scan.histories[LISTED] = load("etherscan/tokentx_usdt_page.json")["result"]
    scan.block_at = "0"  # the recording has real block numbers
    history = await fetch(LISTED, contract=USDT_ETHEREUM)  # recorded on Ethereum: same API
    assert (len(history.transfers), history.complete, history.zero_value) == (200, True, 0)
    newest = history.transfers[0]
    assert newest.tx_hash.startswith("0x")
    assert newest.amount > 0
    assert newest.time >= history.transfers[-1].time
    assert sum(t.recipient == LISTED for t in history.transfers) == 149


async def test_paging_moves_the_end_block_and_reads_the_edge_block_again(
    scan: EtherscanMock,
) -> None:
    """Five transfers share each block, so a page of 1,000 can end mid-block."""
    scan.histories[ME] = rows(2503, per_block=7)
    history = await fetch(ME)
    hashes = [t.tx_hash for t in history.transfers]
    assert len(hashes) == len(set(hashes)) == 2503
    pages = [c for c in scan.calls if c.get("action") == "tokentx" and c.get("sort") == "desc"]
    assert "endblock" not in pages[0]  # BSC is past block 99,999,999: no fixed end block
    assert all("endblock" in page for page in pages[1:])


async def test_more_transfers_than_the_limit_is_incomplete(scan: EtherscanMock) -> None:
    scan.histories[ME] = rows(1200)
    history = await fetch(ME, limit=1000)
    assert (len(history.transfers), history.complete) == (1000, False)


async def test_an_address_without_transfers(scan: EtherscanMock) -> None:
    history = await fetch(ME)
    assert (history.transfers, history.complete, history.first_activity) == ((), True, None)


async def test_first_activity_is_the_earliest_transaction_or_transfer(
    scan: EtherscanMock,
) -> None:
    scan.histories[ME] = rows(3)
    scan.first_normal[ME] = int((NOW - timedelta(days=400)).timestamp())
    history = await fetch(ME)
    assert history.first_activity == NOW - timedelta(days=400)


async def test_zero_value_transfers_are_left_out(scan: EtherscanMock) -> None:
    spam = token_row("0xpoison", NOW - timedelta(days=1), LAZARUS, ME, "0")
    scan.histories[ME] = [*rows(2), spam]
    history = await fetch(ME)
    assert (len(history.transfers), history.zero_value) == (2, 1)
    assert history.transfers[0].amount == Decimal(2)


async def test_the_free_plan_refusal_is_reported_and_carries_no_key(scan: EtherscanMock) -> None:
    scan.free_plan = True
    with pytest.raises(EtherscanError, match="Free API access is not supported") as refused:
        await fetch(ME)
    assert KEY not in str(refused.value)


async def test_a_rate_limit_refusal_is_retried(scan: EtherscanMock) -> None:
    scan.rate_limited = 2
    sleeps = Sleeps()
    history = await fetch(ME, sleep=sleeps)
    assert history.complete
    assert sleeps.calls.count(1.0) == 2


async def test_a_network_failure_is_reported_without_the_url(network: respx.MockRouter) -> None:
    network.get(ETHERSCAN).mock(
        side_effect=httpx.ConnectError(f"cannot reach {ETHERSCAN}?apikey={KEY}")
    )
    with pytest.raises(EtherscanError) as failed:
        await fetch(ME)
    assert str(failed.value) == "could not reach Etherscan (ConnectError)"
    assert failed.value.__cause__ is None


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


async def test_bsc_exposure_flags_an_ofac_listed_counterparty(
    conn: sqlite3.Connection, scan: EtherscanMock
) -> None:
    """OFAC lists Lazarus under ETH; on BSC the same address is the same key (V1)."""
    with (FIXTURES / "ofac" / "sdn_sample.xml").open("rb") as file:
        ofac.store(conn, ofac.parse_list(file), "sha", NOW)
    scan.histories[ME] = rows(3)
    scan.first_normal[ME] = int((NOW - timedelta(days=400)).timestamp())
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
