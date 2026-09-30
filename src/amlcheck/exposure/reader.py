"""Transfer histories for one check: read through the response cache, and fetched once when two
sources ask for the same address at the same time (1-hop and 2-hop exposure, which run together)."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta

from amlcheck.core.clock import utcnow
from amlcheck.core.models import Chain
from amlcheck.exposure.history import History, HistorySource
from amlcheck.storage.cache import ResponseCache

CACHE_SOURCE = "exposure"


class HistoryReader:
    def __init__(
        self,
        source: HistorySource,
        cache: ResponseCache,
        lookback_days: int,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._source = source
        self._cache = cache
        self._lookback = timedelta(days=lookback_days)
        self._now = now
        self._pending: dict[str, asyncio.Future[History]] = {}

    @property
    def chain(self) -> Chain:
        return self._source.chain

    async def read(self, address: str, limit: int, *, first_activity: bool = True) -> History:
        """The address's USDT transfers over the lookback, newest first, at most `limit`, and when
        it was first active unless `first_activity` is False (the 2-hop walk)."""
        since = self._now() - self._lookback
        key = f"{CACHE_SOURCE}:{self.chain.value}:{address}:{since.date().isoformat()}:{limit}"
        if not first_activity:
            key += ":transfers"  # a first activity of None here means "not asked", never "none"
        cached = self._cache.get(key)
        if cached is not None:
            return History.from_json(cached)
        pending = self._pending.get(key)
        if pending is None:
            pending = asyncio.ensure_future(self._fetch(key, address, since, limit, first_activity))
            self._pending[key] = pending
            pending.add_done_callback(lambda _: self._pending.pop(key, None))
        # A reader that gives up (a timeout) must not cancel the fetch for the others waiting.
        return await asyncio.shield(pending)

    async def _fetch(
        self, key: str, address: str, since: datetime, limit: int, first_activity: bool
    ) -> History:
        history = await self._source.fetch(address, since, limit, first_activity=first_activity)
        self._cache.put(key, CACHE_SOURCE, history.to_json())
        return history
