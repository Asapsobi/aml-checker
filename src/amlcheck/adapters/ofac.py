"""OFAC SDN list: the daily download, a local index of its digital currency addresses, lookups.

An address matches on the address alone, whatever currency label OFAC gave it: the labels are
unreliable (docs/verification.md, V1). The list's age counts from its last successful download,
not from OFAC's publish date, because OFAC does not publish every day (Q3).
"""

import hashlib
import logging
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import IO

import httpx
from defusedxml.ElementTree import iterparse

from amlcheck.core import rules
from amlcheck.core.address import EVM_FORMAT, TRON_FORMAT, AddressError, parse
from amlcheck.core.clock import from_iso, iso, utcnow
from amlcheck.core.models import Address, Chain, SourceHealth, SourceResult, SourceStatus

SOURCE = "ofac_sdn"
LABEL = "OFAC SDN"
ADDRESS_TYPE = "Digital Currency Address - "
MAX_DROP = 0.2  # PRD §14: a new list may not lose more than 20% of its entries or addresses

Progress = Callable[[int, int | None], None]
log = logging.getLogger(__name__)


class SyncError(Exception):
    """The download or its contents cannot be trusted. The previous list stays in use."""


@dataclass(frozen=True)
class Listed:
    address_norm: str
    chain_hint: str | None
    currency_label: str
    list_entry_id: str
    program: str
    entity_name: str


@dataclass(frozen=True)
class SdnList:
    published: date | None
    record_count: int
    addresses: tuple[Listed, ...]


@dataclass(frozen=True)
class Snapshot:
    id: int
    fetched_at: datetime
    published: str | None
    sha256: str
    record_count: int
    address_count: int


def parse_list(stream: IO[bytes]) -> SdnList:
    namespace = ""
    published: date | None = None
    record_count = 0
    found: list[Listed] = []
    # defusedxml refuses entity tricks and external references in the downloaded file.
    for event, element in iterparse(stream, events=("start", "end")):
        if event == "start":
            if not namespace and element.tag.startswith("{"):
                namespace = element.tag[: element.tag.index("}") + 1]
            continue
        if element.tag == f"{namespace}Publish_Date" and element.text:
            month, day, year = (int(part) for part in element.text.strip().split("/"))
            published = date(year, month, day)
        elif element.tag == f"{namespace}Record_Count" and element.text:
            record_count = int(element.text)
        elif element.tag == f"{namespace}sdnEntry":
            found.extend(_addresses(element, namespace))
            element.clear()
    return SdnList(published, record_count, tuple(found))


def _addresses(entry: ET.Element, ns: str) -> Iterator[Listed]:
    entry_id = (entry.findtext(f"{ns}uid") or "").strip()
    name = " ".join(
        filter(None, (entry.findtext(f"{ns}firstName"), entry.findtext(f"{ns}lastName")))
    )
    programs = ", ".join(p.text.strip() for p in entry.iter(f"{ns}program") if p.text)
    for item in entry.iter(f"{ns}id"):
        kind = item.findtext(f"{ns}idType") or ""
        value = (item.findtext(f"{ns}idNumber") or "").strip()
        if kind.startswith(ADDRESS_TYPE) and value:
            normalized, hint = normalize_listed(value, entry_id)
            yield Listed(
                normalized, hint, kind.removeprefix(ADDRESS_TYPE), entry_id, programs, name
            )


def normalize_listed(value: str, entry_id: str = "") -> tuple[str, str | None]:
    """How a listed address is stored: 0x addresses in lowercase, all others as written.

    An address that fails its checksum is logged and kept: a typo on the list must not hide it.
    """
    if not (EVM_FORMAT.fullmatch(value) or TRON_FORMAT.fullmatch(value)):
        return value, None
    try:
        address = parse(value)
    except AddressError as e:
        log.warning("OFAC entry %s lists an address that fails its checksum: %s", entry_id, e)
        return (value.lower(), "evm") if value.startswith("0x") else (value, "tron")
    return address.normalized, "evm" if address.chain is Chain.bsc else "tron"


async def download(
    http: httpx.AsyncClient, url: str, progress: Progress | None = None
) -> tuple[SdnList, str]:
    """Stream the list to a temporary file, hashing it on the way, then parse it."""
    digest = hashlib.sha256()
    with tempfile.TemporaryFile() as file:
        async with http.stream("GET", url) as response:
            if response.status_code != 200:
                raise SyncError(f"OFAC answered HTTP {response.status_code}")
            length = response.headers.get("Content-Length")
            total = int(length) if length else None
            done = 0
            async for chunk in response.aiter_bytes():
                file.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        file.seek(0)
        try:
            parsed = parse_list(file)
        except (ET.ParseError, ValueError) as e:
            raise SyncError(f"the downloaded list could not be read: {e}") from e
    return parsed, digest.hexdigest()


def latest(conn: sqlite3.Connection) -> Snapshot | None:
    row = conn.execute(
        "SELECT id, fetched_at, published_at, sha256, entry_count, address_count"
        " FROM list_snapshots WHERE source = ? ORDER BY id DESC LIMIT 1",
        (SOURCE,),
    ).fetchone()
    if row is None:
        return None
    return Snapshot(row[0], from_iso(row[1]), row[2], row[3], row[4], row[5] or 0)


def implausible(previous: Snapshot | None, new: SdnList) -> str | None:
    if not new.addresses:
        return "the list holds no digital currency addresses: its format may have changed"
    if previous is None:
        return None
    for what, now_count, before in (
        ("entries", new.record_count, previous.record_count),
        ("addresses", len(new.addresses), previous.address_count),
    ):
        if before and now_count < before * (1 - MAX_DROP):
            return (
                f"the new list has {now_count} {what} against {before} in the last one, a drop of"
                " more than 20%, so it was not used"
            )
    return None


def store(conn: sqlite3.Connection, new: SdnList, sha256: str, fetched_at: datetime) -> Snapshot:
    problem = implausible(latest(conn), new)
    if problem:
        raise SyncError(problem)
    published = new.published.isoformat() if new.published else None
    with conn:
        cursor = conn.execute(
            "INSERT INTO list_snapshots (source, fetched_at, published_at, sha256, entry_count,"
            " address_count) VALUES (?, ?, ?, ?, ?, ?)",
            (SOURCE, iso(fetched_at), published, sha256, new.record_count, len(new.addresses)),
        )
        snapshot_id = cursor.lastrowid
        if snapshot_id is None:
            raise SyncError("the database did not return an ID for the new snapshot")
        conn.executemany(
            "INSERT INTO sanctioned_addresses (address_norm, chain_hint, currency_label,"
            " list_entry_id, program, entity_name, snapshot_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    a.address_norm,
                    a.chain_hint,
                    a.currency_label,
                    a.list_entry_id,
                    a.program,
                    a.entity_name,
                    snapshot_id,
                )
                for a in new.addresses
            ],
        )
    return Snapshot(
        snapshot_id, fetched_at, published, sha256, new.record_count, len(new.addresses)
    )


async def sync(
    conn: sqlite3.Connection,
    http: httpx.AsyncClient,
    url: str,
    progress: Progress | None = None,
    now: Callable[[], datetime] = utcnow,
) -> Snapshot:
    new, sha256 = await download(http, url, progress)
    return store(conn, new, sha256, now())


class OfacAdapter:
    source = SOURCE
    label = LABEL
    required = True

    def __init__(
        self,
        conn: sqlite3.Connection,
        max_age: timedelta,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._conn = conn
        self._max_age = max_age
        self._now = now

    async def check(self, address: Address) -> SourceResult:
        snapshot = latest(self._conn)
        if snapshot is None:
            return SourceResult(
                SOURCE,
                LABEL,
                True,
                SourceStatus.error,
                "no sanctions list yet: run `amlcheck sync sanctions`",
            )
        rows = self._conn.execute(
            "SELECT list_entry_id, entity_name, program, group_concat(currency_label, ', ')"
            " FROM sanctioned_addresses WHERE snapshot_id = ? AND address_norm = ?"
            " GROUP BY list_entry_id, entity_name, program ORDER BY list_entry_id",
            (snapshot.id, address.normalized),
        ).fetchall()
        findings = tuple(
            rules.finding(
                rules.SAN_01,
                SOURCE,
                f"on the OFAC SDN list as {name} (entry {entry}, {program}), listed as {labels}",
                {
                    "list_entry_id": entry,
                    "entity_name": name,
                    "program": program,
                    "currency_label": labels,
                    "list_published": snapshot.published,
                    "list_sha256": snapshot.sha256,
                },
                snapshot.fetched_at,
            )
            for entry, name, program, labels in rows
        )
        age = self._now() - snapshot.fetched_at
        stale = age > self._max_age
        summary = "match" if findings else "no match"
        if stale:
            summary += (
                f"; the list was downloaded {age.total_seconds() / 3600:.0f} hours ago, more than"
                f" {self._max_age.total_seconds() / 3600:.0f}: run `amlcheck sync sanctions`"
            )
        return SourceResult(
            SOURCE,
            LABEL,
            True,
            SourceStatus.stale if stale else SourceStatus.ok,
            summary,
            as_of=snapshot.fetched_at,
            as_of_text=f"list of {snapshot.published}",
            findings=findings,
            evidence_meta={
                "snapshot_id": snapshot.id,
                "list_published": snapshot.published,
                "downloaded": iso(snapshot.fetched_at),
                "sha256": snapshot.sha256,
                "entries": snapshot.record_count,
                "addresses": snapshot.address_count,
            },
        )

    async def health(self) -> SourceHealth:
        snapshot = latest(self._conn)
        if snapshot is None:
            return SourceHealth(
                SOURCE, LABEL, SourceStatus.error, "no list yet: run `amlcheck sync sanctions`"
            )
        age = self._now() - snapshot.fetched_at
        status = SourceStatus.stale if age > self._max_age else SourceStatus.ok
        return SourceHealth(
            SOURCE,
            LABEL,
            status,
            f"list of {snapshot.published}, downloaded {age.total_seconds() / 3600:.1f} hours ago,"
            f" {snapshot.address_count:,} addresses on {snapshot.record_count:,} entries",
        )
