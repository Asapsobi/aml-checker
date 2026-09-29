"""Batch checks (PRD §10.1 `amlcheck batch`, U3): screen every address in a CSV file.

The file needs a header row with an `address` column. `chain`, `amount`, `note` and `client` are
optional, and other columns are ignored. Every row is read before anything is screened, and
nothing is screened when any row is wrong. The rows are screened one at a time, sharing one
connection and one Eagle Virtual rate limit, so a batch stays within every free plan's limits.
"""

import asyncio
import csv
import sqlite3
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from amlcheck import adapters
from amlcheck.config import Config, Secrets
from amlcheck.core import engine
from amlcheck.core.address import AddressError, parse
from amlcheck.core.clock import iso
from amlcheck.core.models import Address, Chain, CheckResult, Finding, Severity, Verdict
from amlcheck.inputs import amount_hint, client_name, safe_cell
from amlcheck.net import RateLimiter, Sleep
from amlcheck.output import attributions

COLUMNS = ("address", "chain", "amount", "note", "client")
RESULT_COLUMNS = (
    "line",
    "address",
    "chain",
    "verdict",
    "findings",
    "summary",
    "client",
    "amount",
    "note",
    "check_id",
    "checked_at",
    "attribution",
)
WORST_FIRST = (Verdict.BLOCK, Verdict.INCOMPLETE, Verdict.REVIEW, Verdict.NO_HITS)
SEVERITY_RANK = {Severity.BLOCK: 0, Severity.INCOMPLETE: 1, Severity.REVIEW: 2}


@dataclass(frozen=True)
class Row:
    line: int
    address: Address
    amount: str | None
    note: str | None
    client: str | None


def read_csv(path: Path, client: str | None = None) -> tuple[list[Row], list[str]]:
    """The rows to screen, and a description of every row that is wrong. `client` applies to the
    rows that name none."""
    rows: list[Row] = []
    problems: list[str] = []
    first_seen: dict[tuple[str, str], int] = {}
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        header = {(name or "").strip().lower(): name for name in reader.fieldnames or []}
        if "address" not in header:
            return [], ["the header has no address column: use address,chain,amount,note,client"]
        for raw in reader:
            line = reader.line_num  # the file's own line: the reader skips blank lines
            cell = {key: (raw.get(header[key]) or "").strip() for key in COLUMNS if key in header}
            if not any(cell.values()):
                continue
            try:
                row = _row(line, cell, client)
            except ValueError as e:
                problems.append(f"line {line}: {e}")
                continue
            key = (row.address.normalized, row.address.chain.value)
            if key in first_seen:
                problems.append(f"line {line}: repeats line {first_seen[key]}")
                continue
            first_seen[key] = line
            rows.append(row)
    if not rows and not problems:
        problems.append("the file has no addresses")
    return rows, problems


def _row(line: int, cell: dict[str, str], default_client: str | None) -> Row:
    """One row, or a ValueError that says what is wrong with it."""
    if not cell.get("address"):
        raise ValueError("the address is empty")
    chain: Chain | None = None
    if cell.get("chain"):
        try:
            chain = Chain(cell["chain"].lower())
        except ValueError:
            raise ValueError(f"the chain must be tron or bsc, not {cell['chain']!r}") from None
    try:
        address = parse(cell["address"], chain)
    except AddressError as e:
        raise ValueError(str(e)) from None
    try:
        amount = amount_hint(cell["amount"]) if cell.get("amount") else None
    except ValueError as e:
        raise ValueError(f"the amount {e}") from None
    try:
        client = client_name(cell.get("client")) or default_client
    except ValueError as e:
        raise ValueError(f"the client {e}") from None
    return Row(line, address, amount, cell.get("note") or None, client)


def top_finding(result: CheckResult) -> Finding | None:
    """The finding that weighs most in the verdict."""
    return min(result.findings, key=lambda f: SEVERITY_RANK[f.severity], default=None)


def result_row(row: Row, result: CheckResult) -> dict[str, str]:
    """One line of the results file."""
    top = top_finding(result)
    values = {
        "line": str(row.line),
        "address": result.address.display,
        "chain": result.address.chain.value,
        "verdict": result.verdict.value,
        "findings": " ".join(dict.fromkeys(f.rule_id for f in result.findings)),
        "summary": top.summary if top else "",
        "client": row.client or "",
        "amount": row.amount or "",
        "note": row.note or "",
        "check_id": result.check_id,
        "checked_at": iso(result.created_at),
        "attribution": " ".join(attributions(result)),
    }
    return {key: safe_cell(value) for key, value in values.items()}


def worst(verdicts: Iterable[Verdict]) -> Verdict:
    return min(verdicts, key=WORST_FIRST.index, default=Verdict.NO_HITS)


async def run(
    rows: Sequence[Row],
    *,
    conn: sqlite3.Connection,
    http: httpx.AsyncClient,
    config: Config,
    secrets: Secrets,
    done: Callable[[Row, CheckResult], None],
    sleep: Sleep = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[CheckResult]:
    """Screen the rows one at a time. `done` is called after each, once it is in the audit log."""
    eagle = RateLimiter(config.eagle_virtual.requests_per_second, sleep=sleep, clock=clock)
    results = []
    for row in rows:
        sources = adapters.build(
            row.address.chain,
            conn=conn,
            http=http,
            config=config,
            secrets=secrets,
            sleep=sleep,
            eagle_limiter=eagle,
            two_hop=adapters.wants_two_hop(config, row.amount),
        )
        result = await engine.screen(
            row.address,
            sources,
            conn=conn,
            config=config,
            amount=row.amount,
            note=row.note,
            client=row.client,
        )
        results.append(result)
        done(row, result)
    return results
