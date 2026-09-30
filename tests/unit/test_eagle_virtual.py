import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from conftest import load
from pydantic import SecretStr

from amlcheck.adapters.eagle_virtual import EagleVirtualAdapter
from amlcheck.config import EagleVirtual
from amlcheck.core.address import parse
from amlcheck.core.models import Severity, SourceStatus
from amlcheck.storage import db
from amlcheck.storage.cache import ResponseCache

BASE = "https://eaglevirtual.com"
CREDIT = "Data from Eagle Virtual, https://eaglevirtual.com/license"
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
TRON_CLEAN = "TJwwz9NR37hjXdAV5gowj7src4avMuZZNW"
TRON_FROZEN = "TAQM43owNJLZz3vh3PXxBu2qTWf2McMQwJ"  # blacklisted by Tether on 2026-09-27
LAZARUS = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"
KEY = SecretStr("ev_live_test")


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


def reply(fixture: str, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, json=load(fixture), headers={"x-ev-credit-line": CREDIT, **headers}
    )


def mock(network: respx.MockRouter, path: str, *responses: httpx.Response) -> respx.Route:
    route = network.get(f"{BASE}{path}")
    return (
        route.mock(side_effect=list(responses)) if len(responses) > 1 else route.mock(responses[0])
    )


async def check(
    conn: sqlite3.Connection,
    address: str,
    key: SecretStr | None = KEY,
    sleeps: Sleeps | None = None,
) -> Any:
    async with httpx.AsyncClient() as http:
        adapter = EagleVirtualAdapter(
            http,
            key,
            EagleVirtual(),
            ResponseCache(conn, 900, lambda: NOW),
            max_retry_after=10,
            sleep=sleeps or Sleeps(),
        )
        return await adapter.check(parse(address))


async def test_clear_address(conn: sqlite3.Connection, network: respx.MockRouter) -> None:
    route = mock(network, f"/v1/check/{TRON_CLEAN}", reply("eagle_virtual/check_clear.json"))
    result = await check(conn, TRON_CLEAN)
    assert (result.status, result.summary, result.findings) == (SourceStatus.ok, "CLEAR", ())
    assert result.attribution == CREDIT
    assert route.calls.last.request.headers["Authorization"] == "Bearer ev_live_test"


async def test_frozen_address_names_the_freezing_transaction(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    """AT-04, with Eagle Virtual's real record of Tether's freeze."""
    mock(network, f"/v1/check/{TRON_FROZEN}", reply("eagle_virtual/check_frozen_tron.json"))
    mock(network, f"/v1/address/{TRON_FROZEN}", reply("eagle_virtual/address_frozen_tron.json"))
    result = await check(conn, TRON_FROZEN)
    [finding] = result.findings
    assert (finding.rule_id, finding.severity) == ("R-FRZ-01", Severity.BLOCK)
    [record] = finding.evidence["records"]
    assert record["tx_hash"] == "c743e8f463596d2803c9240f903302821fc178bfd8c6652c8eb9590365c2cbfe"
    assert (record["company"], record["token"], record["amount"]) == (
        "Tether",
        "USDT",
        "99874.117384 USDT",
    )
    assert finding.summary.startswith("FROZEN: Freeze of USDT by Tether on Tron")


async def test_freeze_on_any_evm_chain_counts_for_bsc(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    """Q2: one answer covers the address on every EVM chain."""
    address = LAZARUS.lower()
    mock(network, f"/v1/check/{address}", reply("eagle_virtual/check_frozen_evm.json"))
    mock(network, f"/v1/address/{address}", reply("eagle_virtual/address_frozen_evm.json"))
    result = await check(conn, LAZARUS)
    [finding] = result.findings
    assert finding.rule_id == "R-FRZ-01"
    assert finding.evidence["record_count"] == 122
    assert len(finding.evidence["records"]) == 5
    assert "122 records" in finding.summary


async def test_released_address_is_review(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    """AT-05."""
    mock(network, f"/v1/check/{TRON_FROZEN}", reply("eagle_virtual/check_unfrozen_tron.json"))
    mock(network, f"/v1/address/{TRON_FROZEN}", reply("eagle_virtual/address_unfrozen_tron.json"))
    result = await check(conn, TRON_FROZEN)
    [finding] = result.findings
    assert (finding.rule_id, finding.severity) == ("R-FRZ-02", Severity.REVIEW)
    assert finding.evidence["records"][0]["is_release"] is True


async def test_a_chain_it_cannot_vouch_for_makes_it_stale_and_is_asked_again(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    """AT-06: the engine turns this into R-SYS-01 and INCOMPLETE."""
    route = mock(network, f"/v1/check/{TRON_CLEAN}", reply("eagle_virtual/check_unvouched.json"))
    for _ in range(2):
        result = await check(conn, TRON_CLEAN)
        assert result.status is SourceStatus.stale
        assert "Polygon (scan behind)" in result.summary
    assert route.call_count == 2


async def test_rate_limit_is_waited_out_when_short(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    """AT-07."""
    mock(
        network,
        f"/v1/check/{TRON_CLEAN}",
        httpx.Response(429, headers={"Retry-After": "1"}),
        reply("eagle_virtual/check_clear.json"),
    )
    sleeps = Sleeps()
    result = await check(conn, TRON_CLEAN, sleeps=sleeps)
    assert result.status is SourceStatus.ok
    assert 1.0 in sleeps.calls


async def test_rate_limit_with_a_long_wait_is_an_error(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    """AT-07: still failing, so the check becomes INCOMPLETE."""
    mock(network, f"/v1/check/{TRON_CLEAN}", httpx.Response(429, headers={"Retry-After": "3600"}))
    result = await check(conn, TRON_CLEAN)
    assert result.status is SourceStatus.error
    assert "retry in 3600 s" in result.summary


async def test_refused_key_is_an_error(conn: sqlite3.Connection, network: respx.MockRouter) -> None:
    mock(network, f"/v1/check/{TRON_CLEAN}", reply("eagle_virtual/error_invalid_key.json", 401))
    result = await check(conn, TRON_CLEAN)
    assert result.status is SourceStatus.error
    assert result.summary.endswith("(invalid_key)")


async def test_missing_key_is_an_error_without_a_request(conn: sqlite3.Connection) -> None:
    result = await check(conn, TRON_CLEAN, key=None)
    assert result.status is SourceStatus.error
    assert "EAGLE_VIRTUAL_API_KEY" in result.summary


async def test_network_failure_is_an_error(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    network.get(f"{BASE}/v1/check/{TRON_CLEAN}").mock(side_effect=httpx.ConnectError("down"))
    result = await check(conn, TRON_CLEAN)
    assert result.status is SourceStatus.error
    assert "could not reach Eagle Virtual" in result.summary


async def test_unknown_verdict_is_an_error(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    body = {**load("eagle_virtual/check_clear.json"), "verdict": "MAYBE"}
    mock(network, f"/v1/check/{TRON_CLEAN}", httpx.Response(200, json=body))
    assert (await check(conn, TRON_CLEAN)).status is SourceStatus.error


async def test_answers_are_reused_within_the_ttl(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    route = mock(network, f"/v1/check/{TRON_CLEAN}", reply("eagle_virtual/check_clear.json"))
    first = await check(conn, TRON_CLEAN)
    second = await check(conn, TRON_CLEAN)
    assert route.call_count == 1
    assert (first.evidence_meta["cached"], second.evidence_meta["cached"]) == (False, True)
    assert second.attribution == CREDIT


async def test_health_reports_usage_and_warns_near_the_limit(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    usage = load("eagle_virtual/usage.json")
    network.get(f"{BASE}/v1/usage").mock(
        side_effect=[
            httpx.Response(200, json=usage),
            httpx.Response(200, json={**usage, "calls_today": 850}),
        ]
    )
    async with httpx.AsyncClient() as http:
        adapter = EagleVirtualAdapter(
            http,
            SecretStr("ev_live_test"),
            EagleVirtual(),
            ResponseCache(conn, 900),
            max_retry_after=10,
            sleep=Sleeps(),
        )
        calm, busy = await adapter.health(), await adapter.health()
    assert calm.status is SourceStatus.ok
    assert "4 of 1,000 calls used today" in calm.detail
    assert calm.warnings == ()
    assert busy.warnings == ("850 of today's 1,000 Eagle Virtual calls are used",)


# Key rotation (Q19): a paid plan's keys take turns when one's day is used up.
KEY_1, KEY_2, KEY_3 = (SecretStr(f"ev_live_key{n}") for n in (1, 2, 3))
SPENT_UNTIL_MIDNIGHT = "43200"  # 12 hours from NOW, which is noon UTC


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Keyed:
    """Eagle Virtual answering each key by its plan, and noting which key asked. A spent key gets
    429 with a Retry-After to midnight UTC, which is all the spec says of that answer (V16)."""

    def __init__(self, network: respx.MockRouter, plans: dict[str, str]) -> None:
        self.plans = plans
        self.spent: set[str] = set()
        self.asked: list[str] = []
        network.get(url__regex=rf"{BASE}/v1/(check|address)/\w+").mock(side_effect=self._answer)
        network.get(f"{BASE}/v1/usage").mock(side_effect=self._usage)

    @staticmethod
    def _key(request: httpx.Request) -> str:
        return request.headers["Authorization"].removeprefix("Bearer ")

    def _answer(self, request: httpx.Request) -> httpx.Response:
        key = self._key(request)
        self.asked.append(key)
        if key in self.spent:
            return httpx.Response(429, headers={"Retry-After": SPENT_UNTIL_MIDNIGHT})
        return httpx.Response(200, json=load("eagle_virtual/check_clear.json"))

    def _usage(self, request: httpx.Request) -> httpx.Response:
        plan = self.plans[self._key(request)]
        limit = 1_000 if plan == "free" else 25_000
        usage = load("eagle_virtual/usage.json")
        return httpx.Response(200, json={**usage, "plan": plan, "daily_limit": limit})


def business(*numbers: int) -> dict[str, str]:
    return {f"ev_live_key{n}": "business" for n in numbers}


async def rotating(
    conn: sqlite3.Connection, keys: tuple[SecretStr, ...], clock: Clock, address: str = TRON_CLEAN
) -> Any:
    async with httpx.AsyncClient() as http:
        adapter = EagleVirtualAdapter(
            http,
            keys,
            EagleVirtual(),
            ResponseCache(conn, 0, clock),  # nothing cached: every check asks
            max_retry_after=10,
            sleep=Sleeps(),
            now=clock,
        )
        return await adapter.check(parse(address))


async def test_the_next_key_is_used_when_one_is_used_up(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    eagle = Keyed(network, business(1, 2))
    eagle.spent.add("ev_live_key1")
    clock = Clock()
    result = await rotating(conn, (KEY_1, KEY_2), clock)
    assert (result.status, result.summary) == (SourceStatus.ok, "CLEAR")
    assert result.attribution is None  # a paid plan owes no credit line
    assert eagle.asked == ["ev_live_key1", "ev_live_key2"]
    # Key 1 rests until its Retry-After runs out, so the next check goes straight to key 2.
    await rotating(conn, (KEY_1, KEY_2), clock)
    assert eagle.asked[2:] == ["ev_live_key2"]
    # After midnight UTC, key 1 is first again.
    eagle.spent.clear()
    clock.now += timedelta(hours=12, seconds=1)
    await rotating(conn, (KEY_1, KEY_2), clock)
    assert eagle.asked[3:] == ["ev_live_key1"]


async def test_a_later_key_on_the_free_plan_is_never_used(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    """Eagle Virtual's terms forbid getting around its limits, and the Free plan counts per
    account: only the first key may be a Free plan key."""
    eagle = Keyed(network, {"ev_live_key1": "free", "ev_live_key2": "free", **business(3)})
    eagle.spent.add("ev_live_key1")
    result = await rotating(conn, (KEY_1, KEY_2, KEY_3), Clock())
    assert result.status is SourceStatus.ok
    assert eagle.asked == ["ev_live_key1", "ev_live_key3"]


async def test_every_key_used_up_is_an_error_saying_when_one_is_back(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    eagle = Keyed(network, {**business(1), "ev_live_key2": "free", **business(3)})
    eagle.spent.update({"ev_live_key1", "ev_live_key3"})
    clock = Clock()
    result = await rotating(conn, (KEY_1, KEY_2, KEY_3), clock)
    assert result.status is SourceStatus.error
    assert result.summary == (
        "every Eagle Virtual key that can be used is over its plan's limit; the first answers"
        " again at 2026-09-29 00:00 UTC; key 2 is on the free plan, so it is not used"
    )
    again = await rotating(conn, (KEY_1, KEY_2, KEY_3), clock)
    assert again.summary == result.summary
    assert eagle.asked == ["ev_live_key1", "ev_live_key3"]  # resting keys are not asked again


async def test_a_key_whose_plan_cannot_be_read_is_not_used(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    eagle = Keyed(network, business(1))
    eagle.spent.add("ev_live_key1")
    network.get(f"{BASE}/v1/usage").mock(side_effect=httpx.ConnectError("down"))
    result = await rotating(conn, (KEY_1, KEY_2), Clock())
    assert result.status is SourceStatus.error
    assert "the plan of key 2 could not be read (could not reach Eagle Virtual" in result.summary
    assert eagle.asked == ["ev_live_key1"]


async def test_health_names_each_key(conn: sqlite3.Connection, network: respx.MockRouter) -> None:
    eagle = Keyed(network, {**business(1, 2), "ev_live_key3": "free"})
    eagle.spent.add("ev_live_key1")
    clock = Clock()
    await rotating(conn, (KEY_1, KEY_2, KEY_3), clock)
    async with httpx.AsyncClient() as http:
        adapter = EagleVirtualAdapter(
            http,
            (KEY_1, KEY_2, KEY_3),
            EagleVirtual(),
            ResponseCache(conn, 0, clock),
            max_retry_after=10,
            sleep=Sleeps(),
            now=clock,
        )
        health = await adapter.health()
    assert health.status is SourceStatus.ok
    assert health.detail == (
        "2 of 3 keys in use (business plan), 8 of 50,000 calls used today (resets at midnight UTC)"
    )
    assert health.warnings == (
        "key 1 rests until 2026-09-29 00:00 UTC",
        "key 3 is on the free plan, so it is never used: only a paid plan's keys take turns",
    )


async def test_health_with_no_key_answering_is_an_error(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    network.get(f"{BASE}/v1/usage").mock(
        return_value=reply("eagle_virtual/error_invalid_key.json", 401)
    )
    async with httpx.AsyncClient() as http:
        adapter = EagleVirtualAdapter(
            http,
            (KEY_1, KEY_2),
            EagleVirtual(),
            ResponseCache(conn, 0),
            max_retry_after=10,
            sleep=Sleeps(),
        )
        health = await adapter.health()
    assert (health.status, health.detail) == (SourceStatus.error, "no Eagle Virtual key answered")
    assert [w[:6] for w in health.warnings] == ["key 1:", "key 2:"]
    assert all(w.endswith("(invalid_key)") for w in health.warnings)
