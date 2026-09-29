"""Etherscan V2: USDT transfer history on BSC (docs/verification.md, V11).

The key travels in the query string, so nothing raised here may include a URL: an error that
reaches the audit log or the screen must never carry the key.
"""

import asyncio
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretStr

from amlcheck.core.clock import from_timestamp
from amlcheck.net import RateLimiter, Sleep, request

PAGE = 1000  # the most Etherscan returns per request


class EtherscanError(Exception):
    pass


class Etherscan:
    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        key: SecretStr,
        chain_id: int,
        *,
        per_second: float,
        max_retry_after: float,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._url = url
        self._key = key
        self._chain_id = chain_id
        self._limiter = RateLimiter(per_second, sleep=sleep)
        self._max_retry_after = max_retry_after
        self._sleep = sleep

    async def _result(self, **params: str | int) -> Any:
        """The `result` of a successful call. "No transactions found" is an answer, not an error."""
        for attempt in range(3):
            try:
                response = await request(
                    self._http,
                    "GET",
                    self._url,
                    params={
                        "chainid": self._chain_id,
                        **params,
                        "apikey": self._key.get_secret_value(),
                    },
                    limiter=self._limiter,
                    max_retry_after=self._max_retry_after,
                    sleep=self._sleep,
                )
            except httpx.HTTPError as e:
                raise EtherscanError(f"could not reach Etherscan ({type(e).__name__})") from None
            if response.status_code != 200:
                raise EtherscanError(f"Etherscan answered HTTP {response.status_code}")
            body = response.json()
            if body.get("status") == "1":
                return body["result"]
            if body.get("message") == "No transactions found":
                return []
            reason = str(body.get("result") or body.get("message"))
            if "rate limit" in reason.lower() and attempt < 2:
                await self._sleep(1.0)
                continue
            raise EtherscanError(f"Etherscan refused the request: {reason}")
        raise EtherscanError("Etherscan kept refusing the requests as too frequent")

    async def block_at(self, moment: datetime) -> int:
        """The first block at or after `moment`."""
        result = await self._result(
            module="block",
            action="getblocknobytime",
            timestamp=int(moment.timestamp()),
            closest="after",
        )
        return int(result)

    async def token_transfers(
        self, address: str, contract: str, start_block: int, limit: int
    ) -> tuple[list[dict[str, Any]], bool]:
        """The address's transfers of `contract` from `start_block` on, newest first, at most
        `limit`. The flag is True when older ones in the window were left unread.

        Etherscan returns at most 10,000 rows per query, so each page of 1,000 moves the end block
        down instead of turning pages. Rows have no log index to tell two alike apart, so the block
        at a page's edge is dropped and read again whole with the next page.
        """
        rows: list[dict[str, Any]] = []
        # No end block on the first page: Etherscan then reads to the latest block. (Its examples
        # use endblock=99999999, which BSC passed long ago: it is past block 124 million.)
        end: dict[str, int] = {}
        while True:
            page = await self._result(
                module="account",
                action="tokentx",
                contractaddress=contract,
                address=address,
                startblock=start_block,
                **end,
                page=1,
                offset=PAGE,
                sort="desc",
            )
            if len(page) < PAGE:
                rows.extend(page)
                return rows[:limit], len(rows) > limit
            edge = int(page[-1]["blockNumber"])
            whole = [r for r in page if int(r["blockNumber"]) > edge]
            if not whole:
                raise EtherscanError(f"more than {PAGE:,} transfers of {address} in block {edge:,}")
            rows.extend(whole)
            if len(rows) > limit:
                return rows[:limit], True
            end = {"endblock": edge}

    async def first_activity(self, address: str) -> datetime | None:
        """The time of the address's first transaction or token transfer of any kind."""
        firsts = []
        for action in ("txlist", "tokentx"):
            rows = await self._result(
                module="account", action=action, address=address, page=1, offset=1, sort="asc"
            )
            if rows:
                firsts.append(int(rows[0]["timeStamp"]))
        return from_timestamp(min(firsts)) if firsts else None
