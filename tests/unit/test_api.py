import asyncio
import importlib.util
import json
import socket
import sqlite3
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import closing
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest
import uvicorn
from conftest import CHEIL_TRON, CLEAN_BSC, CLEAN_TRON, Services, runner

from amlcheck import api
from amlcheck.cli import app as cli
from amlcheck.config import db_path, load_config, load_secrets

TOKEN = "api-token-for-tests-0123456789abcdef"
BASE = "http://127.0.0.1:8766"
KEY = "8e03978e-40d5-43e8-bc93-6894a57f9324"
SCRIPT = Path(__file__).parents[2] / "scripts" / "corridor_mock.py"


def make_app() -> Any:
    return api.create_app(
        config=load_config(), secrets_=load_secrets(), database=db_path(), token=TOKEN
    )


@pytest.fixture
async def client(synced: Services) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=make_app())
    headers = {"Authorization": f"Bearer {TOKEN}"}
    async with httpx.AsyncClient(transport=transport, base_url=BASE, headers=headers) as c:
        yield c


def checks_in_log(isolated: Path) -> int:
    with closing(sqlite3.connect(isolated / "amlcheck.db")) as conn:
        count: int = conn.execute("SELECT count(*) FROM checks").fetchone()[0]
        return count


def corridor() -> ModuleType:
    spec = importlib.util.spec_from_file_location("corridor_mock", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_a_check_answers_with_the_json_contract(
    client: httpx.AsyncClient, isolated: Path
) -> None:
    response = await client.post("/v1/check", json={"address": CHEIL_TRON, "client": "ACME"})
    assert response.status_code == 200
    body = response.json()
    assert corridor().contract_problems(body) == []
    assert (body["verdict"], body["address"], body["chain"]) == ("BLOCK", CHEIL_TRON, "tron")
    assert body["client"] == "ACME"
    assert "R-SAN-01" in {f["rule_id"] for f in body["findings"]}
    assert response.headers["content-location"] == f"/v1/checks/{body['check_id']}"
    assert response.headers["cache-control"] == "no-store"
    assert "idempotent-replayed" not in response.headers
    assert checks_in_log(isolated) == 1
    # The same check, read back from the audit log, is the same answer.
    stored = await client.get(response.headers["content-location"])
    assert stored.status_code == 200
    assert stored.json() == body


def steady(body: dict[str, Any]) -> dict[str, Any]:
    """A check's JSON without what differs from one check to the next: its ID, times and hash,
    and whether an answer came from the cache."""
    kept = {k: v for k, v in body.items() if k not in ("check_id", "created_at", "record_hash")}
    kept["sources"] = [
        {k: v for k, v in s.items() if k != "as_of"}
        | {"meta": {k: v for k, v in s["meta"].items() if k != "cached"}}
        for s in body["sources"]
    ]
    kept["findings"] = [
        {k: v for k, v in f.items() if k != "observed_at"} for f in body["findings"]
    ]
    return kept


async def test_the_answer_is_the_same_json_as_check_json(client: httpx.AsyncClient) -> None:
    """PRD §10.4: the same JSON contract as `amlcheck check --json`."""
    from_api = (await client.post("/v1/check", json={"address": CLEAN_BSC})).json()
    # The command runs its own event loop, so it runs in a thread of its own.
    from_cli = await asyncio.to_thread(runner.invoke, cli, ["check", CLEAN_BSC, "--json"])
    printed = json.loads(from_cli.output)
    assert from_api.keys() == printed.keys()
    assert steady(from_api) == steady(printed)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong-token"}, {"Authorization": f"Basic {TOKEN}"}],
)
async def test_the_token_is_required(
    synced: Services, isolated: Path, headers: dict[str, str]
) -> None:
    transport = httpx.ASGITransport(app=make_app())
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as anonymous:
        response = await anonymous.post("/v1/check", json={"address": CHEIL_TRON}, headers=headers)
        assert response.status_code == 401
        assert response.headers["www-authenticate"].startswith("Bearer")
        assert response.headers["content-type"] == "application/problem+json"
        assert (await anonymous.get("/v1/health", headers=headers)).status_code == 401
        assert (await anonymous.get("/no/such/page", headers=headers)).status_code == 401
    assert checks_in_log(isolated) == 0


async def test_other_host_names_are_refused(client: httpx.AsyncClient) -> None:
    """DNS rebinding: a page on evil.example resolved to 127.0.0.1 still says Host: evil.example."""
    response = await client.get("/v1/health", headers={"Host": "evil.example"})
    assert response.status_code == 400


async def test_health(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"address": "not an address"}, 400, "Invalid address"),
        ({"address": CHEIL_TRON, "chain": "bsc"}, 400, "Invalid address"),
        ({"address": CHEIL_TRON, "amount": "-5"}, 400, "amount: must be a positive number"),
        ({"address": CHEIL_TRON, "amount": "lots"}, 400, "amount: 'lots' is not a number"),
        ({"address": CHEIL_TRON, "amount": True}, 400, "amount"),
        ({"address": CHEIL_TRON, "two_hop": "yes"}, 400, "two_hop"),
        ({"address": CHEIL_TRON, "client": "x" * 201}, 400, "client: is longer than"),
        ({"address": CHEIL_TRON, "extra": 1}, 400, "extra: Extra inputs are not permitted"),
        ({}, 400, "address: Field required"),
        ({"address": CHEIL_TRON, "note": "x" * 20_000}, 413, "at most 16,384 bytes"),
    ],
)
async def test_bad_requests_are_refused_before_any_check(
    client: httpx.AsyncClient, isolated: Path, body: dict[str, Any], status: int, detail: str
) -> None:
    response = await client.post("/v1/check", json=body)
    assert response.status_code == status
    problem = response.json()
    assert problem["status"] == status
    assert detail in f"{problem['title']} {problem['detail']}"
    assert checks_in_log(isolated) == 0


async def test_a_body_must_be_json(client: httpx.AsyncClient) -> None:
    form = await client.post("/v1/check", data={"address": CHEIL_TRON})
    assert form.status_code == 415
    broken = await client.post(
        "/v1/check", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert broken.status_code == 400


async def test_a_retry_with_the_same_key_gets_the_same_check(
    client: httpx.AsyncClient, isolated: Path
) -> None:
    """IETF Idempotency-Key draft §2.6: a retry after the first request completed."""
    ask = {"address": CHEIL_TRON, "amount": "5000", "note": "settlement 42"}
    first = await client.post("/v1/check", json=ask, headers={"Idempotency-Key": f'"{KEY}"'})
    again = await client.post("/v1/check", json=ask, headers={"Idempotency-Key": KEY})
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.headers["idempotent-replayed"] == "true"
    assert again.json() == first.json()
    assert checks_in_log(isolated) == 1
    # The request is compared as understood: another spelling of the same amount is the same.
    same = await client.post(
        "/v1/check",
        json={**ask, "amount": 5000, "note": " settlement 42 "},
        headers={"Idempotency-Key": KEY},
    )
    assert same.status_code == 200
    assert same.json()["check_id"] == first.json()["check_id"]
    # A new key is a new check.
    other = await client.post("/v1/check", json=ask, headers={"Idempotency-Key": "second-key"})
    assert other.json()["check_id"] != first.json()["check_id"]
    assert checks_in_log(isolated) == 2


@pytest.mark.parametrize(
    "change", [{"amount": "5001"}, {"address": CLEAN_TRON}, {"two_hop": True}, {"client": "B"}]
)
async def test_a_key_cannot_be_reused_for_another_request(
    client: httpx.AsyncClient, isolated: Path, change: dict[str, Any]
) -> None:
    ask = {"address": CHEIL_TRON, "amount": "5000"}
    await client.post("/v1/check", json=ask, headers={"Idempotency-Key": KEY})
    reused = await client.post(
        "/v1/check", json={**ask, **change}, headers={"Idempotency-Key": KEY}
    )
    assert reused.status_code == 422
    assert reused.json()["title"] == "Idempotency-Key is already used"
    assert checks_in_log(isolated) == 1


@pytest.mark.parametrize(
    "key", ["short", "has space in it", ("é" * 10).encode(), "x" * 129, '"unclosed']
)
async def test_a_malformed_key_is_refused(
    client: httpx.AsyncClient, isolated: Path, key: str | bytes
) -> None:
    raw = key if isinstance(key, bytes) else key.encode()
    response = await client.post(
        "/v1/check", json={"address": CHEIL_TRON}, headers={b"Idempotency-Key": raw}
    )
    assert response.status_code == 400
    assert response.json()["title"] == "Idempotency-Key is malformed"
    assert checks_in_log(isolated) == 0


async def test_a_repeat_while_the_first_runs_is_a_conflict(
    client: httpx.AsyncClient, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IETF Idempotency-Key draft §2.7: 409 while the first request is still being processed."""
    started, release = asyncio.Event(), asyncio.Event()
    real = api._screen

    async def slow(*args: Any) -> Any:
        started.set()
        await release.wait()
        return await real(*args)

    monkeypatch.setattr(api, "_screen", slow)
    ask = {"address": CHEIL_TRON}
    headers = {"Idempotency-Key": KEY}
    first = asyncio.create_task(client.post("/v1/check", json=ask, headers=headers))
    await started.wait()
    repeat = await client.post("/v1/check", json=ask, headers=headers)
    different = await client.post("/v1/check", json={"address": CLEAN_TRON}, headers=headers)
    release.set()
    done = await first
    assert repeat.status_code == 409
    assert repeat.json()["title"] == "A request is outstanding for this Idempotency-Key"
    assert different.status_code == 422
    assert done.status_code == 200
    replay = await client.post("/v1/check", json=ask, headers=headers)
    assert replay.json()["check_id"] == done.json()["check_id"]
    assert checks_in_log(isolated) == 1


async def test_a_failed_check_frees_its_key(
    synced: Services, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*args: Any) -> Any:
        raise RuntimeError("disk on fire")

    real = api._screen
    monkeypatch.setattr(api, "_screen", broken)
    transport = httpx.ASGITransport(app=make_app(), raise_app_exceptions=False)
    headers = {"Authorization": f"Bearer {TOKEN}", "Idempotency-Key": KEY}
    async with httpx.AsyncClient(transport=transport, base_url=BASE, headers=headers) as c:
        failed = await c.post("/v1/check", json={"address": CHEIL_TRON})
        assert failed.status_code == 500
        assert failed.json()["title"] == "Internal error"
        assert "disk on fire" not in failed.text
        monkeypatch.setattr(api, "_screen", real)
        retried = await c.post("/v1/check", json={"address": CHEIL_TRON})
    assert retried.status_code == 200
    assert "idempotent-replayed" not in retried.headers
    assert checks_in_log(isolated) == 1


async def test_a_tampered_record_is_not_served(client: httpx.AsyncClient, isolated: Path) -> None:
    first = await client.post(
        "/v1/check", json={"address": CHEIL_TRON}, headers={"Idempotency-Key": KEY}
    )
    with closing(sqlite3.connect(isolated / "amlcheck.db")) as conn:
        conn.execute("UPDATE checks SET verdict = 'NO_HITS'")
        conn.commit()
    stored = await client.get(first.headers["content-location"])
    replay = await client.post(
        "/v1/check", json={"address": CHEIL_TRON}, headers={"Idempotency-Key": KEY}
    )
    for response in (stored, replay):
        assert response.status_code == 500
        assert response.json()["title"] == "Audit record does not verify"


@pytest.mark.parametrize("check_id", ["00000000-0000-0000-0000-000000000000", "not-an-id"])
async def test_an_unknown_check_is_not_found(client: httpx.AsyncClient, check_id: str) -> None:
    response = await client.get(f"/v1/checks/{check_id}")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


async def test_a_large_amount_or_the_flag_walks_two_hops(client: httpx.AsyncClient) -> None:
    """Q15, as for `amlcheck check --amount`; two_hop asks for the walk at any amount."""
    small = (await client.post("/v1/check", json={"address": CLEAN_TRON, "amount": 50})).json()
    large = (await client.post("/v1/check", json={"address": CLEAN_TRON, "amount": 10_000})).json()
    asked = (await client.post("/v1/check", json={"address": CLEAN_TRON, "two_hop": True})).json()
    walked = [{s["source"] for s in body["sources"]} for body in (small, large, asked)]
    assert ["exposure_2hop" in sources for sources in walked] == [False, True, True]
    stored = await client.get(f"/v1/checks/{large['check_id']}")
    assert stored.json() == large


def test_a_short_token_is_refused(synced: Services) -> None:
    with pytest.raises(ValueError, match="at least 32"):
        api.create_app(
            config=load_config(), secrets_=load_secrets(), database=db_path(), token="short"
        )


def test_the_command_needs_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[Any] = []
    monkeypatch.setattr(api, "serve", lambda *args: served.append(args))
    missing = runner.invoke(cli, ["api"])
    assert missing.exit_code == 1
    assert "AMLCHECK_API_TOKEN must be set" in missing.output
    monkeypatch.setenv("AMLCHECK_API_TOKEN", "too-short")
    assert runner.invoke(cli, ["api"]).exit_code == 1
    monkeypatch.setenv("AMLCHECK_API_TOKEN", TOKEN)
    started = runner.invoke(cli, ["api", "--port", "9000"])
    assert started.exit_code == 0, started.output
    [(_, _, _, port, token)] = served
    assert (port, token) == (9000, TOKEN)
    assert TOKEN not in started.output


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@pytest.fixture
def server(synced: Services) -> Iterator[str]:
    """`amlcheck api` on a real port, for a caller that speaks HTTP like any other program."""
    port = free_port()
    running = uvicorn.Server(
        uvicorn.Config(make_app(), host=api.HOST, port=port, log_level="warning")
    )
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not running.started:
        assert time.monotonic() < deadline, "the API did not start"
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    running.should_exit = True
    thread.join(timeout=10)


def test_a_corridor_calls_the_api_and_gets_the_contract(server: str, isolated: Path) -> None:
    """The PRD's Phase 5 exit test: a corridor mock calls over real HTTP, with the standard library
    only, and gets the stable JSON contract."""
    mock = corridor()
    blocked, replayed = mock.screen(server, TOKEN, CHEIL_TRON, "25000", key=KEY)
    assert mock.contract_problems(blocked) == []
    assert (blocked["verdict"], replayed) == ("BLOCK", False)
    again, replayed = mock.screen(server, TOKEN, CHEIL_TRON, "25000", key=KEY)
    assert (again, replayed) == (blocked, True)
    clean, _ = mock.screen(server, TOKEN, CLEAN_BSC)
    assert mock.contract_problems(clean) == []
    assert clean["verdict"] == "NO_HITS"
    with pytest.raises(RuntimeError, match="HTTP 401"):
        mock.screen(server, "wrong", CLEAN_BSC)
    assert checks_in_log(isolated) == 2


def test_the_corridor_script_says_go_or_hold(
    server: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mock = corridor()
    monkeypatch.setattr(mock, "BASE", server)
    monkeypatch.setenv("AMLCHECK_API_TOKEN", TOKEN)
    assert mock.main(["corridor_mock.py", CLEAN_BSC]) == 0
    assert "NO_HITS" in capsys.readouterr().out
    assert mock.main(["corridor_mock.py", CHEIL_TRON, "100"]) == 2
    assert "hold the transfer" in capsys.readouterr().out
    monkeypatch.delenv("AMLCHECK_API_TOKEN")
    assert mock.main(["corridor_mock.py", CLEAN_BSC]) == 1


async def test_unknown_paths_and_methods_answer_as_problems(client: httpx.AsyncClient) -> None:
    missing = await client.get("/v1/no-such-thing")
    wrong = await client.get("/v1/check")
    for response, status in ((missing, 404), (wrong, 405)):
        assert response.status_code == status
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["status"] == status
