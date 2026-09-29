import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from amlcheck import watchlist
from amlcheck.core import audit
from amlcheck.core.address import parse
from amlcheck.core.models import CheckResult, Verdict
from amlcheck.storage import db

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)
TRON = parse("TJwwz9NR37hjXdAV5gowj7src4avMuZZNW")
BSC = parse("0x7a3f9c2e8b1d4f6a0c5e9b2d7f1a3c8e6b4d2f90")


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    with closing(db.connect(tmp_path / "amlcheck.db")) as connection:
        yield connection


def result(verdict: Verdict, address: Any = TRON, check_id: str = "c1") -> CheckResult:
    return CheckResult(check_id, NOW, address, verdict, (), (), "0.1.0", "c" * 64)


def test_a_new_entry_starts_from_the_latest_check(conn: sqlite3.Connection) -> None:
    audit.append(conn, result(Verdict.NO_HITS, check_id="old"))
    audit.append(
        conn, replace(result(Verdict.REVIEW, check_id="new"), created_at=NOW + timedelta(1))
    )
    watched, added = watchlist.add(conn, TRON, "ACME", None, NOW)
    assert added
    assert (watched.last_verdict, watched.last_check_id) == (Verdict.REVIEW, "new")
    assert watched.last_checked_at == NOW + timedelta(1)
    unchecked, _ = watchlist.add(conn, BSC, None, None, NOW)
    assert (unchecked.last_verdict, unchecked.last_checked_at) == (None, None)


def test_adding_again_updates_only_what_is_given(conn: sqlite3.Connection) -> None:
    watchlist.add(conn, TRON, "ACME", "approved", NOW)
    watched, added = watchlist.add(conn, TRON, None, "renewed", NOW + timedelta(1))
    assert not added
    assert (watched.client, watched.note, watched.added_at) == ("ACME", "renewed", NOW)


def test_entries_keep_their_order_and_can_be_removed(conn: sqlite3.Connection) -> None:
    watchlist.add(conn, TRON, None, None, NOW)
    watchlist.add(conn, BSC, None, None, NOW + timedelta(seconds=1))
    assert [w.address for w in watchlist.entries(conn)] == [TRON, BSC]
    assert watchlist.remove(conn, TRON)
    assert not watchlist.remove(conn, TRON)
    assert [w.address for w in watchlist.entries(conn)] == [BSC]


def test_a_first_check_is_not_a_change(conn: sqlite3.Connection) -> None:
    watched, _ = watchlist.add(conn, TRON, None, None, NOW)
    assert not watchlist.Outcome(watched, result(Verdict.BLOCK)).changed
    seen = replace(watched, last_verdict=Verdict.NO_HITS)
    assert not watchlist.Outcome(seen, result(Verdict.NO_HITS)).changed
    assert watchlist.Outcome(seen, result(Verdict.INCOMPLETE)).changed


def test_the_alert_names_the_worst_changes_first(conn: sqlite3.Connection) -> None:
    watched, _ = watchlist.add(conn, TRON, None, None, NOW)
    before = replace(watched, last_verdict=Verdict.NO_HITS)
    changes = [
        watchlist.Outcome(before, result(Verdict.REVIEW)),
        watchlist.Outcome(before, result(Verdict.BLOCK, BSC)),
        *(watchlist.Outcome(before, result(Verdict.INCOMPLETE)) for _ in range(3)),
    ]
    text = watchlist.alert_text(changes)
    assert text.startswith(f"{BSC.display[:10]}… NO_HITS → BLOCK; ")
    assert text.endswith("and 2 more")


class Runs:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[list[str]] = []
        self.error = error

    def __call__(self, argv: list[str], **_: Any) -> None:
        self.calls.append(argv)
        if self.error:
            raise self.error


def test_the_notification_passes_its_text_as_data() -> None:
    runs = Runs()
    shown = watchlist.notify("amlcheck", 'say "hi" & quit', platform="darwin", run=runs)
    assert shown
    assert runs.calls == [
        [
            "/usr/bin/osascript",
            "-e",
            "on run argv",
            "-e",
            "display notification (item 1 of argv) with title (item 2 of argv)",
            "-e",
            "end run",
            'say "hi" & quit',
            "amlcheck",
        ]
    ]


def test_no_notification_off_macos_or_when_it_fails() -> None:
    runs = Runs()
    assert not watchlist.notify("t", "m", platform="linux", run=runs)
    assert runs.calls == []
    failing = Runs(subprocess.CalledProcessError(1, "osascript"))
    assert not watchlist.notify("t", "m", platform="darwin", run=failing)
