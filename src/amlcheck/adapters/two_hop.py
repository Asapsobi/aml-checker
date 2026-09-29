"""Exposure (2-hop), PRD Phase 4: whom the screened address's largest counterparties received USDT
from (R-EXP-03).

The `counterparties` largest counterparties, by USDT exchanged with the screened address, have their
own histories read over the lookback. Every sanctioned or frozen wallet that sent one of them at
least `min_flagged_usdt` raises R-EXP-03 (Q16). As in the 1-hop walk, flags come from local data
only, so the walk spends no API quota beyond the histories.

- A counterparty flagged itself is already R-EXP-01, so it is not read.
- A counterparty with more than `max_transfers` transfers is a hub, such as an exchange. 2 hops
  through it reach almost everyone, so it is listed, not read.
- A counterparty that could not be read, in the time budget or at all, leaves the result stale, and
  so INCOMPLETE: a clean result never rests on part of the walk (PRD §0 rule 4).

The network found is kept in `evidence_meta["graph"]`, for the graph view.
"""

import asyncio
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from amlcheck.adapters.exposure import HISTORY_ERRORS, dealings, plural, usdt
from amlcheck.config import Exposure, TwoHop
from amlcheck.core import rules
from amlcheck.core.clock import utcnow
from amlcheck.core.models import Address, Finding, SourceHealth, SourceResult, SourceStatus
from amlcheck.exposure.reader import HistoryReader
from amlcheck.exposure.walker import (
    FROZEN,
    SANCTIONED,
    Counterparty,
    Flag,
    counterparties,
    flags,
    transfer_evidence,
)

SOURCE = "exposure_2hop"
LABEL = "Exposure (2-hop)"
RISK = (SANCTIONED, FROZEN)
MAX_FINDINGS = 10
READ, HUB, FLAGGED, NOT_REACHED, FAILED = "read", "hub", "flagged", "not_reached", "failed"


@dataclass
class Exposed:
    """A sanctioned or frozen wallet that sent a counterparty USDT."""

    sender: Counterparty  # as seen from the counterparty: `received` is what it got
    flags: list[Flag]


@dataclass
class Reach:
    """What became of one counterparty in the walk."""

    address: str
    state: str  # READ, HUB, FLAGGED, NOT_REACHED or FAILED
    detail: str | None = None
    exposed: list[Exposed] = field(default_factory=list)


def _by_size(parties: dict[str, Counterparty]) -> list[str]:
    return sorted(parties, key=lambda a: parties[a].received + parties[a].sent, reverse=True)


class TwoHopAdapter:
    source = SOURCE
    label = LABEL
    required = True

    def __init__(
        self,
        conn: sqlite3.Connection,
        reader: HistoryReader | None,
        exposure: Exposure,
        settings: TwoHop,
        *,
        unavailable: str = "no transfer history source is set up for this chain",
        now: Callable[[], datetime] = utcnow,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._conn = conn
        self._reader = reader
        self._exposure = exposure
        self._settings = settings
        self._unavailable = unavailable
        self._now = now
        self._clock = clock
        self.timeout = settings.time_budget_seconds + 30  # the engine allows the walk this long

    def _result(self, status: SourceStatus, summary: str, **kwargs: Any) -> SourceResult:
        return SourceResult(SOURCE, LABEL, self.required, status, summary, **kwargs)

    async def check(self, address: Address) -> SourceResult:
        reader = self._reader
        if reader is None:
            return self._result(SourceStatus.error, self._unavailable)
        now = self._now()
        deadline = self._clock() + self._settings.time_budget_seconds
        me = address.normalized
        try:
            history = await reader.read(me, self._exposure.max_transfers)
        except HISTORY_ERRORS as e:
            return self._result(SourceStatus.error, f"the transfer history could not be read: {e}")
        parties = counterparties(me, history.transfers)
        direct = {
            a: [f for f in found if f.kind in RISK]
            for a, found in flags(self._conn, address.chain, list(parties)).items()
            if any(f.kind in RISK for f in found)
        }
        scope = _by_size(parties)[: self._settings.counterparties]
        limit = asyncio.Semaphore(self._settings.parallel_reads)

        async def visit(other: str) -> Reach:
            if other in direct:
                return Reach(other, FLAGGED)
            async with limit:
                left = deadline - self._clock()
                if left <= 0:
                    return Reach(other, NOT_REACHED, "the time budget ran out")
                try:
                    theirs = await asyncio.wait_for(
                        reader.read(other, self._settings.max_transfers), left
                    )
                except TimeoutError:
                    return Reach(other, NOT_REACHED, "the time budget ran out")
                except HISTORY_ERRORS as e:
                    return Reach(other, FAILED, str(e))
            if not theirs.complete:
                cap = self._settings.max_transfers
                return Reach(other, HUB, f"more than {cap:,} transfers: a hub, not read")
            senders = {  # never the screened address itself: that would be circular
                a: p
                for a, p in counterparties(other, theirs.transfers).items()
                if p.received and a != me
            }
            found = flags(self._conn, address.chain, list(senders))
            least = Decimal(str(self._settings.min_flagged_usdt))
            exposed = [
                Exposed(senders[a], risky)
                for a, marks in found.items()
                if (risky := [f for f in marks if f.kind in RISK]) and senders[a].received >= least
            ]
            return Reach(other, READ, exposed=exposed)

        reached = await asyncio.gather(*(visit(other) for other in scope))
        findings = self._findings(me, parties, reached, now)
        count = {state: sum(r.state == state for r in reached) for state in (READ, HUB, FLAGGED)}
        missing = [r for r in reached if r.state in (NOT_REACHED, FAILED)]
        exposed = sum(bool(r.exposed) for r in reached)
        summary = f"{plural(count[READ], 'counterparty')} read"
        if count[HUB]:
            summary += f" ({plural(count[HUB], 'hub')} left out)"
        summary += (
            f"; {exposed} received USDT from a flagged wallet"
            if exposed
            else "; none received USDT from a flagged wallet"
        )
        status = SourceStatus.ok
        if missing:
            status = SourceStatus.stale
            summary = (
                f"{len(missing)} of the {len(scope)} largest counterparties could not be read"
                f" ({missing[0].detail}), so this result is not complete"
            )
        return self._result(
            status,
            summary,
            as_of=now,
            as_of_text=f"{len(scope)} of {plural(len(parties), 'counterparty')}",
            findings=tuple(findings),
            evidence_meta={
                "counterparties": len(parties),
                "scope": len(scope),
                "min_flagged_usdt": str(self._settings.min_flagged_usdt),
                "hub_above_transfers": self._settings.max_transfers,
                "reached": {
                    r.address: r.state + (f": {r.detail}" if r.detail else "") for r in reached
                },
                "graph": self._graph(me, parties, direct, reached),
            },
        )

    def _findings(
        self, me: str, parties: dict[str, Counterparty], reached: list[Reach], now: datetime
    ) -> list[Finding]:
        pairs = [(r, e) for r in reached for e in r.exposed]
        pairs.sort(key=lambda pair: pair[1].sender.received, reverse=True)
        findings = []
        for reach, e in pairs[:MAX_FINDINGS]:
            middle = parties[reach.address]
            reasons = "; ".join(f.detail for f in e.flags)
            findings.append(
                rules.finding(
                    rules.EXP_03,
                    SOURCE,
                    f"{dealings(middle)}, which received {usdt(e.sender.received)} from"
                    f" {e.sender.address} ({reasons})",
                    {
                        "counterparty": reach.address,
                        "received_usdt": str(middle.received),
                        "sent_usdt": str(middle.sent),
                        "transfers": transfer_evidence(me, middle.transfers),
                        "flagged_sender": e.sender.address,
                        "flagged_sent_usdt": str(e.sender.received),
                        "flags": [
                            {"kind": f.kind, "detail": f.detail, **f.evidence} for f in e.flags
                        ],
                        "flagged_transfers": transfer_evidence(reach.address, e.sender.transfers),
                    },
                    now,
                )
            )
        return findings

    @staticmethod
    def _graph(
        me: str,
        parties: dict[str, Counterparty],
        direct: dict[str, list[Flag]],
        reached: list[Reach],
    ) -> dict[str, Any]:
        """The network walked: the address, its counterparties in scope, and the flagged wallets
        that sent them USDT."""
        nodes: list[dict[str, Any]] = [{"id": me, "ring": 0, "state": "target", "flags": []}]
        edges: list[dict[str, Any]] = []
        for r in reached:
            party = parties[r.address]
            nodes.append(
                {
                    "id": r.address,
                    "ring": 1,
                    "state": r.state,
                    "flags": [f.detail for f in direct.get(r.address, [])],
                }
            )
            edges.append(
                {
                    "from": r.address,
                    "to": me,
                    "received_usdt": str(party.received),
                    "sent_usdt": str(party.sent),
                }
            )
            for e in r.exposed:
                if all(n["id"] != e.sender.address for n in nodes):
                    flagged = [f.detail for f in e.flags]
                    nodes.append(
                        {"id": e.sender.address, "ring": 2, "state": FLAGGED, "flags": flagged}
                    )
                edges.append(
                    {
                        "from": e.sender.address,
                        "to": r.address,
                        "received_usdt": str(e.sender.received),
                        "sent_usdt": "0",
                    }
                )
        return {"nodes": nodes, "edges": edges}

    async def health(self) -> SourceHealth:
        if self._reader is None:
            return SourceHealth(SOURCE, LABEL, SourceStatus.error, self._unavailable)
        s = self._settings
        return SourceHealth(
            SOURCE,
            LABEL,
            SourceStatus.ok,
            f"the {s.counterparties} largest counterparties, within {s.time_budget_seconds:g} s;"
            f" on `investigate` and checks of {s.auto_amount_usdt:,.0f} USDT or more",
        )
