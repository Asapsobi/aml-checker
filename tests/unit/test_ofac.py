import hashlib
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx
from conftest import FIXTURES

from amlcheck.adapters import ofac
from amlcheck.config import Ofac
from amlcheck.core.address import parse
from amlcheck.core.models import Severity, SourceStatus
from amlcheck.storage import db

SAMPLE = FIXTURES / "ofac" / "sdn_sample.xml"
SDN_URL = Ofac().sdn_url
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
LAZARUS = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"  # listed as ETH
CHEIL_TRON = "TA3941uFAvmVibSkQ6fMJXxmaSNovX86mz"  # listed as USDT
WANG_TRON = "TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq"  # a TRON address OFAC filed under XBT


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


@pytest.fixture
def listed(conn: sqlite3.Connection) -> ofac.Snapshot:
    with SAMPLE.open("rb") as file:
        return ofac.store(conn, ofac.parse_list(file), "sha", NOW)


def adapter(conn: sqlite3.Connection, now: datetime = NOW) -> ofac.OfacAdapter:
    return ofac.OfacAdapter(conn, timedelta(hours=48), now=lambda: now)


def test_the_sample_parses_like_the_published_list() -> None:
    with SAMPLE.open("rb") as file:
        parsed = ofac.parse_list(file)
    assert parsed.published == date(2026, 9, 23)
    assert parsed.record_count == 19391
    assert len(parsed.addresses) == 8 + 53 + 4
    found = {a.address_norm: a for a in parsed.addresses}
    lazarus = found[LAZARUS.lower()]
    assert (lazarus.list_entry_id, lazarus.entity_name) == ("27307", "LAZARUS GROUP")
    assert (lazarus.currency_label, lazarus.chain_hint) == ("ETH", "evm")
    assert lazarus.program
    wang = found[WANG_TRON]
    assert (wang.entity_name, wang.currency_label, wang.chain_hint) == (
        "Mingming WANG",
        "XBT",
        "tron",
    )
    assert all(
        a.address_norm == a.address_norm.lower() for a in found.values() if a.chain_hint == "evm"
    )


def test_a_listed_address_with_a_bad_checksum_is_kept(caplog: pytest.LogCaptureFixture) -> None:
    typo = CHEIL_TRON[:-1] + "n"
    with caplog.at_level(logging.WARNING):
        assert ofac.normalize_listed(typo, "22985") == (typo, "tron")
    assert "22985" in caplog.text
    assert ofac.normalize_listed("bnb136ns6lfw4zs5hg4n85vdthaad7hq5m4gtkgf23") == (
        "bnb136ns6lfw4zs5hg4n85vdthaad7hq5m4gtkgf23",
        None,
    )


async def test_sync_follows_the_redirect_and_keeps_the_hash(
    conn: sqlite3.Connection, network: respx.MockRouter
) -> None:
    content = SAMPLE.read_bytes()
    network.get(SDN_URL).respond(302, headers={"Location": "https://s3.test/SDN.XML"})
    network.get("https://s3.test/SDN.XML").respond(200, content=content)
    seen: list[int] = []
    async with httpx.AsyncClient(follow_redirects=True) as http:
        snapshot = await ofac.sync(
            conn, http, SDN_URL, lambda done, total: seen.append(done), now=lambda: NOW
        )
    assert snapshot.sha256 == hashlib.sha256(content).hexdigest()
    assert (snapshot.published, snapshot.address_count, snapshot.fetched_at) == (
        "2026-09-23",
        65,
        NOW,
    )
    assert seen[-1] == len(content)
    assert ofac.latest(conn) == snapshot


async def test_a_failed_download_keeps_the_previous_list(
    conn: sqlite3.Connection, listed: ofac.Snapshot, network: respx.MockRouter
) -> None:
    network.get(SDN_URL).respond(500)
    async with httpx.AsyncClient() as http:
        with pytest.raises(ofac.SyncError, match="HTTP 500"):
            await ofac.sync(conn, http, SDN_URL)
    assert ofac.latest(conn) == listed


def test_a_list_that_lost_more_than_a_fifth_is_refused(
    conn: sqlite3.Connection, listed: ofac.Snapshot
) -> None:
    with SAMPLE.open("rb") as file:
        full = ofac.parse_list(file)
    shrunk = ofac.SdnList(full.published, full.record_count, full.addresses[:10])
    with pytest.raises(ofac.SyncError, match="more than 20%"):
        ofac.store(conn, shrunk, "sha2", NOW)
    assert ofac.latest(conn) == listed
    assert ofac.implausible(None, ofac.SdnList(None, 0, ())) is not None


async def test_listed_address_blocks_with_its_list_entry(
    conn: sqlite3.Connection, listed: ofac.Snapshot
) -> None:
    """AT-03. Lazarus is listed as ETH; the same key is the same wallet on BSC."""
    result = await adapter(conn).check(parse(LAZARUS))
    assert result.status is SourceStatus.ok
    [finding] = result.findings
    assert (finding.rule_id, finding.severity) == ("R-SAN-01", Severity.BLOCK)
    assert finding.evidence["list_entry_id"] == "27307"
    assert "LAZARUS GROUP" in finding.summary


async def test_the_currency_label_does_not_matter(
    conn: sqlite3.Connection, listed: ofac.Snapshot
) -> None:
    result = await adapter(conn).check(parse(WANG_TRON))
    assert [f.evidence["entity_name"] for f in result.findings] == ["Mingming WANG"]


async def test_unlisted_address_is_no_match(
    conn: sqlite3.Connection, listed: ofac.Snapshot
) -> None:
    result = await adapter(conn).check(parse("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"))
    assert (result.status, result.summary, result.findings) == (SourceStatus.ok, "no match", ())
    assert result.as_of_text == "list of 2026-09-23"


async def test_an_old_download_is_stale_but_still_matches(
    conn: sqlite3.Connection, listed: ofac.Snapshot
) -> None:
    """AT-08: the list's age counts from its download (Q3)."""
    result = await adapter(conn, now=NOW + timedelta(hours=49)).check(parse(CHEIL_TRON))
    assert result.status is SourceStatus.stale
    assert "49 hours ago" in result.summary
    assert len(result.findings) == 1


async def test_no_list_yet_is_an_error(conn: sqlite3.Connection) -> None:
    result = await adapter(conn).check(parse(CHEIL_TRON))
    assert result.status is SourceStatus.error
    assert "amlcheck sync sanctions" in result.summary
    assert (await adapter(conn).health()).status is SourceStatus.error


async def test_health_reports_the_age_and_size(
    conn: sqlite3.Connection, listed: ofac.Snapshot
) -> None:
    health = await adapter(conn, now=NOW + timedelta(hours=3)).health()
    assert health.status is SourceStatus.ok
    assert "3.0 hours ago" in health.detail
    assert "65 addresses" in health.detail
