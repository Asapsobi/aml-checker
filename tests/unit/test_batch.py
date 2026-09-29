import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
from conftest import HYPERSYNC_TOKEN, EagleVirtualMock, HyperSyncMock
from pydantic import SecretStr

from amlcheck import batch
from amlcheck.config import Config, Secrets
from amlcheck.core.models import Chain, CheckResult, Verdict
from amlcheck.storage import db

TRON = "TJwwz9NR37hjXdAV5gowj7src4avMuZZNW"
BSC = "0x7a3f9c2e8b1d4f6a0c5e9b2d7f1a3c8e6b4d2f90"
NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def write(tmp_path: Path, text: str, encoding: str = "utf-8") -> Path:
    path = tmp_path / "addresses.csv"
    path.write_text(text, encoding=encoding)
    return path


def test_rows_are_read_with_their_optional_columns(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        " Address ,CHAIN,amount,Note,client,name\n"
        f'{TRON},,"1,000",first deal,,Alice\n'
        "\n"
        f"{BSC.upper().replace('0X', '0x')},bsc,,,Other Ltd,Bob\n",
    )
    rows, problems = batch.read_csv(path, client="ACME")
    assert problems == []
    assert [(r.line, r.address.chain, r.amount, r.note, r.client) for r in rows] == [
        (2, Chain.tron, "1000", "first deal", "ACME"),
        (4, Chain.bsc, None, None, "Other Ltd"),
    ]
    assert rows[1].address.normalized == BSC


def test_a_spreadsheet_bom_and_an_address_only_file_are_fine(tmp_path: Path) -> None:
    rows, problems = batch.read_csv(write(tmp_path, f"address\n{TRON}\n", "utf-8-sig"))
    assert (problems, [r.address.normalized for r in rows]) == ([], [TRON])


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("chain\ntron\n", "the header has no address column"),
        ("address\n\n", "the file has no addresses"),
        ("address,chain\n,tron\n", "line 2: the address is empty"),
        (f"address,chain\n{TRON},eth\n", "line 2: the chain must be tron or bsc, not 'eth'"),
        ("address\nTnotanaddress\n", "line 2: "),
        (f"address,amount\n{TRON},lots\n", "line 2: the amount 'lots' is not a number"),
        (f"address,amount\n{TRON},-5\n", "line 2: the amount must be a positive number"),
        (f"address,client\n{TRON},{'x' * 201}\n", "line 2: the client is longer than 200"),
        (f"address\n{TRON}\n{TRON}\n", "line 3: repeats line 2"),
    ],
)
def test_every_wrong_row_is_reported(tmp_path: Path, text: str, problem: str) -> None:
    _, problems = batch.read_csv(write(tmp_path, text))
    assert any(p.startswith(problem) for p in problems), problems


def test_a_result_cell_cannot_run_as_a_formula(tmp_path: Path) -> None:
    rows, _ = batch.read_csv(write(tmp_path, f'address,note\n{TRON},=HYPERLINK("x")\n'))
    result = CheckResult("id", NOW, rows[0].address, Verdict.NO_HITS, (), (), "0.1.0", "c" * 64)
    assert batch.result_row(rows[0], result)["note"] == '\'=HYPERLINK("x")'


def test_the_worst_verdict_wins() -> None:
    assert batch.worst([Verdict.NO_HITS, Verdict.REVIEW, Verdict.INCOMPLETE]) is Verdict.INCOMPLETE
    assert batch.worst([Verdict.REVIEW, Verdict.BLOCK]) is Verdict.BLOCK
    assert batch.worst([]) is Verdict.NO_HITS


class VirtualTime:
    """A clock that only moves when something sleeps."""

    def __init__(self) -> None:
        self.now = 0.0

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


async def test_a_batch_of_100_keeps_to_the_eagle_virtual_rate(
    conn: sqlite3.Connection, network: respx.MockRouter, tmp_path: Path
) -> None:
    """Phase 3 exit criterion: 100 addresses, one Eagle Virtual call a second (the Free plan)."""
    time = VirtualTime()
    eagle = EagleVirtualMock(network, clock=time.clock)
    HyperSyncMock(network, int(NOW.timestamp()))
    addresses = ["0x" + secrets.token_hex(20) for _ in range(100)]
    rows, problems = batch.read_csv(write(tmp_path, "address\n" + "\n".join(addresses) + "\n"))
    assert problems == []
    keys = Secrets(
        eagle_virtual_api_key=SecretStr("ev_live_test"),
        hypersync_api_token=SecretStr(HYPERSYNC_TOKEN),
    )
    seen: list[int] = []
    async with httpx.AsyncClient() as http:
        results = await batch.run(
            rows,
            conn=conn,
            http=http,
            config=Config(),
            secrets=keys,
            done=lambda row, _: seen.append(row.line),
            sleep=time.sleep,
            clock=time.clock,
        )
    assert seen == list(range(2, 102))
    assert conn.execute("SELECT count(*) FROM checks").fetchone()[0] == 100
    assert len(eagle.times) == 100
    gaps = [later - earlier for earlier, later in zip(eagle.times, eagle.times[1:], strict=False)]
    assert min(gaps) >= 1.0 - 1e-9
    assert {r.verdict for r in results} == {Verdict.INCOMPLETE}  # no OFAC list synced here
    first = batch.result_row(rows[0], results[0])
    assert (first["line"], first["verdict"], first["check_id"]) == (
        "2",
        "INCOMPLETE",
        results[0].check_id,
    )
    assert "R-SYS-01" in first["findings"]
