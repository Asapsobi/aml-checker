"""HTTP for every source: one client, per-source rate limits, retries that honour Retry-After."""

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from tenacity import AsyncRetrying, RetryCallState, retry_if_exception, stop_after_attempt

from amlcheck import __version__

USER_AGENT = f"amlcheck/{__version__}"
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
Sleep = Callable[[float], Awaitable[None]]


def new_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True
    )


class RateLimiter:
    """Spaces the requests to one source so that at most `per_second` go out each second."""

    def __init__(
        self,
        per_second: float,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval = 1 / per_second
        self._sleep = sleep
        self._clock = clock
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._next > now:
                await self._sleep(self._next - now)
                now = self._next
            self._next = now + self._interval


def retry_after(response: httpx.Response) -> float | None:
    """The wait a server asked for, in seconds, from a Retry-After header in either form."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


class _Retry(Exception):
    def __init__(self, response: httpx.Response) -> None:
        super().__init__(f"HTTP {response.status_code}")
        self.response = response
        self.wait = retry_after(response)


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retry_after: float,
    limiter: RateLimiter | None = None,
    attempts: int = 3,
    retry_statuses: Collection[int] = RETRY_STATUSES,
    sleep: Sleep = asyncio.sleep,
    **kwargs: Any,
) -> httpx.Response:
    """Send a request, retrying network errors and the statuses in `retry_statuses`.

    A Retry-After longer than `max_retry_after` is not waited out: that response comes back to the
    caller to report, so a check never stalls. Network errors that persist are raised.
    """

    def should_retry(error: BaseException) -> bool:
        if isinstance(error, _Retry):
            return error.wait is None or error.wait <= max_retry_after
        return isinstance(error, httpx.TransportError)

    def wait(state: RetryCallState) -> float:
        error = state.outcome.exception() if state.outcome else None
        if isinstance(error, _Retry) and error.wait is not None:
            return error.wait
        return float(min(0.5 * 2 ** (state.attempt_number - 1), 4.0))

    try:
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait,
            retry=retry_if_exception(should_retry),
            sleep=sleep,
            reraise=True,
        ):
            with attempt:
                if limiter is not None:
                    await limiter.wait()
                response = await client.request(method, url, **kwargs)
                if response.status_code in retry_statuses:
                    raise _Retry(response)
                return response
    except _Retry as final:
        return final.response
    raise AssertionError("unreachable: every attempt returns or raises")
