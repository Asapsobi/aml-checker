"""The sources a check runs (PRD §6): OFAC and Eagle Virtual on every chain, the chain's own USDT
freeze check, and the 1-hop exposure of the address's transfers."""

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import Decimal

import httpx

from amlcheck.adapters.base import SourceAdapter
from amlcheck.adapters.bsc import BscUsdtAdapter
from amlcheck.adapters.eagle_virtual import EagleVirtualAdapter
from amlcheck.adapters.exposure import ExposureAdapter
from amlcheck.adapters.hypersync import HyperSync
from amlcheck.adapters.ofac import OfacAdapter
from amlcheck.adapters.tron import TronGrid, TronUsdtAdapter
from amlcheck.adapters.two_hop import TwoHopAdapter
from amlcheck.config import Config, Secrets
from amlcheck.core.clock import utcnow
from amlcheck.core.models import Chain
from amlcheck.exposure.history import BscHistory, HistorySource, TronHistory
from amlcheck.exposure.reader import HistoryReader
from amlcheck.net import RateLimiter, Sleep
from amlcheck.storage.cache import ResponseCache

BSC_HISTORY_MISSING = (
    "HYPERSYNC_API_TOKEN is not set: BSC transfer history needs a free Envio HyperSync token"
    " (docs/verification.md, V12)"
)


def wants_two_hop(config: Config, amount: str | None) -> bool:
    """Whether a check of this amount includes the 2-hop walk (Q15). 0 turns it off."""
    least = Decimal(str(config.two_hop.auto_amount_usdt))
    return amount is not None and least > 0 and Decimal(amount) >= least


def tron_grid(
    http: httpx.AsyncClient, config: Config, secrets: Secrets, sleep: Sleep = asyncio.sleep
) -> TronGrid:
    return TronGrid(
        http,
        config.tron.trongrid_url,
        secrets.trongrid_api_key,
        max_retry_after=config.network.max_retry_after_seconds,
        sleep=sleep,
    )


def build(
    chain: Chain,
    *,
    conn: sqlite3.Connection,
    http: httpx.AsyncClient,
    config: Config,
    secrets: Secrets,
    now: Callable[[], datetime] = utcnow,
    sleep: Sleep = asyncio.sleep,
    eagle_limiter: RateLimiter | None = None,
    two_hop: bool = False,
) -> list[SourceAdapter]:
    """The sources for one check. With `two_hop`, the 2-hop walk too (`investigate`, and checks of
    a large amount: Q15)."""
    cache = ResponseCache(conn, config.cache.target_ttl_seconds, now)
    eagle = EagleVirtualAdapter(
        http,
        secrets.eagle_virtual_keys(),
        config.eagle_virtual,
        cache,
        max_retry_after=config.network.max_retry_after_seconds,
        sleep=sleep,
        limiter=eagle_limiter,
        now=now,
    )
    sources: list[SourceAdapter] = [
        OfacAdapter(conn, timedelta(hours=config.freshness.sanctions_max_age_hours), now),
        eagle,
    ]
    history: HistorySource | None = None
    if chain is Chain.tron:
        max_lag = timedelta(minutes=config.freshness.tron_index_max_lag_minutes)
        grid = tron_grid(http, config, secrets, sleep)
        sources.append(TronUsdtAdapter(conn, grid, config.tron.usdt_contract, max_lag, now))
        history = TronHistory(grid, config.tron.usdt_contract)
    else:
        sources.append(BscUsdtAdapter())
        if secrets.hypersync_api_token is not None:
            hypersync = HyperSync(
                http,
                config.bsc.hypersync_url,
                secrets.hypersync_api_token,
                max_retry_after=config.network.max_retry_after_seconds,
                sleep=sleep,
            )
            history = BscHistory(hypersync, config.bsc.usdt_contract)
    lookups = config.eagle_virtual.max_remote_counterparty_lookups
    # One reader for both walks, so the address's own history is read once.
    reader = HistoryReader(history, cache, config.exposure.lookback_days, now) if history else None
    sources.append(
        ExposureAdapter(
            conn,
            chain,
            history,
            config.exposure,
            config.heuristics,
            cache,
            unavailable=BSC_HISTORY_MISSING,
            remote=eagle.verdict if lookups else None,
            max_remote=lookups,
            now=now,
            reader=reader,
        )
    )
    if two_hop:
        sources.append(
            TwoHopAdapter(
                conn,
                reader,
                config.exposure,
                config.two_hop,
                unavailable=BSC_HISTORY_MISSING,
                now=now,
            )
        )
    return sources
