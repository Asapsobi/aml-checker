"""Exposure (1-hop): who the screened address dealt with, and how it moved money (PRD Phase 2).

R-EXP-01 and R-EXP-02 look at counterparties that are sanctioned or frozen; R-HEU-01 to R-HEU-05
read the pattern of the address's own USDT transfers over the lookback. The history is read in full
or the source is stale: more than `max_transfers` in the window make the result INCOMPLETE, so a
clean result never rests on part of it (decided 2026-09-28).
"""

import sqlite3
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from amlcheck.adapters.tron import TronGridError
from amlcheck.config import Exposure, Heuristics
from amlcheck.core import rules
from amlcheck.core.clock import iso, utcnow
from amlcheck.core.models import (
    Address,
    Chain,
    Finding,
    SourceHealth,
    SourceResult,
    SourceStatus,
)
from amlcheck.exposure.heuristics import busiest_window, pass_through
from amlcheck.exposure.history import History, HistorySource
from amlcheck.exposure.walker import (
    FROZEN,
    LABEL,
    SANCTIONED,
    Counterparty,
    Flag,
    counterparties,
    flags,
    transfer_evidence,
)
from amlcheck.storage.cache import ResponseCache

SOURCE = "exposure"
MAX_FINDINGS = 10  # per rule, largest counterparties first
FROZEN_ELSEWHERE = "frozen_elsewhere"  # a remote lookup: Eagle Virtual says FROZEN or SEIZED
RISK_FLAGS = (SANCTIONED, FROZEN, FROZEN_ELSEWHERE)


class LookupFailed(Exception):
    pass


# Eagle Virtual's verdict for a counterparty (PRD §11 remote lookups); raises LookupFailed.
RemoteLookup = Callable[[str], Awaitable[str | None]]


def usdt(amount: Decimal) -> str:
    return f"{amount:,.2f} USDT"


def dealings(party: Counterparty) -> str:
    if party.received and party.sent:
        return (
            f"dealt with {party.address} (received {usdt(party.received)}, sent {usdt(party.sent)})"
        )
    if party.received:
        return f"received {usdt(party.received)} from {party.address}"
    return f"sent {usdt(party.sent)} to {party.address}"


def _by_size(parties: dict[str, Counterparty], addresses: list[str]) -> list[str]:
    return sorted(addresses, key=lambda a: parties[a].received + parties[a].sent, reverse=True)


class ExposureAdapter:
    source = SOURCE
    label = "Exposure (1-hop)"
    required = True

    def __init__(
        self,
        conn: sqlite3.Connection,
        chain: Chain,
        history: HistorySource | None,
        exposure: Exposure,
        heuristics: Heuristics,
        cache: ResponseCache,
        *,
        unavailable: str = "no transfer history source is set up for this chain",
        remote: RemoteLookup | None = None,
        max_remote: int = 0,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._conn = conn
        self._chain = chain
        self._history = history
        self._exposure = exposure
        self._heuristics = heuristics
        self._cache = cache
        self._unavailable = unavailable
        self._remote = remote
        self._max_remote = max_remote
        self._now = now

    def _result(self, status: SourceStatus, summary: str, **kwargs: Any) -> SourceResult:
        return SourceResult(SOURCE, self.label, self.required, status, summary, **kwargs)

    async def _read(self, source: HistorySource, address: Address, since: datetime) -> History:
        key = (
            f"{SOURCE}:{address.chain.value}:{address.normalized}:{since.date().isoformat()}"
            f":{self._exposure.max_transfers}"
        )
        cached = self._cache.get(key)
        if cached is not None:
            return History.from_json(cached)
        history = await source.fetch(address.normalized, since, self._exposure.max_transfers)
        self._cache.put(key, SOURCE, history.to_json())
        return history

    async def check(self, address: Address) -> SourceResult:
        if self._history is None:
            return self._result(SourceStatus.error, self._unavailable)
        now = self._now()
        days = self._exposure.lookback_days
        try:
            history = await self._read(self._history, address, now - timedelta(days=days))
        except (TronGridError, httpx.HTTPError) as e:
            return self._result(SourceStatus.error, f"the transfer history could not be read: {e}")

        parties = counterparties(address.normalized, history.transfers)
        flagged = flags(self._conn, address.chain, list(parties))
        lookups = await self._look_up(parties, flagged)
        risky = {
            a: [f for f in found if f.kind in RISK_FLAGS]
            for a, found in flagged.items()
            if any(f.kind in RISK_FLAGS for f in found)
        }
        findings = self._exposure_findings(address, parties, risky, now)
        findings += self._behaviour_findings(address, history, parties, flagged, now)

        count = len(history.transfers)
        summary = f"{count:,} transfers with {len(parties):,} counterparties"
        summary += f"; {len(risky)} flagged" if risky else "; none flagged"
        status = SourceStatus.ok
        if not history.complete:
            status = SourceStatus.stale
            cap = self._exposure.max_transfers
            summary = (
                f"more than {cap:,} transfers in the last {days} days: only the newest {cap:,}"
                " were read, so this result is not complete"
            )
        return self._result(
            status,
            summary,
            as_of=now,
            as_of_text=f"last {days} d, {count:,} transfers",
            findings=tuple(findings),
            evidence_meta={
                "lookback_days": days,
                "since": iso(history.since),
                "transfers": count,
                "complete": history.complete,
                "zero_value_transfers_ignored": history.zero_value,
                "counterparties": len(parties),
                "flagged_counterparties": sorted(risky),
                "first_activity": iso(history.first_activity) if history.first_activity else None,
                **lookups,
            },
        )

    async def _look_up(
        self, parties: dict[str, Counterparty], flagged: dict[str, list[Flag]]
    ) -> dict[str, Any]:
        """Ask Eagle Virtual about the largest counterparties not already flagged, up to the
        configured number (PRD §11: 0 by default, to keep the Free plan's quota)."""
        if self._remote is None or self._max_remote <= 0:
            return {}
        unflagged = [
            a for a in parties if not any(f.kind in RISK_FLAGS for f in flagged.get(a, []))
        ]
        asked, failed = 0, []
        for address in _by_size(parties, unflagged)[: self._max_remote]:
            try:
                verdict = await self._remote(address)
            except LookupFailed:
                failed.append(address)
                continue
            asked += 1
            if verdict in ("FROZEN", "SEIZED"):
                flag = Flag(
                    FROZEN_ELSEWHERE,
                    f"{verdict} according to Eagle Virtual",
                    {"verdict": verdict, "source": "eagle_virtual"},
                )
                flagged.setdefault(address, []).append(flag)
        return {"remote_lookups": asked, "remote_lookups_failed": failed}

    def _exposure_findings(
        self,
        address: Address,
        parties: dict[str, Counterparty],
        risky: dict[str, list[Flag]],
        now: datetime,
    ) -> list[Finding]:
        findings = []
        ordered = _by_size(parties, list(risky))
        for other in ordered[:MAX_FINDINGS]:
            party = parties[other]
            reasons = "; ".join(f.detail for f in risky[other])
            findings.append(
                rules.finding(
                    rules.EXP_01,
                    SOURCE,
                    f"{dealings(party)} ({reasons})",
                    {
                        "counterparty": other,
                        "received_usdt": str(party.received),
                        "sent_usdt": str(party.sent),
                        "flags": [
                            {"kind": f.kind, "detail": f.detail, **f.evidence} for f in risky[other]
                        ],
                        "transfers": transfer_evidence(address.normalized, party.transfers),
                    },
                    now,
                )
            )
        received = sum((p.received for p in parties.values()), Decimal(0))
        from_flagged = sum((parties[a].received for a in risky), Decimal(0))
        days = self._exposure.lookback_days
        if received and from_flagged / received >= Decimal(
            str(self._exposure.flagged_inflow_share)
        ):
            share = from_flagged / received
            findings.append(
                rules.finding(
                    rules.EXP_02,
                    SOURCE,
                    f"{share:.1%} of the USDT received in the last {days} days came from flagged"
                    f" addresses ({usdt(from_flagged)} of {usdt(received)})",
                    {
                        "share": f"{share:.4f}",
                        "received_usdt": str(received),
                        "from_flagged_usdt": str(from_flagged),
                        "lookback_days": days,
                        "flagged_senders": [a for a in ordered if parties[a].received],
                    },
                    now,
                )
            )
        return findings

    def _behaviour_findings(
        self,
        address: Address,
        history: History,
        parties: dict[str, Counterparty],
        flagged: dict[str, list[Flag]],
        now: datetime,
    ) -> list[Finding]:
        h = self._heuristics
        me = address.normalized
        findings = []

        first = history.first_activity
        if first is None or now - first < timedelta(days=h.new_address_days):
            when = (
                "no activity on chain yet"
                if first is None
                else (f"first active on {first.date()}, {(now - first).days} days ago")
            )
            findings.append(
                rules.finding(
                    rules.HEU_01,
                    SOURCE,
                    f"new address: {when}",
                    {
                        "first_activity": iso(first) if first else None,
                        "priority": rules.LOW_PRIORITY,
                    },
                    now,
                )
            )

        allowlisted = {
            a
            for a, found in flagged.items()
            if any(f.kind == LABEL and f.evidence["tag"] == h.allowlist_tag for f in found)
        }
        counted = [
            t
            for t in history.transfers
            if t.sender != t.recipient
            and (t.sender if t.recipient == me else t.recipient) not in allowlisted
        ]
        window = timedelta(hours=h.pass_through_hours)
        received, passed = pass_through(me, counted, window)
        if received and passed / received >= Decimal(str(h.pass_through_share)):
            share = passed / received
            findings.append(
                rules.finding(
                    rules.HEU_02,
                    SOURCE,
                    f"pass-through: {share:.0%} of the USDT received ({usdt(passed)} of"
                    f" {usdt(received)}) left again within {h.pass_through_hours} hours",
                    {
                        "share": f"{share:.4f}",
                        "received_usdt": str(received),
                        "passed_on_usdt": str(passed),
                        "within_hours": h.pass_through_hours,
                    },
                    now,
                )
            )

        small = Decimal(str(h.fan_in_small_usdt))
        fan_in = busiest_window(
            ((t.time, t.sender) for t in counted if t.recipient == me and t.amount < small),
            timedelta(hours=h.fan_in_window_hours),
        )
        if fan_in.count > h.fan_in_senders and fan_in.start and fan_in.end:
            findings.append(
                rules.finding(
                    rules.HEU_03,
                    SOURCE,
                    f"fan-in: {fan_in.count} different addresses each sent under {usdt(small)}"
                    f" within {h.fan_in_window_hours} hours"
                    f" ({iso(fan_in.start)} to {iso(fan_in.end)})",
                    {
                        "senders": fan_in.count,
                        "start": iso(fan_in.start),
                        "end": iso(fan_in.end),
                        "under_usdt": str(small),
                    },
                    now,
                )
            )
        fan_out = busiest_window(
            ((t.time, t.recipient) for t in counted if t.sender == me),
            timedelta(hours=h.fan_out_window_hours),
        )
        if fan_out.count > h.fan_out_recipients and fan_out.start and fan_out.end:
            findings.append(
                rules.finding(
                    rules.HEU_04,
                    SOURCE,
                    f"fan-out: paid {fan_out.count} different addresses within"
                    f" {h.fan_out_window_hours} hours ({iso(fan_out.start)} to {iso(fan_out.end)})",
                    {
                        "recipients": fan_out.count,
                        "start": iso(fan_out.start),
                        "end": iso(fan_out.end),
                    },
                    now,
                )
            )

        tagged = {
            a: [f for f in found if f.kind == LABEL and f.evidence["tag"] in h.risky_tags]
            for a, found in flagged.items()
        }
        for other in _by_size(parties, [a for a, found in tagged.items() if found])[:MAX_FINDINGS]:
            party = parties[other]
            findings.append(
                rules.finding(
                    rules.HEU_05,
                    SOURCE,
                    f"{dealings(party)} ({'; '.join(f.detail for f in tagged[other])})",
                    {
                        "counterparty": other,
                        "labels": [f.evidence for f in tagged[other]],
                        "transfers": transfer_evidence(me, party.transfers),
                    },
                    now,
                )
            )
        return findings

    async def health(self) -> SourceHealth:
        label = f"Exposure ({self._chain.upper()})"
        if self._history is None:
            return SourceHealth(SOURCE, label, SourceStatus.error, self._unavailable)
        labels = self._conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0]
        return SourceHealth(
            SOURCE,
            label,
            SourceStatus.ok,
            f"transfers over {self._exposure.lookback_days} days, up to"
            f" {self._exposure.max_transfers:,}; {labels:,} labels loaded",
        )
