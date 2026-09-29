"""Command line (PRD §10.1).

Every command exists so `amlcheck --help` shows the whole plan. Commands from later phases say so
and exit non-zero rather than pretend to screen anything.
"""

import asyncio
import csv
import json
import sqlite3
import tomllib
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import httpx
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.progress import DownloadColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table
from rich.text import Text

from amlcheck import __version__, adapters, labels, logs
from amlcheck.adapters import ofac, tron
from amlcheck.config import (
    Config,
    Secrets,
    config_path,
    db_path,
    home_dir,
    load_config,
    load_secrets,
)
from amlcheck.core import audit, engine
from amlcheck.core.address import AddressError, parse
from amlcheck.core.clock import iso
from amlcheck.core.models import Address, Chain, CheckResult, SourceHealth, Verdict
from amlcheck.net import new_client
from amlcheck.output import STATUS_STYLE, VERDICT_STYLE, local, render, to_json
from amlcheck.storage import db

app = typer.Typer(no_args_is_help=True, add_completion=False)
audit_app = typer.Typer(help="Browse, export and verify the audit log.", no_args_is_help=True)
watch_app = typer.Typer(help="Re-screen saved addresses on a schedule.", no_args_is_help=True)
labels_app = typer.Typer(help="Manage your own address labels.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")
app.add_typer(watch_app, name="watch")
app.add_typer(labels_app, name="labels")

out = Console()
err = Console(stderr=True)

EXIT_FAILED = 1
EXIT_FOR = {Verdict.NO_HITS: 0, Verdict.REVIEW: 3, Verdict.INCOMPLETE: 4, Verdict.BLOCK: 5}


class SyncTarget(StrEnum):
    sanctions = "sanctions"
    tron_index = "tron-index"
    all = "all"


class ExportFormat(StrEnum):
    csv = "csv"
    json = "json"
    pdf = "pdf"


def _fail(message: str) -> NoReturn:
    err.print(Text(message, "red"), soft_wrap=True)
    raise typer.Exit(code=EXIT_FAILED)


def _not_built(command: str, phase: int) -> NoReturn:
    err.print(f"amlcheck {command} is not built yet (planned for Phase {phase}).")
    raise typer.Exit(code=EXIT_FAILED)


def _config() -> Config:
    path = config_path()
    try:
        return load_config(path)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as e:
        _fail(f"Config error in {path}:\n{e}")


def _database() -> sqlite3.Connection:
    try:
        return db.connect(db_path())
    except (OSError, sqlite3.Error, RuntimeError) as e:
        _fail(f"Database error in {db_path()}: {e}")


def _show_version(value: bool) -> None:
    if value:
        out.print(f"amlcheck {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version", callback=_show_version, is_eager=True, help="Show the version and exit."
        ),
    ] = False,
) -> None:
    """Screen TRON and BSC addresses before sending or receiving USDT.

    Decision support, not a legal determination: NO_HITS is not a clearance.
    """
    logs.setup(home_dir())


def _amount(text: str | None) -> str | None:
    if text is None:
        return None
    try:
        value = Decimal(text.replace(",", ""))
    except InvalidOperation:
        _fail(f"--amount {text!r} is not a number")
    if not value.is_finite() or value <= 0:
        _fail("--amount must be a positive number")
    return format(value, "f")


async def _screen(
    address: Address,
    conn: sqlite3.Connection,
    config: Config,
    secrets: Secrets,
    amount: str | None,
    note: str | None,
) -> CheckResult:
    async with new_client(config.network.timeout_seconds) as http:
        sources = adapters.build(
            address.chain, conn=conn, http=http, config=config, secrets=secrets
        )
        return await engine.screen(
            address, sources, conn=conn, config=config, amount=amount, note=note
        )


@app.command()
def check(
    address: Annotated[str, typer.Argument(help="TRON (T...) or BSC (0x...) address.")],
    chain: Annotated[
        Chain | None, typer.Option(help="Set the chain instead of detecting it.")
    ] = None,
    amount: Annotated[
        str | None, typer.Option(help="Planned amount in USDT, kept in the audit log.")
    ] = None,
    note: Annotated[str | None, typer.Option(help="Operator note, kept in the audit log.")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the JSON result.")] = False,
) -> None:
    """Screen one address. The chain is detected from the address format.

    Every check is written to the audit log before its result is shown. Exit status:
    0 NO_HITS, 3 REVIEW, 4 INCOMPLETE, 5 BLOCK, 1 when the check could not run.
    """
    try:
        parsed = parse(address, chain)
    except AddressError as e:
        _fail(str(e))
    amount_hint = _amount(amount)
    config = _config()
    secrets = load_secrets()
    with closing(_database()) as conn:
        result = asyncio.run(_screen(parsed, conn, config, secrets, amount_hint, note))
    if json_output:
        typer.echo(json.dumps(to_json(result), indent=2, ensure_ascii=False))
    else:
        render(result, out)
    raise typer.Exit(code=EXIT_FOR[result.verdict])


@app.command()
def batch(
    file: Annotated[Path, typer.Argument(help="CSV file of addresses.")],
    out_file: Annotated[
        Path | None, typer.Option("--out", help="Write results to this CSV.")
    ] = None,
) -> None:
    """Screen every address in a CSV file."""
    _not_built("batch", phase=3)


async def _sync_sanctions(
    conn: sqlite3.Connection, http: httpx.AsyncClient, config: Config
) -> bool:
    columns = (SpinnerColumn(), TextColumn("{task.description}"), DownloadColumn())
    with Progress(*columns, console=err, transient=True) as progress:
        task = progress.add_task("Downloading the OFAC SDN list", total=None)

        def advance(done: int, total: int | None) -> None:
            progress.update(task, completed=done, total=total)

        try:
            snapshot = await ofac.sync(conn, http, config.ofac.sdn_url, advance)
        except (ofac.SyncError, httpx.HTTPError) as e:
            err.print(f"OFAC SDN: {e}. The previous list stays in use.", soft_wrap=True)
            return False
    out.print(
        f"OFAC SDN: list of {snapshot.published}, {snapshot.address_count:,} addresses"
        f" on {snapshot.record_count:,} entries"
    )
    return True


async def _sync_tron(
    conn: sqlite3.Connection, http: httpx.AsyncClient, config: Config, secrets: Secrets
) -> bool:
    if tron.index_state(conn) is None:
        out.print("TRON USDT: building the blacklist index from its start in 2020...")
    grid = adapters.tron_grid(http, config, secrets)
    try:
        report = await tron.sync_index(conn, grid, config.tron.usdt_contract)
    except (tron.TronGridError, httpx.HTTPError) as e:
        err.print(f"TRON USDT: {e}", soft_wrap=True)
        return False
    out.print(
        f"TRON USDT: {report.new_events:,} new blacklist events, indexed up to block"
        f" {report.head:,} ({local(report.head_time)})"
    )
    return True


async def _sync(
    target: SyncTarget, conn: sqlite3.Connection, config: Config, secrets: Secrets
) -> bool:
    ok = True
    async with new_client(config.network.timeout_seconds) as http:
        if target in (SyncTarget.sanctions, SyncTarget.all):
            ok = await _sync_sanctions(conn, http, config) and ok
        if target in (SyncTarget.tron_index, SyncTarget.all):
            ok = await _sync_tron(conn, http, config, secrets) and ok
    return ok


@app.command()
def sync(
    target: Annotated[SyncTarget, typer.Argument(help="What to refresh.")] = SyncTarget.all,
) -> None:
    """Refresh local data: the OFAC list (run this daily) and the TRON USDT blacklist index."""
    config = _config()
    secrets = load_secrets()
    with closing(_database()) as conn:
        ok = asyncio.run(_sync(target, conn, config, secrets))
    if not ok:
        raise typer.Exit(code=EXIT_FAILED)


def _row(label: str, value: str, style: str = "") -> None:
    out.print(Text.assemble((f"{label:<24}", "bold"), (value, style)), soft_wrap=True)


async def _health(conn: sqlite3.Connection, config: Config, secrets: Secrets) -> list[SourceHealth]:
    async with new_client(config.network.timeout_seconds) as http:
        tron_sources = adapters.build(
            Chain.tron, conn=conn, http=http, config=config, secrets=secrets
        )
        bsc_sources = adapters.build(
            Chain.bsc, conn=conn, http=http, config=config, secrets=secrets
        )
        wanted = [*tron_sources, *(s for s in bsc_sources if s.source in ("bsc_usdt", "exposure"))]
        return [await s.health() for s in wanted]


@app.command()
def status() -> None:
    """Show the setup and each source's health: list age, index lag, API calls left."""
    path = config_path()
    config = _config()
    secrets = load_secrets()
    with closing(_database()) as conn:
        version = db.schema_version(conn)
        health = asyncio.run(_health(conn, config, secrets))

    _row("amlcheck", __version__)
    _row("Config", str(path) if path.is_file() else f"{path} (not found, using defaults)")
    _row("Config hash", config.hash()[:16])
    _row("Database", f"{db_path()} (schema v{version})")
    keys = {
        "EAGLE_VIRTUAL_API_KEY": secrets.eagle_virtual_api_key,
        "TRONGRID_API_KEY": secrets.trongrid_api_key,
        "ETHERSCAN_API_KEY": secrets.etherscan_api_key,
    }
    for name, key in keys.items():
        _row(name, "set" if key else "missing", "green" if key else "yellow")
    for h in health:
        _row(h.label, f"{h.status.value}: {h.detail}", STATUS_STYLE[h.status])
        for warning in h.warnings:
            _row("", f"warning: {warning}", "yellow")


def _day(text: str, option: str) -> datetime:
    """Local midnight at the start of a YYYY-MM-DD day."""
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()  # noqa: DTZ007 - only the date is used
    except ValueError:
        _fail(f"{option} {text!r} is not a date in the form YYYY-MM-DD")
    return datetime.combine(day, time(), tzinfo=datetime.now(UTC).astimezone().tzinfo)


@audit_app.command("list")
def audit_list(
    start: Annotated[str | None, typer.Option("--from", help="First day, YYYY-MM-DD.")] = None,
    end: Annotated[str | None, typer.Option("--to", help="Last day, YYYY-MM-DD.")] = None,
    address: Annotated[str | None, typer.Option(help="Only checks of this address.")] = None,
    verdict: Annotated[
        str | None, typer.Option(help="BLOCK, REVIEW, INCOMPLETE or NO_HITS.")
    ] = None,
) -> None:
    """Browse past checks, newest first."""
    filters = {
        "start": iso(_day(start, "--from")) if start else None,
        "end": iso(_day(end, "--to") + timedelta(days=1)) if end else None,
        "address": None,
        "verdict": None,
    }
    if address:
        try:
            filters["address"] = parse(address).normalized
        except AddressError as e:
            _fail(str(e))
    if verdict:
        if verdict.upper() not in Verdict.__members__:
            _fail(f"--verdict must be one of {', '.join(Verdict)}")
        filters["verdict"] = verdict.upper()
    with closing(_database()) as conn:
        rows = conn.execute(
            "SELECT created_at, verdict, chain, address_norm, check_id, amount_hint, operator_note"
            " FROM checks WHERE (:start IS NULL OR created_at >= :start)"
            " AND (:end IS NULL OR created_at < :end)"
            " AND (:address IS NULL OR address_norm = :address)"
            " AND (:verdict IS NULL OR verdict = :verdict) ORDER BY seq DESC",
            filters,
        ).fetchall()
    if not rows:
        out.print("No checks match.")
        return
    table = Table(box=None, pad_edge=False, header_style="bold", padding=(0, 2, 0, 0))
    for column in ("Time", "Verdict", "Chain", "Address", "Check", "Amount", "Note"):
        table.add_column(column, overflow="fold")
    for created, found, chain, addr, check_id, amount, note in rows:
        table.add_row(
            local(datetime.fromisoformat(created)),
            Text(found, VERDICT_STYLE[Verdict(found)]),
            chain.upper(),
            addr,
            check_id,
            amount or "",
            note or "",
        )
    natural_width = out.measure(table, options=out.options.update_width(10_000)).maximum
    if natural_width <= out.width:
        out.print(table)
        return
    # Too narrow for the table: one block per check, so no address or ID is broken across lines.
    for created, found, chain, addr, check_id, amount, note in rows:
        when = local(datetime.fromisoformat(created))
        verdict_label = (f" {found} ", VERDICT_STYLE[Verdict(found)])
        out.print(Text.assemble(verdict_label, f"  {when}  {chain.upper()}"), soft_wrap=True)
        out.print(Text(addr), soft_wrap=True)
        out.print(Text(f"check {check_id}"), soft_wrap=True)
        extras = [f"amount {amount}"] if amount else []
        extras += [f"note: {note}"] if note else []
        if extras:
            out.print(Text("  ".join(extras)), soft_wrap=True)
        out.print()


@audit_app.command("export")
def audit_export(
    start: Annotated[str | None, typer.Option("--from", help="First day, YYYY-MM-DD.")] = None,
    end: Annotated[str | None, typer.Option("--to", help="Last day, YYYY-MM-DD.")] = None,
    export_format: Annotated[
        ExportFormat, typer.Option("--format", help="Output format.")
    ] = ExportFormat.csv,
) -> None:
    """Export checks for a date range."""
    _not_built("audit export", phase=3)


@audit_app.command("verify")
def audit_verify() -> None:
    """Recompute the hash chain and report the first record that does not match."""
    with closing(_database()) as conn:
        report = audit.verify(conn)
    if report.intact:
        out.print(
            f"Audit log intact: {report.records:,} records. Latest hash {report.head}."
            " Keep a copy of it elsewhere: records cut off the end cannot be seen otherwise.",
            soft_wrap=True,
        )
        return
    _fail(
        f"Audit log BROKEN at record {report.broken_seq} (check {report.broken_check_id}):"
        f" {report.reason}. The {report.records:,} records before it are intact."
    )


@watch_app.command("add")
def watch_add(
    address: Annotated[str, typer.Argument(help="Address to re-screen.")],
    chain: Annotated[
        Chain | None, typer.Option(help="Set the chain instead of detecting it.")
    ] = None,
) -> None:
    """Add an address to the watchlist."""
    _not_built("watch add", phase=3)


@watch_app.command("remove")
def watch_remove(address: Annotated[str, typer.Argument(help="Address to stop watching.")]) -> None:
    """Remove an address from the watchlist."""
    _not_built("watch remove", phase=3)


@watch_app.command("list")
def watch_list() -> None:
    """Show watched addresses and their last verdicts."""
    _not_built("watch list", phase=3)


@watch_app.command("run")
def watch_run() -> None:
    """Re-screen every watched address and flag verdicts that changed."""
    _not_built("watch run", phase=3)


@labels_app.command("import")
def labels_import(
    file: Annotated[Path, typer.Argument(help="CSV with columns address,chain,tag,note,source.")],
) -> None:
    """Replace your labels with the ones in a CSV file.

    Tags mixer, bridge and high_risk ([heuristics] risky_tags) raise R-HEU-05. Counterparties
    tagged allowlist are left out of the behaviour rules. Nothing is imported if any row is wrong.
    """
    try:
        found, problems = labels.read_csv(file)
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        _fail(f"{file} could not be read: {e}")
    if problems:
        shown = problems[:20] + (
            [f"... and {len(problems) - 20} more"] if len(problems) > 20 else []
        )
        _fail(f"Nothing was imported. Fix these rows in {file}:\n" + "\n".join(shown))
    with closing(_database()) as conn:
        labels.replace(conn, found)
    tags = Counter(label.tag for label in found)
    listed = ", ".join(f"{tag} {count:,}" for tag, count in tags.most_common())
    out.print(f"Imported {len(found):,} labels" + (f": {listed}" if listed else ""), soft_wrap=True)
