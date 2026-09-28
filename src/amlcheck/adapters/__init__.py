"""The sources a check runs (PRD §6): OFAC and Eagle Virtual on every chain, plus the chain's own
USDT freeze check."""

import asyncio
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx

from amlcheck.adapters.base import SourceAdapter
from amlcheck.adapters.bsc import BscUsdtAdapter
from amlcheck.adapters.eagle_virtual import EagleVirtualAdapter
from amlcheck.adapters.ofac import OfacAdapter
from amlcheck.adapters.tron import TronGrid, TronUsdtAdapter
from amlcheck.config import Config, Secrets
from amlcheck.core.clock import utcnow
from amlcheck.core.models import Chain
from amlcheck.net import Sleep
from amlcheck.storage.cache import ResponseCache


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
) -> list[SourceAdapter]:
    sources: list[SourceAdapter] = [
        OfacAdapter(conn, timedelta(hours=config.freshness.sanctions_max_age_hours), now),
        EagleVirtualAdapter(
            http,
            secrets.eagle_virtual_api_key,
            config.eagle_virtual,
            ResponseCache(conn, config.cache.target_ttl_seconds, now),
            max_retry_after=config.network.max_retry_after_seconds,
            sleep=sleep,
        ),
    ]
    if chain is Chain.tron:
        max_lag = timedelta(minutes=config.freshness.tron_index_max_lag_minutes)
        grid = tron_grid(http, config, secrets, sleep)
        sources.append(TronUsdtAdapter(conn, grid, config.tron.usdt_contract, max_lag, now))
    else:
        sources.append(BscUsdtAdapter())
    return sources
