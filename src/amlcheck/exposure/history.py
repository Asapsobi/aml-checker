"""A screened address's USDT transfers over the lookback, and when it was first active.

Transfers of 0 USDT are left out: on TRON they are address-poisoning spam that anyone can send to
any address, not dealings of the address (decided 2026-09-28, docs/verification.md).
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from amlcheck.adapters.tron import TronGrid
from amlcheck.core.clock import from_iso, from_timestamp, iso
from amlcheck.core.models import Chain


@dataclass(frozen=True)
class Transfer:
    tx_hash: str
    time: datetime
    sender: str
    recipient: str
    amount: Decimal


@dataclass(frozen=True)
class History:
    transfers: tuple[Transfer, ...]  # newest first
    since: datetime
    complete: bool  # False when older transfers in the window were left unread
    first_activity: datetime | None  # None: never active on chain
    zero_value: int = 0  # 0 USDT transfers left out

    def to_json(self) -> dict[str, Any]:
        return {
            "transfers": [
                [t.tx_hash, iso(t.time), t.sender, t.recipient, str(t.amount)]
                for t in self.transfers
            ],
            "since": iso(self.since),
            "complete": self.complete,
            "first_activity": iso(self.first_activity) if self.first_activity else None,
            "zero_value": self.zero_value,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "History":
        return cls(
            tuple(
                Transfer(tx, from_iso(time), sender, recipient, Decimal(amount))
                for tx, time, sender, recipient, amount in data["transfers"]
            ),
            from_iso(data["since"]),
            data["complete"],
            from_iso(data["first_activity"]) if data["first_activity"] else None,
            data["zero_value"],
        )


class HistorySource(Protocol):
    chain: Chain

    async def fetch(self, address: str, since: datetime, limit: int) -> History: ...


class TronHistory:
    """USDT on TRON from TronGrid (docs/verification.md, V10)."""

    chain = Chain.tron

    def __init__(self, grid: TronGrid, contract: str) -> None:
        self._grid = grid
        self._contract = contract

    async def fetch(self, address: str, since: datetime, limit: int) -> History:
        (rows, truncated), created, first = await asyncio.gather(
            self._grid.token_transfers(address, self._contract, since, limit),
            self._grid.created(address),
            self._grid.first_token_transfer(address, self._contract),
        )
        transfers = []
        zero_value = 0
        for row in rows:
            if row.get("type") != "Transfer":
                continue
            decimals = int((row.get("token_info") or {}).get("decimals", 6))
            amount = Decimal(row["value"]).scaleb(-decimals)
            if amount == 0:
                zero_value += 1
                continue
            time = from_timestamp(row["block_timestamp"] / 1000)
            transfers.append(Transfer(row["transaction_id"], time, row["from"], row["to"], amount))
        starts = [moment for moment in (created, first) if moment is not None]
        return History(
            tuple(transfers), since, not truncated, min(starts) if starts else None, zero_value
        )
