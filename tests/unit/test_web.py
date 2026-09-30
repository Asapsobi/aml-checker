import hashlib
import sqlite3
from collections.abc import AsyncIterator
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from conftest import (
    BLACKLISTED,
    CHEIL_TRON,
    CLEAN_BSC,
    FUNNEL,
    LAZARUS,
    Services,
    bsc_transfer,
    load,
)

from amlcheck.config import db_path, load_config, load_secrets
from amlcheck.core.clock import utcnow
from amlcheck.explorer import explorer
from amlcheck.web import CSP, create_app, evidence_tree

TOKEN = "form-token-for-tests"
BASE = "http://127.0.0.1:8765"
HTMX_SHA256 = "71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de"  # npm 2.0.10


@pytest.fixture
async def page(synced: Services) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(config=load_config(), secrets_=load_secrets(), database=db_path(), token=TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        yield client


def checks_in_log(isolated: Path) -> int:
    with closing(sqlite3.connect(isolated / "amlcheck.db")) as conn:
        count: int = conn.execute("SELECT count(*) FROM checks").fetchone()[0]
        return count


async def test_the_page_carries_its_token_and_strict_headers(page: httpx.AsyncClient) -> None:
    response = await page.get("/")
    assert response.status_code == 200
    assert f'name="token" value="{TOKEN}"' in response.text
    assert response.headers["content-security-policy"] == CSP
    assert response.headers["x-frame-options"] == "DENY"
    htmx = await page.get("/static/htmx.min.js")
    assert hashlib.sha256(htmx.content).hexdigest() == HTMX_SHA256


async def test_other_host_names_are_refused(page: httpx.AsyncClient) -> None:
    """DNS rebinding: a page on evil.example resolved to 127.0.0.1 still says Host: evil.example."""
    response = await page.get("/", headers={"Host": "evil.example"})
    assert response.status_code == 400


async def test_a_check_without_the_token_is_refused(
    page: httpx.AsyncClient, isolated: Path
) -> None:
    response = await page.post("/check", data={"address": CHEIL_TRON, "token": "guess"})
    assert response.status_code == 403
    assert checks_in_log(isolated) == 0


async def test_a_check_from_the_page(page: httpx.AsyncClient, isolated: Path) -> None:
    form = {"token": TOKEN, "address": CHEIL_TRON, "client": "ACME", "note": "new client"}
    fragment = await page.post("/check", data=form, headers={"HX-Request": "true"})
    assert fragment.status_code == 200
    assert "<html" not in fragment.text  # HTMX gets only the result
    assert "BLOCK" in fragment.text
    assert "R-SAN-01" in fragment.text
    assert "client ACME" in fragment.text
    assert f'href="https://tronscan.org/#/address/{CHEIL_TRON}"' in fragment.text
    full = await page.post("/check", data=form)  # without JavaScript, the whole page
    assert "<html" in full.text
    assert "BLOCK" in full.text
    assert checks_in_log(isolated) == 2


async def test_a_wrong_address_is_explained(page: httpx.AsyncClient, isolated: Path) -> None:
    response = await page.post(
        "/check", data={"token": TOKEN, "address": "0x1234"}, headers={"HX-Request": "true"}
    )
    assert response.status_code == 200
    assert 'class="error"' in response.text
    assert checks_in_log(isolated) == 0


async def test_history_lists_and_filters(page: httpx.AsyncClient) -> None:
    await page.post("/check", data={"token": TOKEN, "address": CHEIL_TRON, "client": "ACME"})
    await page.post("/check", data={"token": TOKEN, "address": FUNNEL})
    everything = await page.get("/history")
    assert CHEIL_TRON in everything.text
    assert FUNNEL in everything.text
    only_acme = await page.get("/history", params={"client": "acme"})
    assert CHEIL_TRON in only_acme.text
    assert FUNNEL not in only_acme.text
    blocked = await page.get("/history", params={"verdict": "REVIEW"})
    assert FUNNEL in blocked.text
    assert CHEIL_TRON not in blocked.text
    wrong = await page.get("/history", params={"start": "2026-13-01"})
    assert "is not a date in the form YYYY-MM-DD" in wrong.text


async def test_the_details_link_evidence_to_the_explorer(page: httpx.AsyncClient) -> None:
    frozen_tx = next(
        r["transaction_id"]
        for r in load("tron/transfers_TAjoXR.json")["data"]
        if r["from"] == BLACKLISTED
    )
    result = await page.post("/check", data={"token": TOKEN, "address": FUNNEL})
    check_id = result.text.split('href="/checks/')[1].split('"')[0]
    detail = await page.get(f"/checks/{check_id}")
    assert detail.status_code == 200
    assert "R-EXP-01" in detail.text
    assert f'href="https://tronscan.org/#/transaction/{frozen_tx}"' in detail.text
    assert f'href="https://tronscan.org/#/address/{BLACKLISTED}"' in detail.text
    assert "Record hash" in detail.text
    missing = await page.get("/checks/not-a-check")
    assert missing.status_code == 404


async def test_text_from_a_form_cannot_become_markup(page: httpx.AsyncClient) -> None:
    note = '<script>alert("x")</script>'
    result = await page.post("/check", data={"token": TOKEN, "address": CHEIL_TRON, "note": note})
    check_id = result.text.split('href="/checks/')[1].split('"')[0]
    detail = await page.get(f"/checks/{check_id}")
    assert "&lt;script&gt;" in detail.text
    assert "<script>alert" not in detail.text


def test_explorer_links_come_only_from_real_formats() -> None:
    tx = "ab" * 32
    assert explorer("tron", tx) == f"https://tronscan.org/#/transaction/{tx}"
    assert explorer("tron", FUNNEL) == f"https://tronscan.org/#/address/{FUNNEL}"
    assert explorer("bsc", "0x" + tx) == f"https://bscscan.com/tx/0x{tx}"
    assert explorer("bsc", "0x" + "ab" * 20) == f"https://bscscan.com/address/0x{'ab' * 20}"
    assert explorer("bsc", tx) is None  # a TRON hash is not a BSC one
    assert explorer("tron", "javascript:alert(1)") is None


def test_evidence_links_only_on_the_checked_chain() -> None:
    evidence = {"transfers": [{"tx_hash": "ab" * 32, "amount_usdt": "5"}]}
    linked = evidence_tree(evidence, "tron")
    leaf = linked.children[0].children[0].children[0]
    assert (leaf.key, leaf.href) == ("tx_hash", f"https://tronscan.org/#/transaction/{'ab' * 32}")
    plain = evidence_tree(evidence, None)
    assert plain.children[0].children[0].children[0].href is None


async def test_a_2_hop_walk_shows_its_network(page: httpx.AsyncClient, synced: Services) -> None:
    now = utcnow()
    middleman = "0x3333333333333333333333333333333333333333"
    synced.hypersync.transfers += [
        bsc_transfer("0xdirty", now - timedelta(days=20), LAZARUS, middleman, "5000"),
        bsc_transfer("0xpay", now - timedelta(days=3), middleman, CLEAN_BSC, "2000"),
    ]
    assert 'name="two_hop"' in (await page.get("/")).text
    form = {"token": TOKEN, "address": CLEAN_BSC, "two_hop": "1"}
    result = await page.post("/check", data=form, headers={"HX-Request": "true"})
    assert "R-EXP-03" in result.text
    check_id = result.text.split('href="/checks/')[1].split('"')[0]
    detail = (await page.get(f"/checks/{check_id}")).text
    assert "2-hop network" in detail
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in detail
    assert f'href="https://bscscan.com/address/{middleman}"' in detail
    assert "5,000.00" in detail
