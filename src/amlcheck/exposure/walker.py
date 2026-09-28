"""The 1-hop walk (PRD §7): the screened address's counterparties, and which of them are flagged.

Flags come from local data only, so a walk costs no API quota (PRD §11): the OFAC snapshot, Tether's
TRON blacklist index, and the user's labels. A counterparty is frozen when its latest blacklist
event is an AddedBlackList.
"""

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from amlcheck.adapters import ofac
from amlcheck.core.clock import iso
from amlcheck.core.models import Chain
from amlcheck.exposure.history import Transfer

SANCTIONED = "sanctioned"
FROZEN = "frozen"
LABEL = "label"


@dataclass
class Counterparty:
    address: str
    received: Decimal = Decimal(0)  # what the screened address received from it
    sent: Decimal = Decimal(0)  # what the screened address sent to it
    transfers: list[Transfer] = field(default_factory=list)


@dataclass(frozen=True)
class Flag:
    kind: str
    detail: str
    evidence: dict[str, Any]


def counterparties(address: str, transfers: tuple[Transfer, ...]) -> dict[str, Counterparty]:
    found: dict[str, Counterparty] = {}
    for t in transfers:
        if t.sender == t.recipient:
            continue
        if t.recipient == address:
            other = found.setdefault(t.sender, Counterparty(t.sender))
            other.received += t.amount
        elif t.sender == address:
            other = found.setdefault(t.recipient, Counterparty(t.recipient))
            other.sent += t.amount
        else:
            continue
        other.transfers.append(t)
    return found


def flags(conn: sqlite3.Connection, chain: Chain, addresses: list[str]) -> dict[str, list[Flag]]:
    found: dict[str, list[Flag]] = defaultdict(list)
    wanted = json.dumps(sorted(addresses))
    snapshot = ofac.latest(conn)
    if snapshot is not None:
        for address, entry, name, program in conn.execute(
            "SELECT address_norm, list_entry_id, entity_name, program FROM sanctioned_addresses"
            " WHERE snapshot_id = ? AND address_norm IN (SELECT value FROM json_each(?))"
            " GROUP BY address_norm, list_entry_id ORDER BY address_norm, list_entry_id",
            (snapshot.id, wanted),
        ):
            found[address].append(
                Flag(
                    SANCTIONED,
                    f"on the OFAC SDN list as {name} (entry {entry})",
                    {"list_entry_id": entry, "entity_name": name, "program": program},
                )
            )
    if chain is Chain.tron:
        latest: dict[str, tuple[str, str, str]] = {}
        for address, kind, tx, when in conn.execute(
            "SELECT address_norm, event_type, tx_hash, block_time FROM issuer_events"
            " WHERE chain = 'tron' AND event_type IN ('AddedBlackList', 'RemovedBlackList')"
            " AND address_norm IN (SELECT value FROM json_each(?)) ORDER BY block, event_type",
            (wanted,),
        ):
            latest[address] = (kind, tx, when)
        for address, (kind, tx, when) in sorted(latest.items()):
            if kind == "AddedBlackList":
                found[address].append(
                    Flag(
                        FROZEN,
                        f"blacklisted by Tether since {when[:10]} (tx {tx})",
                        {"issuer": "Tether", "event_tx": tx, "since": when},
                    )
                )
    for address, tag, note, source in conn.execute(
        "SELECT address_norm, tag, note, source FROM labels WHERE chain = ?"
        " AND address_norm IN (SELECT value FROM json_each(?)) ORDER BY address_norm, tag",
        (chain.value, wanted),
    ):
        detail = f"labelled {tag} in labels.csv" + (f" ({note})" if note else "")
        found[address].append(Flag(LABEL, detail, {"tag": tag, "note": note, "source": source}))
    return dict(found)


def transfer_evidence(
    address: str, transfers: list[Transfer], limit: int = 3
) -> list[dict[str, Any]]:
    """The largest transfers with a counterparty, as proof."""
    largest = sorted(transfers, key=lambda t: t.amount, reverse=True)[:limit]
    return [
        {
            "tx_hash": t.tx_hash,
            "time": iso(t.time),
            "direction": "in" if t.recipient == address else "out",
            "amount_usdt": str(t.amount),
        }
        for t in largest
    ]
