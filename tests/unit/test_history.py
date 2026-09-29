from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import respx
from conftest import FUNNEL, USDT_TRON, TronGridMock, transfer_row

from amlcheck.adapters.tron import TronGrid
from amlcheck.exposure.history import History, TronHistory

BASE = "https://api.trongrid.io"
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
SINCE = NOW - timedelta(days=180)
ADDRESS = "TJwwz9NR37hjXdAV5gowj7src4avMuZZNW"
OTHER = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


async def no_sleep(_: float) -> None:
    return None


async def fetch(address: str, limit: int = 5000) -> History:
    async with httpx.AsyncClient() as http:
        grid = TronGrid(http, BASE, None, max_retry_after=10, sleep=no_sleep)
        return await TronHistory(grid, USDT_TRON).fetch(address, SINCE, limit)


def rows(count: int, start: datetime = NOW - timedelta(days=30)) -> list[dict[str, object]]:
    return [
        transfer_row(f"tx{n}", start + timedelta(minutes=n), OTHER, ADDRESS, "1.5")
        for n in range(count)
    ]


async def test_the_recorded_funnel_history(network: respx.MockRouter) -> None:
    TronGridMock(network)
    history = await fetch(FUNNEL)
    assert history.complete
    assert len(history.transfers) == 12
    newest = history.transfers[0]
    assert (newest.recipient, newest.amount) == ("TRvAepSMWFUyX7FGx9EzC4SZRCnVfwcqfW", Decimal(20))
    # Activated on 2026-09-24, two days before its first USDT transfer.
    assert history.first_activity == datetime(2026, 9, 24, 1, 31, 57, tzinfo=UTC)


async def test_pages_are_followed_to_the_end(network: respx.MockRouter) -> None:
    grid = TronGridMock(network)
    grid.add_history(ADDRESS, rows(450), created=NOW - timedelta(days=400))
    history = await fetch(ADDRESS)
    assert (len(history.transfers), history.complete) == (450, True)
    assert history.transfers[0].tx_hash == "tx449"  # newest first


async def test_more_than_the_limit_is_reported_as_incomplete(network: respx.MockRouter) -> None:
    grid = TronGridMock(network)
    grid.add_history(ADDRESS, rows(450), created=NOW - timedelta(days=400))
    history = await fetch(ADDRESS, limit=300)
    assert (len(history.transfers), history.complete) == (300, False)


async def test_zero_value_transfers_and_approvals_are_left_out(network: respx.MockRouter) -> None:
    grid = TronGridMock(network)
    spam = transfer_row("poison", NOW - timedelta(days=1), OTHER, ADDRESS, "0")
    approval = {
        **transfer_row("approve", NOW - timedelta(days=2), ADDRESS, OTHER, "5"),
        "type": "Approval",
    }
    grid.add_history(ADDRESS, [*rows(3), spam, approval], created=NOW - timedelta(days=400))
    history = await fetch(ADDRESS)
    assert [t.tx_hash for t in history.transfers] == ["tx2", "tx1", "tx0"]
    assert history.zero_value == 1


async def test_a_never_used_address_has_no_first_activity(network: respx.MockRouter) -> None:
    TronGridMock(network)
    history = await fetch(ADDRESS)
    assert (history.transfers, history.complete, history.first_activity) == ((), True, None)


async def test_an_address_never_activated_is_active_from_its_first_transfer(
    network: respx.MockRouter,
) -> None:
    """Like TAQM43ow…: it moved USDT without ever being activated (V10)."""
    grid = TronGridMock(network)
    grid.add_history(ADDRESS, rows(3), created=None)
    history = await fetch(ADDRESS)
    assert history.first_activity == NOW - timedelta(days=30)


async def test_history_survives_the_cache_round_trip(network: respx.MockRouter) -> None:
    TronGridMock(network)
    history = await fetch(FUNNEL)
    assert History.from_json(history.to_json()) == history
