"""TRON USDT: Tether's blacklist, from a live isBlackListed call and a local index of its events.

The live call says whether the address is blacklisted now; the index adds the history and the
transactions that prove it (docs/verification.md, V5). Tether can retire the contract
(`deprecate`), after which its blacklist is not the one in force, so every sync first asks
`deprecated()`. The index reads only confirmed blocks, and each sync re-reads the last ten minutes
so nothing is missed at the edge; an event already stored is not stored twice.
"""

import asyncio
import sqlite3
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from pydantic import SecretStr

from amlcheck.core import rules
from amlcheck.core.address import tron_abi_word, tron_from_hex
from amlcheck.core.clock import from_iso, from_timestamp, iso, utcnow
from amlcheck.core.models import Address, Finding, SourceHealth, SourceResult, SourceStatus
from amlcheck.net import Sleep, request

SOURCE = "tron_usdt"
LABEL = "TRON USDT"
INDEX = "tron_usdt_index"
EVENTS = ("AddedBlackList", "RemovedBlackList", "DestroyedBlackFunds")
OVERLAP = timedelta(minutes=10)
USDT_DECIMALS = 6
# TronGrid answers a rate-limited request with 403 or 429 (docs/verification.md, V7).
RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})


class TronGridError(Exception):
    pass


class ContractDeprecated(TronGridError):
    pass


class TronGrid:
    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        key: SecretStr | None,
        *,
        max_retry_after: float,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._base = base_url
        self._headers = {"TRON-PRO-API-KEY": key.get_secret_value()} if key else {}
        self._max_retry_after = max_retry_after
        self._sleep = sleep

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = await request(
            self._http,
            method,
            url,
            headers=self._headers,
            max_retry_after=self._max_retry_after,
            retry_statuses=RETRY_STATUSES,
            sleep=self._sleep,
            **kwargs,
        )
        if response.status_code != 200:
            raise TronGridError(f"TronGrid answered HTTP {response.status_code}")
        data = response.json()
        if not isinstance(data, dict):
            raise TronGridError("TronGrid sent an answer that is not a JSON object")
        return data

    async def confirmed_head(self) -> tuple[int, datetime]:
        data = await self._json("POST", f"{self._base}/walletsolidity/getnowblock")
        header = data["block_header"]["raw_data"]
        return int(header["number"]), from_timestamp(header["timestamp"] / 1000)

    async def events(
        self, contract: str, name: str, since: datetime | None
    ) -> AsyncIterator[dict[str, Any]]:
        url: str | None = f"{self._base}/v1/contracts/{contract}/events"
        params: dict[str, Any] | None = {
            "event_name": name,
            "only_confirmed": "true",
            "order_by": "block_timestamp,asc",
            "limit": 200,
        }
        if since is not None and params is not None:
            params["min_block_timestamp"] = int(since.timestamp() * 1000)
        while url:
            data = await self._json("GET", url, params=params)
            if data.get("success") is not True:
                raise TronGridError(f"TronGrid could not list {name} events: {data.get('Error')}")
            for row in data.get("data") or []:
                yield row
            url = ((data.get("meta") or {}).get("links") or {}).get("next")
            params = None  # the next link carries its own query

    async def call(self, contract: str, signature: str, parameter: str = "") -> int:
        data = await self._json(
            "POST",
            f"{self._base}/wallet/triggerconstantcontract",
            json={
                "owner_address": contract,
                "contract_address": contract,
                "function_selector": signature,
                "parameter": parameter,
                "visible": True,
            },
        )
        if not (data.get("result") or {}).get("result") or not data.get("constant_result"):
            raise TronGridError(f"TronGrid could not call {signature}: {data.get('result')}")
        return int(data["constant_result"][0] or "0", 16)

    async def token_transfers(
        self, address: str, contract: str, since: datetime, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """The address's confirmed transfers of `contract` since `since`, newest first, at most
        `limit` of them. The flag is True when older transfers in the window were left unread."""
        url: str | None = f"{self._base}/v1/accounts/{address}/transactions/trc20"
        params: dict[str, Any] | None = {
            "contract_address": contract,
            "only_confirmed": "true",
            "order_by": "block_timestamp,desc",
            "min_timestamp": int(since.timestamp() * 1000),
            "limit": 200,
        }
        rows: list[dict[str, Any]] = []
        while url:
            data = await self._json("GET", url, params=params)
            if data.get("success") is not True:
                raise TronGridError(f"TronGrid could not list transfers: {data.get('Error')}")
            rows.extend(data.get("data") or [])
            url = ((data.get("meta") or {}).get("links") or {}).get("next")
            params = None
            if len(rows) > limit or (url and len(rows) >= limit):
                return rows[:limit], True
        return rows, False

    async def first_token_transfer(self, address: str, contract: str) -> datetime | None:
        data = await self._json(
            "GET",
            f"{self._base}/v1/accounts/{address}/transactions/trc20",
            params={
                "contract_address": contract,
                "only_confirmed": "true",
                "order_by": "block_timestamp,asc",
                "limit": 1,
            },
        )
        rows = data.get("data") or []
        return from_timestamp(rows[0]["block_timestamp"] / 1000) if rows else None

    async def created(self, address: str) -> datetime | None:
        """When the account was activated. An address can hold and move USDT without ever being
        activated, so None does not mean unused (docs/verification.md, V10)."""
        data = await self._json(
            "POST", f"{self._base}/wallet/getaccount", json={"address": address, "visible": True}
        )
        stamp = data.get("create_time")
        return from_timestamp(stamp / 1000) if stamp else None


@dataclass(frozen=True)
class IndexState:
    last_block: int
    last_block_time: datetime
    updated_at: datetime
    events: int


@dataclass(frozen=True)
class IndexSync:
    new_events: int
    head: int
    head_time: datetime
    first_sync: bool


@dataclass(frozen=True)
class Event:
    kind: str
    tx_hash: str
    block: int
    time: str
    amount: str | None


def index_state(conn: sqlite3.Connection) -> IndexState | None:
    row = conn.execute(
        "SELECT last_block, last_block_time, updated_at FROM index_state WHERE source = ?", (INDEX,)
    ).fetchone()
    if row is None or row[1] is None:
        return None
    count = conn.execute("SELECT COUNT(*) FROM issuer_events WHERE chain = 'tron'").fetchone()[0]
    return IndexState(row[0], from_iso(row[1]), from_iso(row[2]), count)


def _event_row(contract: str, event: dict[str, Any]) -> tuple[Any, ...]:
    result = event.get("result") or {}
    who = result.get("_user") or result.get("_blackListedUser")
    if not who:
        raise TronGridError(f"a {event.get('event_name')} event without an address: {result}")
    return (
        contract,
        tron_from_hex(who),
        event["event_name"],
        event["transaction_id"],
        int(event["block_number"]),
        iso(from_timestamp(event["block_timestamp"] / 1000)),
        result.get("_balance"),
    )


async def sync_index(
    conn: sqlite3.Connection,
    grid: TronGrid,
    contract: str,
    now: Callable[[], datetime] = utcnow,
) -> IndexSync:
    if await grid.call(contract, "deprecated()"):
        raise ContractDeprecated(
            f"Tether has deprecated the USDT contract {contract}, so its blacklist is no longer"
            " the one in force"
        )
    head, head_time = await grid.confirmed_head()
    state = index_state(conn)
    since = None if state is None else state.last_block_time - OVERLAP
    rows = [
        _event_row(contract, event)
        for name in EVENTS
        async for event in grid.events(contract, name, since)
    ]
    with conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO issuer_events (chain, token_contract, address_norm,"
            " event_type, tx_hash, block, block_time, amount) VALUES ('tron', ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        added = conn.total_changes - before
        conn.execute(
            "INSERT INTO index_state (source, last_block, last_block_time, updated_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (source) DO UPDATE SET"
            " last_block = excluded.last_block, last_block_time = excluded.last_block_time,"
            " updated_at = excluded.updated_at",
            (INDEX, head, iso(head_time), iso(now())),
        )
    return IndexSync(added, head, head_time, state is None)


def minutes(delta: timedelta) -> str:
    count = round(delta.total_seconds() / 60)
    return f"{count} minute" if count == 1 else f"{count} minutes"


def history(conn: sqlite3.Connection, address: str) -> list[Event]:
    return [
        Event(*row)
        for row in conn.execute(
            "SELECT event_type, tx_hash, block, block_time, amount FROM issuer_events"
            " WHERE chain = 'tron' AND address_norm = ? ORDER BY block, event_type",
            (address,),
        )
    ]


def usdt(raw: str | None) -> str:
    return f"{Decimal(raw or 0).scaleb(-USDT_DECIMALS):,} USDT"


def findings_for(
    blacklisted: bool, events: list[Event], observed_at: datetime
) -> tuple[Finding, ...]:
    added = [e for e in events if e.kind == "AddedBlackList"]
    removed = [e for e in events if e.kind == "RemovedBlackList"]
    destroyed = [e for e in events if e.kind == "DestroyedBlackFunds"]
    evidence: dict[str, Any] = {
        "is_blacklisted_now": blacklisted,
        "events": [asdict(e) for e in events],
    }
    if blacklisted:
        since = added[-1] if added else None
        seized = [e for e in destroyed if since is None or e.block >= since.block]
        state = "SEIZED" if seized else "FROZEN"
        what = (
            f"blacklisted by Tether since {since.time[:10]} (tx {since.tx_hash})"
            if since
            else "blacklisted by Tether (the index holds no AddedBlackList event for it)"
        )
        if seized:
            what += f"; {usdt(seized[-1].amount)} destroyed (tx {seized[-1].tx_hash})"
        return (
            rules.finding(
                rules.FRZ_01, SOURCE, f"{state}: {what}", {"state": state, **evidence}, observed_at
            ),
        )
    if added:
        first = added[0]
        released = (
            f" and released on {removed[-1].time[:10]} (tx {removed[-1].tx_hash})"
            if removed
            else ", and is not blacklisted now (the index holds no RemovedBlackList event)"
        )
        what = f"blacklisted by Tether on {first.time[:10]} (tx {first.tx_hash}){released}"
        return (
            rules.finding(
                rules.FRZ_02,
                SOURCE,
                f"UNFROZEN: {what}",
                {"state": "UNFROZEN", **evidence},
                observed_at,
            ),
        )
    return ()


class TronUsdtAdapter:
    source = SOURCE
    label = LABEL
    required = True

    def __init__(
        self,
        conn: sqlite3.Connection,
        grid: TronGrid,
        contract: str,
        max_lag: timedelta,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._conn = conn
        self._grid = grid
        self._contract = contract
        self._max_lag = max_lag
        self._now = now

    def _result(self, status: SourceStatus, summary: str, **kwargs: Any) -> SourceResult:
        return SourceResult(SOURCE, LABEL, self.required, status, summary, **kwargs)

    async def check(self, address: Address) -> SourceResult:
        if index_state(self._conn) is None:
            return self._result(
                SourceStatus.error,
                "the TRON blacklist index is not built yet: run `amlcheck sync tron-index`",
            )
        refresh_problem = None
        try:
            await sync_index(self._conn, self._grid, self._contract, self._now)
        except ContractDeprecated as e:
            return self._result(SourceStatus.error, str(e))
        except (TronGridError, httpx.HTTPError) as e:
            refresh_problem = f"the index could not be refreshed: {e}"
        try:
            blacklisted = bool(
                await self._grid.call(
                    self._contract, "isBlackListed(address)", tron_abi_word(address.normalized)
                )
            )
        except (TronGridError, httpx.HTTPError) as e:
            return self._result(SourceStatus.error, f"the live isBlackListed check failed: {e}")

        state = index_state(self._conn)
        if state is None:
            return self._result(SourceStatus.error, "the TRON blacklist index disappeared")
        events = history(self._conn, address.normalized)
        observed = self._now()
        lag = observed - state.last_block_time
        stale = lag > self._max_lag
        if blacklisted:
            summary = "blacklisted"
        elif any(e.kind == "AddedBlackList" for e in events):
            summary = "not blacklisted now; it was before"
        else:
            summary = "not blacklisted"
        if stale:
            summary += f"; the index is {minutes(lag)} behind the chain"
            if refresh_problem:
                summary += f" ({refresh_problem})"
        meta: dict[str, Any] = {
            "is_blacklisted_now": blacklisted,
            "index_block": state.last_block,
            "index_block_time": iso(state.last_block_time),
            "index_lag_seconds": int(lag.total_seconds()),
            "events": len(events),
        }
        if refresh_problem:
            meta["refresh_problem"] = refresh_problem
        return self._result(
            SourceStatus.stale if stale else SourceStatus.ok,
            summary,
            as_of=state.last_block_time,
            as_of_text=f"block {state.last_block:,}",
            findings=findings_for(blacklisted, events, observed),
            evidence_meta=meta,
        )

    async def health(self) -> SourceHealth:
        state = index_state(self._conn)
        if state is None:
            return SourceHealth(
                SOURCE,
                LABEL,
                SourceStatus.error,
                "the blacklist index is not built yet: run `amlcheck sync tron-index`",
            )
        lag = self._now() - state.last_block_time
        return SourceHealth(
            SOURCE,
            LABEL,
            SourceStatus.stale if lag > self._max_lag else SourceStatus.ok,
            f"{state.events:,} blacklist events up to block {state.last_block:,},"
            f" {minutes(lag)} ago (every check refreshes it)",
        )
