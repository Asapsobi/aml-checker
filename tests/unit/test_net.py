from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from amlcheck.net import RateLimiter, request, retry_after

URL = "https://api.example.test/thing"


class Sleeps:
    """Records the waits asked for instead of waiting, and moves a fake clock forward."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.clock = 0.0

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock += seconds


async def test_retry_after_is_honoured_then_the_answer_returned(network: respx.MockRouter) -> None:
    """AT-07."""
    route = network.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    sleeps = Sleeps()
    async with httpx.AsyncClient() as client:
        response = await request(client, "GET", URL, max_retry_after=10, sleep=sleeps)
    assert response.status_code == 200
    assert sleeps.calls == [2.0]
    assert route.call_count == 2


async def test_a_wait_longer_than_allowed_is_not_waited_out(network: respx.MockRouter) -> None:
    network.get(URL).respond(429, headers={"Retry-After": "3600"})
    sleeps = Sleeps()
    async with httpx.AsyncClient() as client:
        response = await request(client, "GET", URL, max_retry_after=10, sleep=sleeps)
    assert response.status_code == 429
    assert sleeps.calls == []


async def test_server_errors_are_retried_then_the_last_answer_returned(
    network: respx.MockRouter,
) -> None:
    route = network.get(URL).respond(503)
    sleeps = Sleeps()
    async with httpx.AsyncClient() as client:
        response = await request(client, "GET", URL, max_retry_after=10, sleep=sleeps)
    assert response.status_code == 503
    assert route.call_count == 3
    assert sleeps.calls == [0.5, 1.0]


async def test_network_errors_are_retried_then_raised(network: respx.MockRouter) -> None:
    route = network.get(URL).mock(side_effect=httpx.ConnectError("down"))
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError):
            await request(client, "GET", URL, max_retry_after=10, sleep=Sleeps())
    assert route.call_count == 3


async def test_other_statuses_come_back_at_once(network: respx.MockRouter) -> None:
    route = network.get(URL).respond(403)
    async with httpx.AsyncClient() as client:
        response = await request(client, "GET", URL, max_retry_after=10, sleep=Sleeps())
    assert response.status_code == 403
    assert route.call_count == 1


def test_retry_after_reads_both_header_forms() -> None:
    def wait(value: str | None) -> float | None:
        headers = {"Retry-After": value} if value is not None else {}
        return retry_after(httpx.Response(429, headers=headers))

    assert wait("7") == 7.0
    assert wait(None) is None
    assert wait("soon") is None
    in_a_minute = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    assert 55 <= (wait(in_a_minute) or 0) <= 60
    assert wait(format_datetime(datetime(2020, 1, 1, tzinfo=UTC), usegmt=True)) == 0.0


async def test_rate_limiter_spaces_requests_evenly() -> None:
    sleeps = Sleeps()
    limiter = RateLimiter(2, sleep=sleeps, clock=lambda: sleeps.clock)
    for _ in range(3):
        await limiter.wait()
    assert sleeps.calls == [0.5, 0.5]
