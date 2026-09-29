"""Pattern measures behind R-HEU-02 to R-HEU-04 (PRD §5.2): pure functions over transfers."""

from collections import Counter, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from amlcheck.exposure.history import Transfer


@dataclass
class _Lot:
    arrived: datetime
    left: Decimal


def pass_through(
    address: str, transfers: Iterable[Transfer], window: timedelta
) -> tuple[Decimal, Decimal]:
    """(USDT received, the part of it sent on within `window` of arriving), first in, first out.

    Money that was already there before the history starts is not counted as passed through.
    """
    lots: deque[_Lot] = deque()
    received = Decimal(0)
    passed = Decimal(0)
    for t in sorted(transfers, key=lambda t: t.time):
        if t.recipient == address:
            received += t.amount
            lots.append(_Lot(t.time, t.amount))
            continue
        needed = t.amount
        while needed > 0 and lots:
            lot = lots[0]
            take = min(needed, lot.left)
            if t.time - lot.arrived <= window:
                passed += take
            needed -= take
            lot.left -= take
            if not lot.left:
                lots.popleft()
    return received, passed


@dataclass(frozen=True)
class Burst:
    count: int
    start: datetime | None = None
    end: datetime | None = None


def busiest_window(events: Iterable[tuple[datetime, str]], window: timedelta) -> Burst:
    """The most distinct counterparties seen within any stretch of `window`."""
    ordered = sorted(events)
    seen: Counter[str] = Counter()
    best = Burst(0)
    left = 0
    for time, who in ordered:
        seen[who] += 1
        while time - ordered[left][0] > window:
            gone = ordered[left][1]
            seen[gone] -= 1
            if not seen[gone]:
                del seen[gone]
            left += 1
        if len(seen) > best.count:
            best = Burst(len(seen), ordered[left][0], time)
    return best
