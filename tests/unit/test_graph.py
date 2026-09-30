import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import defusedxml.ElementTree as ElementTree
import pytest
from conftest import FIXTURES
from test_two_hop import LAZARUS, ME, MIDDLE, OTHER, Chain_, check

from amlcheck import graph
from amlcheck.adapters import ofac
from amlcheck.core.models import SourceResult
from amlcheck.storage import db

SVG = "{http://www.w3.org/2000/svg}"
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        with (FIXTURES / "ofac" / "sdn_sample.xml").open("rb") as file:
            ofac.store(connection, ofac.parse_list(file), "sha", NOW)
        yield connection


async def walked(conn: sqlite3.Connection) -> SourceResult:
    """A 2-hop walk over a fixture: LAZARUS paid MIDDLE, MIDDLE paid ME, ME paid OTHER, and a
    hub paid ME too."""
    chain = Chain_()
    chain.pay(LAZARUS, MIDDLE, "5000", days_ago=20)
    chain.pay(MIDDLE, ME, "1000")
    chain.pay(ME, OTHER, "300")
    hub = "0x" + "44" * 20
    for n in range(5):
        chain.pay(f"0x{n + 5:040x}", hub, "10")
    chain.pay(hub, ME, "50")
    return await check(conn, chain, max_transfers=3)


async def test_the_graph_draws_a_2_hop_view(conn: sqlite3.Connection) -> None:
    """Phase 4 exit criterion: the graph renders a 2-hop view for a fixture."""
    network = (await walked(conn)).evidence_meta["graph"]
    svg = graph.to_svg(network, "bsc", standalone=True)
    root = ElementTree.fromstring(svg)  # well-formed, and parsed without resolving anything
    groups = {g.get("class") for g in root.iter(f"{SVG}g")}
    assert {"node target", "node read", "node hub", "node flagged"} <= groups
    titles = [t.text for t in root.iter(f"{SVG}title")]
    assert any(t and t.startswith(LAZARUS) and "paid " + MIDDLE in t for t in titles)
    links = {a.get("href") for a in root.iter(f"{SVG}a")}
    assert f"https://bscscan.com/address/{LAZARUS}" in links
    flagged_edges = [e for e in root.iter(f"{SVG}line") if e.get("class") == "edge flagged"]
    assert len(flagged_edges) == 1
    marks = {t.text for t in root.iter(f"{SVG}text") if t.get("class") == "mark"}
    assert {"!", "H"} <= marks  # never colour alone
    legend = [t.text for t in root.iter(f"{SVG}text") if t.get("class") == "legend"]
    assert legend == [
        "this address",
        "read, nothing found",
        "sanctioned or frozen",
        "hub, not read",
        "could not be read",
    ]
    assert root.find(f"{SVG}rect") is not None  # a light background for a file


async def test_the_web_page_version_takes_the_page_colours(conn: sqlite3.Connection) -> None:
    network = (await walked(conn)).evidence_meta["graph"]
    svg = graph.to_svg(network, "bsc")
    assert ElementTree.fromstring(svg).find(f"{SVG}rect") is None
    assert "style=" not in svg  # the page's Content-Security-Policy forbids inline styles


async def test_the_table_lists_the_same_network(conn: sqlite3.Connection) -> None:
    rows = graph.walk_rows((await walked(conn)).evidence_meta["graph"])
    by_address = {r["address"]: r for r in rows}
    assert by_address[MIDDLE]["received"] == "1,000.00"
    assert by_address[MIDDLE]["paid_by"] == [(LAZARUS, "5,000.00")]
    assert by_address[OTHER]["sent"] == "300.00"
    assert by_address["0x" + "44" * 20]["state"] == "a hub, not read"


def test_text_in_a_node_cannot_become_markup() -> None:
    network = {
        "nodes": [
            {"id": ME, "ring": 0, "state": "target", "flags": []},
            {"id": MIDDLE, "ring": 1, "state": "flagged", "flags": ['<script>alert("x")</script>']},
        ],
        "edges": [{"from": MIDDLE, "to": ME, "received_usdt": "1", "sent_usdt": "0"}],
    }
    svg = graph.to_svg(network, "bsc")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_an_empty_walk_still_draws() -> None:
    network = {"nodes": [{"id": ME, "ring": 0, "state": "target", "flags": []}], "edges": []}
    root = ElementTree.fromstring(graph.to_svg(network, "bsc"))
    assert next(g.get("class") for g in root.iter(f"{SVG}g")) == "node target"


def test_look_alike_addresses_get_different_labels() -> None:
    """Seen live on 2026-09-30: two hubs that differ only in the middle (address poisoning)."""
    first = "0x6990e7e90ab50c12111f99b84183d3fe298bb3e4"
    second = "0x6990b3b03b9ed21a69440e0dfc0ce5e84dd3b3e4"
    assert graph.short(first) != graph.short(second)
    assert graph.short(first) == "0x6990e7…8bb3e4"
