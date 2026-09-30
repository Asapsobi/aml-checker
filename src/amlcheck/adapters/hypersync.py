"""Envio HyperSync: USDT transfer history on BSC (docs/verification.md, V12).

HyperSync reads raw chain logs in block order, oldest first. A request stops at a time or size
limit and says where (`next_block`), so every read here goes on from there to its end block.
"""

import asyncio
import math
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx
from pydantic import SecretStr

from amlcheck.core.clock import from_timestamp
from amlcheck.net import RETRY_STATUSES, Sleep, request

# keccak256("Transfer(address,address,uint256)"), the event of every ERC-20 and BEP-20 transfer.
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
PACE_SAMPLE = 100_000  # blocks over which the chain's pace is measured (about 12 hours on BSC)
MARGIN = 1_000  # blocks read before the estimated start of a window (about 8 minutes on BSC)
MIN_SAMPLE = 100  # transfers read before their pace is trusted to project the window's total
OVERSHOOT = 2  # a window with up to this many times the transfers still wanted is read whole
TRANSFER_FIELDS = ["block_number", "log_index", "transaction_hash", "topic1", "topic2", "data"]


class HyperSyncError(Exception):
    pass


@dataclass(frozen=True)
class TokenTransfer:
    block: int
    log_index: int
    tx_hash: str
    time: datetime
    sender: str
    recipient: str
    value: int  # in the token's smallest unit


def topic(address: str) -> str:
    """An address as a 32-byte log topic."""
    return "0x" + address.lower()[2:].rjust(64, "0")


def _seconds(value: Any) -> int:
    """Block timestamps come back as hex strings ("0x6abb9411")."""
    return int(value, 16) if isinstance(value, str) else int(value)


def _block_times(page: dict[str, Any]) -> dict[int, int]:
    return {
        int(block["number"]): _seconds(block["timestamp"])
        for batch in page.get("data") or []
        for block in batch.get("blocks") or []
    }


def _address(word: Any) -> str:
    if not (isinstance(word, str) and len(word) == 66):
        raise ValueError(f"not a 32-byte topic: {word!r}")
    return "0x" + word[-40:].lower()


def transfers_in(page: dict[str, Any]) -> list[TokenTransfer]:
    """The transfers in one answer, oldest first."""
    times = _block_times(page)
    rows = []
    for batch in page.get("data") or []:
        for log in batch.get("logs") or []:
            try:
                number = int(log["block_number"])
                rows.append(
                    TokenTransfer(
                        number,
                        int(log["log_index"]),
                        log["transaction_hash"],
                        from_timestamp(times[number]),
                        _address(log["topic1"]),
                        _address(log["topic2"]),
                        int(log["data"], 16),
                    )
                )
            except (KeyError, TypeError, ValueError) as e:
                raise HyperSyncError(f"HyperSync sent an unreadable transfer: {e}") from None
    return sorted(rows, key=lambda r: (r.block, r.log_index))


def _following(page: dict[str, Any], block: int) -> int | None:
    """Where the next request starts, or None once HyperSync holds nothing newer."""
    following = int(page["next_block"])
    if following > int(page["archive_height"]):
        return None
    if following <= block:
        raise HyperSyncError(f"HyperSync made no progress past block {block:,}")
    return following


class Budget:
    """A token's HyperSync budget, from the x-ratelimit-* headers of its answers (V15): the units
    left in the current one-minute window, what a query costs, and when the window resets.

    One budget serves every client in the process, so a batch or a 2-hop walk waits for the next
    window instead of being refused.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._remaining: int | None = None
        self._cost = 1
        self._reset_at = 0.0

    def seen(self, headers: Mapping[str, str]) -> None:
        try:
            remaining = int(headers["x-ratelimit-remaining"])
            reset = float(headers["x-ratelimit-reset"])
        except (KeyError, ValueError):
            return
        self._remaining = remaining
        self._reset_at = self._clock() + reset
        with suppress(KeyError, ValueError):
            self._cost = max(int(headers["x-ratelimit-cost"]), 1)

    def wait(self) -> float:
        """Seconds until a query fits: none while the window has room for one."""
        if self._remaining is None or self._remaining >= self._cost:
            return 0.0
        return max(self._reset_at - self._clock(), 0.0)

    def renewed(self) -> None:
        """The window has reset: its count is unknown until the next answer."""
        self._remaining = None


BUDGET = Budget()
# (url, hour) -> (the moment it was found for, its start block): good for any later moment
_START_BLOCKS: dict[tuple[str, int], tuple[float, int]] = {}


class HyperSync:
    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        token: SecretStr,
        *,
        max_retry_after: float,
        max_wait: float = 65.0,
        sleep: Sleep = asyncio.sleep,
        budget: Budget = BUDGET,
    ) -> None:
        self._http = http
        self._url = url.rstrip("/")
        self._token = token
        self._max_retry_after = max_retry_after
        self._max_wait = max_wait
        self._sleep = sleep
        self._budget = budget

    async def _send(self, method: str, path: str, body: dict[str, Any] | None) -> httpx.Response:
        """One request, paced to the budget. A 429 is waited out, up to `max_wait` seconds (a
        minute window, D33); a longer wait is an error, and so an INCOMPLETE result."""
        for attempt in range(3):
            wait = self._budget.wait()
            if wait > self._max_wait:
                raise HyperSyncError(
                    f"HyperSync's budget for this minute is spent; it resets in {wait:.0f} s"
                )
            if wait:
                await self._sleep(wait)
                self._budget.renewed()
            try:
                response = await request(
                    self._http,
                    method,
                    f"{self._url}{path}",
                    json=body,
                    headers={"Authorization": f"Bearer {self._token.get_secret_value()}"},
                    max_retry_after=self._max_retry_after,
                    retry_statuses=RETRY_STATUSES - {429},
                    sleep=self._sleep,
                )
            except httpx.HTTPError as e:
                raise HyperSyncError(f"could not reach HyperSync ({type(e).__name__})") from None
            self._budget.seen(response.headers)
            if response.status_code != 429:
                return response
            if not self._budget.wait():  # a refusal that says nothing of the window
                await self._sleep(0.5 * 2**attempt)
        raise HyperSyncError("HyperSync kept answering HTTP 429: too many requests")

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        response = await self._send(method, path, body)
        try:
            answer = response.json()
        except ValueError:
            answer = None
        if response.status_code != 200:
            reason = answer.get("error") if isinstance(answer, dict) else None
            detail = f": {reason}" if reason else ""
            raise HyperSyncError(f"HyperSync answered HTTP {response.status_code}{detail}")
        if answer is None:
            raise HyperSyncError("HyperSync's answer is not JSON")
        return answer

    async def _query(self, body: dict[str, Any]) -> dict[str, Any]:
        page = await self._call("POST", "/query", body)
        if not isinstance(page, dict) or "next_block" not in page or "archive_height" not in page:
            raise HyperSyncError("HyperSync's answer has no next_block or archive_height")
        return page

    async def height(self) -> int:
        """The newest block HyperSync holds."""
        return int((await self._call("GET", "/height"))["height"])

    async def block_time(self, number: int) -> int:
        """A block's timestamp, in seconds."""
        page = await self._query(
            {
                "from_block": number,
                "to_block": number + 1,
                "include_all_blocks": True,
                "field_selection": {"block": ["number", "timestamp"]},
            }
        )
        times = _block_times(page)
        if number not in times:
            raise HyperSyncError(f"HyperSync did not return block {number:,}")
        return times[number]

    async def start_block(self, moment: datetime, head: int) -> int:
        """A block at or before `moment`, and no more than about an hour before it.

        The chain's pace (seconds per block) near the head gives a first estimate. While the block
        estimated is still after `moment`, the pace near that block gives the next one, so a chain
        that used to be faster is still found in a step or two. One that used to be slower puts
        the estimate too early instead, and a step forward trims it.
        """
        # Every read in the same hour starts from the same block: it is at or before any later
        # moment, and reading at most an hour of blocks too many costs less than finding it again.
        bucket = (self._url, int(moment.timestamp()) // 3600)
        found = _START_BLOCKS.get(bucket)
        if found is not None and moment.timestamp() >= found[0]:
            return found[1]
        block = await self._find_start(moment, head)
        _START_BLOCKS.clear()
        _START_BLOCKS[bucket] = (moment.timestamp(), block)
        return block

    async def _find_start(self, moment: datetime, head: int) -> int:
        target = moment.timestamp()
        block = head
        seen = await self.block_time(block)
        pace = None
        for _ in range(10):
            if seen <= target:
                break
            sample = min(PACE_SAMPLE, block)
            if sample == 0:
                return 0
            pace = max((seen - await self.block_time(block - sample)) / sample, 0.01)
            block = max(block - math.ceil((seen - target) / pace) - MARGIN, 0)
            seen = await self.block_time(block)
        else:
            raise HyperSyncError(f"could not find the block at {moment:%Y-%m-%d %H:%M} UTC")
        ahead = min(PACE_SAMPLE, head - block)
        if pace is not None and target - seen > 2 * MARGIN * pace and ahead > 0:
            # Well before `moment`: step forward at the pace of the blocks just ahead.
            local = max((await self.block_time(block + ahead) - seen) / ahead, 0.01)
            candidate = min(block + math.floor((target - seen) / local) - MARGIN, head)
            if candidate > block and await self.block_time(candidate) <= target:
                block = candidate
        return block

    async def _read(
        self,
        address: str,
        contract: str,
        start: int,
        end: int,
        cap: int | None,
        enough: Callable[[int, int], bool] | None = None,
    ) -> tuple[list[TokenTransfer], int]:
        """Transfers of `contract` to or from `address` in blocks [start, end), oldest first.
        Reading stops once `cap` are read (None: never), or once `enough(transfers read, block
        reached)` says so, and returns the block where it stopped: `end` when the range was read
        whole."""
        word = topic(address)
        body = {
            "to_block": end,
            "logs": [
                {"address": [contract], "topics": [[TRANSFER], [word]]},
                {"address": [contract], "topics": [[TRANSFER], [], [word]]},
            ],
            "field_selection": {"block": ["number", "timestamp"], "log": TRANSFER_FIELDS},
        }
        rows: list[TokenTransfer] = []
        block = start
        while block < end and (cap is None or len(rows) < cap):
            limits = {} if cap is None else {"max_num_logs": cap - len(rows)}
            page = await self._query({**body, "from_block": block, **limits})
            rows += transfers_in(page)
            following = _following(page, block)
            block = end if following is None else following
            if enough is not None and block < end and enough(len(rows), block):
                break
        return rows, min(block, end)

    async def token_transfers(
        self, address: str, contract: str, since: datetime, limit: int
    ) -> tuple[list[TokenTransfer], bool]:
        """The address's transfers of `contract` since `since`, newest first, at most `limit`. The
        flag is True when older ones in the window were left unread.

        Most addresses are read whole, oldest first. Once the part read shows more than `limit`
        in the window, the newest are read instead, window by window from the head down, each
        window small enough to read whole: HyperSync reads oldest first, so a window cannot be
        cut short at its newest end. The first blocks read can come from just before `since`
        (see `start_block`): they count towards how busy the address is, not towards the result.
        """
        head = await self.height()
        start = await self.start_block(since, head)
        end = head + 1

        def recent(rows: list[TokenTransfer]) -> list[TokenTransfer]:
            return [r for r in rows if r.time >= since]

        def busy(count: int, reached: int) -> bool:
            projected = count * (end - start) / max(reached - start, 1)
            return count >= MIN_SAMPLE and projected > limit

        rows, reached = await self._read(address, contract, start, end, limit + 1, busy)
        if reached == end:
            rows = recent(rows)[::-1]
            return rows[:limit], len(rows) > limit
        newest: list[TokenTransfer] = []
        per_row = (reached - start) / max(len(rows), 1)  # blocks per transfer so far
        top, size = end, max(math.ceil((limit + 1) * per_row / 2), 1)
        while len(newest) <= limit and top > start:
            low = max(start, top - size)
            wanted = limit + 1 - len(newest)
            # A window is read whole unless it holds far more than wanted; one block always is.
            cap = None if top - low == 1 else wanted * OVERSHOOT
            window, stopped = await self._read(address, contract, low, top, cap)
            if stopped < top:
                # Far more than wanted: narrow the window to what its oldest part suggests.
                per_row = (stopped - low) / len(window)
                size = max(min((top - low) // 2, math.floor(wanted * per_row)), 1)
                continue
            newest += reversed(recent(window))
            if window:
                per_row = (top - low) / len(window)
                size = max(math.ceil((limit + 1 - len(newest)) * per_row * 1.2), 1)
            else:
                size *= 2
            top = low
        return newest[:limit], len(newest) > limit

    async def first_activity(self, address: str) -> datetime | None:
        """When the address first sent or received a transaction, or first sent or received any
        token (a Transfer log naming it); None if it never did. BNB paid out to it by a contract
        call, with no log, is not counted."""
        word = topic(address)
        body: dict[str, Any] = {
            "transactions": [{"from": [address]}, {"to": [address]}],
            "logs": [{"topics": [[TRANSFER], [word]]}, {"topics": [[TRANSFER], [], [word]]}],
            "field_selection": {
                "block": ["number", "timestamp"],
                "transaction": ["block_number"],
                "log": ["block_number"],
            },
            "max_num_transactions": 1,
            "max_num_logs": 1,
        }
        block = 0
        while True:
            page = await self._query({**body, "from_block": block})
            found = [
                int(item["block_number"])
                for batch in page.get("data") or []
                for kind in ("transactions", "logs")
                for item in batch.get(kind) or []
            ]
            if found:
                first = min(found)
                seconds = _block_times(page).get(first)
                if seconds is None:
                    seconds = await self.block_time(first)
                return from_timestamp(seconds)
            following = _following(page, block)
            if following is None:
                return None
            block = following
