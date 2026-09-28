import json
from collections.abc import Iterator
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from amlcheck.config import Ofac
from amlcheck.core.address import tron_from_hex

FIXTURES = Path(__file__).parent / "fixtures"
TRONGRID = "https://api.trongrid.io"
EAGLE_VIRTUAL = "https://eaglevirtual.com"
USDT_TRON = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
CREDIT_LINE = "Data from Eagle Virtual, https://eaglevirtual.com/license"
BLACKLISTED = "TAQM43owNJLZz3vh3PXxBu2qTWf2McMQwJ"  # in events_AddedBlackList.json, 2026-09-27
FUNNEL = "TAjoXRsomrsDDCXsxD1ELFQu4wHfF9HZSv"  # received 500,000 USDT from BLACKLISTED (V10)


def load(name: str) -> Any:
    """A recorded response from tests/fixtures (see the README there)."""
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run every test with an empty amlcheck home and working directory and no API keys,
    so a developer's real ~/.amlcheck or .env can never leak into a result."""
    home = tmp_path / "home"
    monkeypatch.setenv("AMLCHECK_HOME", str(home))
    monkeypatch.delenv("AMLCHECK_CONFIG", raising=False)
    for key in ("EAGLE_VIRTUAL_API_KEY", "TRONGRID_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return home


@pytest.fixture(autouse=True)
def network() -> Iterator[respx.MockRouter]:
    """No test reaches the real network: a request that no test mocked fails the test."""
    with respx.mock(assert_all_called=False) as router:
        yield router


def transfer_row(tx: str, when: datetime, sender: str, recipient: str, usdt: str) -> dict[str, Any]:
    """A TronGrid TRC20 transfer row, shaped like those in tron/transfers_TAjoXR.json."""
    return {
        "transaction_id": tx,
        "token_info": {"symbol": "USDT", "address": USDT_TRON, "decimals": 6, "name": "Tether USD"},
        "block_timestamp": int(when.timestamp() * 1000),
        "from": sender,
        "to": recipient,
        "type": "Transfer",
        "value": str(int(Decimal(usdt) * 1_000_000)),
    }


class TronGridMock:
    """TronGrid answering from the recorded responses in tests/fixtures/tron."""

    def __init__(self, network: respx.MockRouter, head_time: datetime | None = None) -> None:
        self.blacklisted = {BLACKLISTED}
        self.deprecated = False
        self.failing: set[str] = set()
        self.event_params: list[dict[str, str]] = []
        self.head = load("tron/solidity_nowblock.json")
        if head_time is not None:
            self.head["block_header"]["raw_data"]["timestamp"] = int(head_time.timestamp() * 1000)
        # address -> (its transfer rows, newest first; its /wallet/getaccount answer)
        self.histories: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {
            FUNNEL: (
                load("tron/transfers_TAjoXR.json")["data"],
                load("tron/getaccount_TAjoXR.json"),
            ),
        }
        self.page_size = 200
        network.post(f"{TRONGRID}/wallet/triggerconstantcontract").mock(side_effect=self._call)
        network.post(f"{TRONGRID}/walletsolidity/getnowblock").mock(side_effect=self._head)
        network.get(f"{TRONGRID}/v1/contracts/{USDT_TRON}/events").mock(side_effect=self._events)
        network.get(url__regex=rf"{TRONGRID}/v1/accounts/(?P<address>\w+)/transactions/trc20").mock(
            side_effect=self._transfers
        )
        network.post(f"{TRONGRID}/wallet/getaccount").mock(side_effect=self._account)

    def add_history(
        self, address: str, rows: list[dict[str, Any]], created: datetime | None
    ) -> None:
        account: dict[str, Any] = {"address": address}
        if created is not None:
            account["create_time"] = int(created.timestamp() * 1000)
        newest_first = sorted(rows, key=lambda r: r["block_timestamp"], reverse=True)
        self.histories[address] = (newest_first, account if created else {})

    def move_histories(self, now: datetime) -> None:
        """Shift every history, keeping its shape, so its newest transfer is two days before
        `now`: tests on the real clock then stay inside the lookback, whatever the date."""
        for address, (rows, account) in list(self.histories.items()):
            if not rows:
                continue
            target = int((now.timestamp() - 2 * 86400) * 1000)
            shift = target - max(r["block_timestamp"] for r in rows)
            moved = [{**r, "block_timestamp": r["block_timestamp"] + shift} for r in rows]
            if account.get("create_time"):
                account = {**account, "create_time": account["create_time"] + shift}
            self.histories[address] = (moved, account)

    def _transfers(self, request: httpx.Request, address: str) -> httpx.Response:
        if "transfers" in self.failing:
            return httpx.Response(500)
        rows = self.histories.get(address, ([], {}))[0]
        params = request.url.params
        if params.get("order_by") == "block_timestamp,asc":
            oldest = sorted(rows, key=lambda r: r["block_timestamp"])[:1]
            return httpx.Response(200, json={"data": oldest, "success": True, "meta": {}})
        since = int(params.get("min_timestamp", 0))
        wanted = [r for r in rows if r["block_timestamp"] >= since]
        start = int(params.get("start", 0))  # this mock's stand-in for TronGrid's fingerprint
        page = wanted[start : start + self.page_size]
        meta: dict[str, Any] = {"page_size": len(page)}
        if start + self.page_size < len(wanted):
            meta["links"] = {
                "next": f"{TRONGRID}/v1/accounts/{address}/transactions/trc20"
                f"?min_timestamp={since}&start={start + self.page_size}"
            }
        return httpx.Response(200, json={"data": page, "success": True, "meta": meta})

    def _account(self, request: httpx.Request) -> httpx.Response:
        address = json.loads(request.content)["address"]
        return httpx.Response(200, json=self.histories.get(address, ([], {}))[1])

    def _call(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        signature = body["function_selector"]
        if signature in self.failing:
            return httpx.Response(500)
        if signature == "deprecated()":
            answer = load("tron/deprecated_false.json")
            if self.deprecated:
                answer["constant_result"] = ["0" * 63 + "1"]
            return httpx.Response(200, json=answer)
        who = tron_from_hex(body["parameter"][-40:])
        name = "true" if who in self.blacklisted else "false"
        return httpx.Response(200, json=load(f"tron/isBlackListed_{name}.json"))

    def _head(self, request: httpx.Request) -> httpx.Response:
        if "head" in self.failing:
            return httpx.Response(500)
        return httpx.Response(200, json=self.head)

    def _events(self, request: httpx.Request) -> httpx.Response:
        self.event_params.append(dict(request.url.params))
        page = load(f"tron/events_{request.url.params['event_name']}.json")
        page["meta"].pop("links", None)  # one recorded page per event type
        return httpx.Response(200, json=page)


class EagleVirtualMock:
    """Eagle Virtual answering CLEAR for any address, unless `answers` says otherwise."""

    def __init__(self, network: respx.MockRouter) -> None:
        # address -> (the /v1/check fixture, the /v1/address fixture)
        self.answers: dict[str, tuple[str, str]] = {}
        network.get(url__regex=rf"{EAGLE_VIRTUAL}/v1/check/(?P<address>\w+)").mock(
            side_effect=self._check
        )
        network.get(url__regex=rf"{EAGLE_VIRTUAL}/v1/address/(?P<address>\w+)").mock(
            side_effect=self._address
        )
        network.get(f"{EAGLE_VIRTUAL}/v1/usage").respond(200, json=load("eagle_virtual/usage.json"))

    def _reply(self, fixture: str) -> httpx.Response:
        return httpx.Response(200, json=load(fixture), headers={"x-ev-credit-line": CREDIT_LINE})

    def _check(self, request: httpx.Request, address: str) -> httpx.Response:
        fixture = self.answers.get(address, ("eagle_virtual/check_clear.json", ""))[0]
        return self._reply(fixture)

    def _address(self, request: httpx.Request, address: str) -> httpx.Response:
        return self._reply(self.answers[address][1])


def mock_ofac(network: respx.MockRouter, status: int = 200) -> None:
    """The SLS download: a redirect to a signed S3 URL, as the real service does (V1)."""
    network.get(Ofac().sdn_url).respond(302, headers={"Location": "https://s3.test/SDN.XML"})
    content = (FIXTURES / "ofac" / "sdn_sample.xml").read_bytes()
    network.get("https://s3.test/SDN.XML").respond(
        status, content=content if status == 200 else b""
    )
