import json
from collections.abc import Iterator
from datetime import datetime
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
        network.post(f"{TRONGRID}/wallet/triggerconstantcontract").mock(side_effect=self._call)
        network.post(f"{TRONGRID}/walletsolidity/getnowblock").mock(side_effect=self._head)
        network.get(f"{TRONGRID}/v1/contracts/{USDT_TRON}/events").mock(side_effect=self._events)

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
