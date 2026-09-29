"""The watchlist (PRD §10.1 `amlcheck watch`, U4): re-screen approved addresses on a schedule.

An address added to the watchlist starts from its latest check in the audit log, when it has one.
`watch run` screens every watched address again, like a batch, and reports each verdict that
changed. A change is shown, kept in the audit log with the check behind it, and signalled by the
exit status and, on macOS, a notification (Q12).
"""

import logging
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import httpx

from amlcheck import batch
from amlcheck.config import Config, Secrets
from amlcheck.core.address import parse
from amlcheck.core.clock import from_iso, iso
from amlcheck.core.models import Address, Chain, CheckResult, Verdict

log = logging.getLogger(__name__)

RUN_NOTE = "watchlist re-screen"  # the operator note on every check `watch run` makes
OSASCRIPT = "/usr/bin/osascript"


@dataclass(frozen=True)
class Watched:
    address: Address
    added_at: datetime
    client: str | None
    note: str | None
    last_checked_at: datetime | None
    last_verdict: Verdict | None
    last_check_id: str | None


@dataclass(frozen=True)
class Outcome:
    watched: Watched
    result: CheckResult

    @property
    def changed(self) -> bool:
        """A verdict differs from the last one. The first check of an address sets its baseline."""
        previous = self.watched.last_verdict
        return previous is not None and previous is not self.result.verdict


def _watched(row: tuple[Any, ...]) -> Watched:
    address, chain, added, client, note, checked, verdict, check_id = row
    return Watched(
        parse(address, Chain(chain)),
        from_iso(added),
        client,
        note,
        from_iso(checked) if checked else None,
        Verdict(verdict) if verdict else None,
        check_id,
    )


def entries(conn: sqlite3.Connection) -> list[Watched]:
    """Every watched address, in the order they were added."""
    rows = conn.execute(
        "SELECT address_norm, chain, added_at, client, note, last_checked_at, last_verdict,"
        " last_check_id FROM watchlist ORDER BY added_at, rowid"
    )
    return [_watched(row) for row in rows]


def find(conn: sqlite3.Connection, address: Address) -> Watched | None:
    row = conn.execute(
        "SELECT address_norm, chain, added_at, client, note, last_checked_at, last_verdict,"
        " last_check_id FROM watchlist WHERE address_norm = ? AND chain = ?",
        (address.normalized, address.chain.value),
    ).fetchone()
    return _watched(row) if row else None


def add(
    conn: sqlite3.Connection,
    address: Address,
    client: str | None,
    note: str | None,
    now: datetime,
) -> tuple[Watched, bool]:
    """Watch an address, or update the client and note of one already watched. A new entry takes
    its baseline from the address's latest check in the audit log. True when newly added."""
    existing = find(conn, address)
    with conn:
        if existing:
            conn.execute(
                "UPDATE watchlist SET client = coalesce(?, client), note = coalesce(?, note)"
                " WHERE address_norm = ? AND chain = ?",
                (client, note, address.normalized, address.chain.value),
            )
        else:
            latest = conn.execute(
                "SELECT created_at, verdict, check_id FROM checks"
                " WHERE address_norm = ? AND chain = ? ORDER BY seq DESC LIMIT 1",
                (address.normalized, address.chain.value),
            ).fetchone()
            checked, verdict, check_id = latest or (None, None, None)
            conn.execute(
                "INSERT INTO watchlist (address_norm, chain, added_at, client, note,"
                " last_checked_at, last_verdict, last_check_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    address.normalized,
                    address.chain.value,
                    iso(now),
                    client,
                    note,
                    checked,
                    verdict,
                    check_id,
                ),
            )
    watched = find(conn, address)
    if watched is None:
        raise RuntimeError(f"{address.display} was not saved to the watchlist")
    return watched, existing is None


def remove(conn: sqlite3.Connection, address: Address) -> bool:
    """Stop watching an address. False when it was not watched."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM watchlist WHERE address_norm = ? AND chain = ?",
            (address.normalized, address.chain.value),
        )
    return cursor.rowcount > 0


def _record(conn: sqlite3.Connection, result: CheckResult) -> None:
    with conn:
        conn.execute(
            "UPDATE watchlist SET last_checked_at = ?, last_verdict = ?, last_check_id = ?"
            " WHERE address_norm = ? AND chain = ?",
            (
                iso(result.created_at),
                result.verdict.value,
                result.check_id,
                result.address.normalized,
                result.address.chain.value,
            ),
        )


async def run(
    conn: sqlite3.Connection,
    watched: Sequence[Watched],
    *,
    http: httpx.AsyncClient,
    config: Config,
    secrets: Secrets,
    done: Callable[[Outcome], None] = lambda _: None,
) -> list[Outcome]:
    """Screen every watched address again, one at a time, as a batch does, and record each new
    verdict as the address's last one."""
    rows = [
        batch.Row(n, w.address, None, RUN_NOTE, w.client) for n, w in enumerate(watched, start=1)
    ]
    outcomes: list[Outcome] = []

    def finished(row: batch.Row, result: CheckResult) -> None:
        outcome = Outcome(watched[row.line - 1], result)
        _record(conn, result)
        outcomes.append(outcome)
        done(outcome)

    await batch.run(rows, conn=conn, http=http, config=config, secrets=secrets, done=finished)
    return outcomes


def notify(
    title: str,
    message: str,
    *,
    platform: str = sys.platform,
    run: Callable[..., Any] = subprocess.run,
) -> bool:
    """Show a macOS notification; False where there is none to show or it failed. The text goes to
    AppleScript as data (argv), never as script, so no address or note can change the script."""
    if platform != "darwin":
        return False
    script = [
        OSASCRIPT,
        "-e",
        "on run argv",
        "-e",
        "display notification (item 1 of argv) with title (item 2 of argv)",
        "-e",
        "end run",
        message,
        title,
    ]
    try:
        run(script, check=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("the notification could not be shown: %s", e)
        return False
    return True


def alert_text(changes: Sequence[Outcome]) -> str:
    """The notification's text: each change, the most serious first."""
    ordered = sorted(changes, key=lambda o: batch.WORST_FIRST.index(o.result.verdict))
    lines = [
        f"{o.result.address.display[:10]}… {o.watched.last_verdict} → {o.result.verdict}"
        for o in ordered[:3]
    ]
    if len(ordered) > 3:
        lines.append(f"and {len(ordered) - 3} more")
    return "; ".join(lines)
