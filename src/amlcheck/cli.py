"""Command line (PRD §10.1)."""

import asyncio
import csv
import io
import json
import sqlite3
import threading
import tomllib
import webbrowser
from collections import Counter
from contextlib import ExitStack, closing
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn, TextIO

import httpx
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.progress import (
    DownloadColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
)
from rich.table import Table
from rich.text import Text

from amlcheck import __version__, adapters, export, graph, labels, logs, watchlist
from amlcheck import batch as batches
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
from amlcheck.core.clock import iso, utcnow
from amlcheck.core.models import Address, Chain, CheckResult, SourceHealth, Verdict
from amlcheck.inputs import amount_hint, client_name
from amlcheck.net import new_client
from amlcheck.output import (
    STATUS_STYLE,
    VERDICT_STYLE,
    attributions,
    local,
    render,
    render_walk,
    to_json,
)
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
EXIT_CHANGED = 6  # `watch run`: a watched address's verdict changed (Q12)
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
        return amount_hint(text)
    except ValueError as e:
        _fail(f"--amount {e}")


def _parsed(address: str, chain: Chain | None) -> Address:
    try:
        return parse(address, chain)
    except AddressError as e:
        _fail(str(e))


def _client(text: str | None) -> str | None:
    try:
        return client_name(text)
    except ValueError as e:
        _fail(f"--client {e}")


async def _screen(
    address: Address,
    conn: sqlite3.Connection,
    config: Config,
    secrets: Secrets,
    amount: str | None,
    note: str | None,
    client: str | None,
    two_hop: bool,
) -> CheckResult:
    async with new_client(config.network.timeout_seconds) as http:
        sources = adapters.build(
            address.chain, conn=conn, http=http, config=config, secrets=secrets, two_hop=two_hop
        )
        return await engine.screen(
            address, sources, conn=conn, config=config, amount=amount, note=note, client=client
        )


def _run_check(
    address: str,
    chain: Chain | None,
    amount: str | None,
    note: str | None,
    client: str | None,
    json_output: bool,
    *,
    investigate: bool,
    graph_file: Path | None = None,
) -> NoReturn:
    parsed = _parsed(address, chain)
    amount_hint = _amount(amount)
    client_name = _client(client)
    config = _config()
    secrets = load_secrets()
    two_hop = investigate or adapters.wants_two_hop(config, amount_hint)
    if two_hop and not json_output:
        budget = config.two_hop.time_budget_seconds
        why = "" if investigate else "The amount is large, so "
        err.print(
            f"{why}the 2-hop walk is included: this can take up to {budget:g} seconds.",
            soft_wrap=True,
        )
    with closing(_database()) as conn:
        result = asyncio.run(
            _screen(parsed, conn, config, secrets, amount_hint, note, client_name, two_hop)
        )
    if json_output:
        typer.echo(json.dumps(to_json(result), indent=2, ensure_ascii=False))
    else:
        render(result, out)
        render_walk(result, out)
    if graph_file is not None:
        network = graph.of(result)
        if network is None:
            err.print("There is no 2-hop network to draw: the walk did not run.", soft_wrap=True)
        else:
            try:
                svg = graph.to_svg(network, parsed.chain.value, standalone=True)
                graph_file.write_text(svg, encoding="utf-8")
            except OSError as e:
                _fail(f"{graph_file} could not be written: {e}")
            err.print(f"Graph written to {graph_file}", soft_wrap=True)
    raise typer.Exit(code=EXIT_FOR[result.verdict])


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
    client: Annotated[
        str | None, typer.Option(help="Client the check is for; exports can filter by it.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the JSON result.")] = False,
) -> None:
    """Screen one address. The chain is detected from the address format.

    Every check is written to the audit log before its result is shown. An --amount of at least
    [two_hop] auto_amount_usdt (10,000 by default) includes the 2-hop walk. Exit status:
    0 NO_HITS, 3 REVIEW, 4 INCOMPLETE, 5 BLOCK, 1 when the check could not run.
    """
    _run_check(address, chain, amount, note, client, json_output, investigate=False)


@app.command()
def investigate(
    address: Annotated[str, typer.Argument(help="TRON (T...) or BSC (0x...) address.")],
    chain: Annotated[
        Chain | None, typer.Option(help="Set the chain instead of detecting it.")
    ] = None,
    amount: Annotated[
        str | None, typer.Option(help="Planned amount in USDT, kept in the audit log.")
    ] = None,
    note: Annotated[str | None, typer.Option(help="Operator note, kept in the audit log.")] = None,
    client: Annotated[
        str | None, typer.Option(help="Client the check is for; exports can filter by it.")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print the JSON result.")] = False,
    graph_file: Annotated[
        Path | None, typer.Option("--graph", help="Also draw the network to this SVG file.")
    ] = None,
) -> None:
    """A check with the 2-hop walk: whom the address's largest counterparties received USDT from.

    It reads the histories of the 20 largest counterparties, so it takes up to two minutes. It is
    kept in the audit log like any check, with the same exit status. --graph draws the network.
    """
    _run_check(
        address, chain, amount, note, client, json_output, investigate=True, graph_file=graph_file
    )


async def _batch(
    rows: list[batches.Row],
    conn: sqlite3.Connection,
    config: Config,
    secrets: Secrets,
    results_file: TextIO | None,
) -> list[CheckResult]:
    writer = csv.DictWriter(results_file, batches.RESULT_COLUMNS) if results_file else None
    if writer:
        writer.writeheader()
    columns = (SpinnerColumn(), TextColumn("{task.description}"), MofNCompleteColumn())
    with Progress(*columns, console=err, transient=True) as progress:
        task = progress.add_task("Screening", total=len(rows))

        def done(row: batches.Row, result: CheckResult) -> None:
            if writer and results_file:
                writer.writerow(batches.result_row(row, result))
                results_file.flush()
            progress.advance(task)

        async with new_client(config.network.timeout_seconds) as http:
            return await batches.run(
                rows, conn=conn, http=http, config=config, secrets=secrets, done=done
            )


def _show_batch(rows: list[batches.Row], results: list[CheckResult]) -> None:
    table = Table(box=None, pad_edge=False, header_style="bold", padding=(0, 2, 0, 0))
    for column in ("Line", "Verdict", "Chain", "Address", "Findings"):
        table.add_column(column, overflow="fold")
    for row, result in zip(rows, results, strict=True):
        table.add_row(
            str(row.line),
            Text(result.verdict.value, VERDICT_STYLE[result.verdict]),
            result.address.chain.upper(),
            result.address.display,
            " ".join(dict.fromkeys(f.rule_id for f in result.findings)),
        )
    out.print(table)
    counts = Counter(r.verdict for r in results)
    tally = ", ".join(f"{counts[v]} {v}" for v in batches.WORST_FIRST if counts[v])
    out.print(f"\nScreened {len(results):,} addresses: {tally}.", soft_wrap=True)
    for line in sorted({a for r in results for a in attributions(r)}):
        out.print(Text(line, "dim"), soft_wrap=True)


@app.command()
def batch(
    file: Annotated[
        Path,
        typer.Argument(help="CSV with an address column; chain, amount, note, client optional."),
    ],
    out_file: Annotated[
        Path | None, typer.Option("--out", help="Also write one result row per address here.")
    ] = None,
    client: Annotated[str | None, typer.Option(help="Client for rows that name none.")] = None,
) -> None:
    """Screen every address in a CSV file, one at a time.

    Nothing is screened if any row is wrong. Every check goes to the audit log, and --out gets
    each result as soon as it is ready. Exit status: the worst verdict found (0 NO_HITS,
    3 REVIEW, 4 INCOMPLETE, 5 BLOCK), or 1 when the batch could not run.
    """
    default_client = _client(client)
    try:
        rows, problems = batches.read_csv(file, default_client)
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        _fail(f"{file} could not be read: {e}")
    if problems:
        shown = problems[:20] + (
            [f"... and {len(problems) - 20} more"] if len(problems) > 20 else []
        )
        _fail(f"Nothing was screened. Fix these rows in {file}:\n" + "\n".join(shown))
    config = _config()
    secrets = load_secrets()
    with ExitStack() as stack:
        results_file = None
        if out_file:
            try:
                results_file = stack.enter_context(out_file.open("w", newline="", encoding="utf-8"))
            except OSError as e:
                _fail(f"{out_file} could not be written: {e}")
        conn = stack.enter_context(closing(_database()))
        results = asyncio.run(_batch(rows, conn, config, secrets, results_file))
    _show_batch(rows, results)
    if out_file:
        out.print(f"Results written to {out_file}", soft_wrap=True)
    raise typer.Exit(code=EXIT_FOR[batches.worst(r.verdict for r in results)])


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
        "HYPERSYNC_API_TOKEN": secrets.hypersync_api_token,
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


def _filters(
    start: str | None,
    end: str | None,
    address: str | None,
    verdict: str | None,
    client: str | None,
) -> dict[str, str | None]:
    """The audit filters as stored values: ISO times for [start, end), a normalised address."""
    filters = {
        "start": iso(_day(start, "--from")) if start else None,
        "end": iso(_day(end, "--to") + timedelta(days=1)) if end else None,
        "address": None,
        "verdict": None,
        "client": _client(client),
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
    return filters


@audit_app.command("list")
def audit_list(
    start: Annotated[str | None, typer.Option("--from", help="First day, YYYY-MM-DD.")] = None,
    end: Annotated[str | None, typer.Option("--to", help="Last day, YYYY-MM-DD.")] = None,
    address: Annotated[str | None, typer.Option(help="Only checks of this address.")] = None,
    verdict: Annotated[
        str | None, typer.Option(help="BLOCK, REVIEW, INCOMPLETE or NO_HITS.")
    ] = None,
    client: Annotated[str | None, typer.Option(help="Only checks for this client.")] = None,
) -> None:
    """Browse past checks, newest first."""
    filters = _filters(start, end, address, verdict, client)
    with closing(_database()) as conn:
        rows = conn.execute(
            "SELECT created_at, verdict, chain, address_norm, check_id, amount_hint,"
            " operator_note, client"
            " FROM checks WHERE (:start IS NULL OR created_at >= :start)"
            " AND (:end IS NULL OR created_at < :end)"
            " AND (:address IS NULL OR address_norm = :address)"
            " AND (:verdict IS NULL OR verdict = :verdict)"
            " AND (:client IS NULL OR client = :client COLLATE NOCASE) ORDER BY seq DESC",
            filters,
        ).fetchall()
    if not rows:
        out.print("No checks match.")
        return
    table = Table(box=None, pad_edge=False, header_style="bold", padding=(0, 2, 0, 0))
    for column in ("Time", "Verdict", "Chain", "Address", "Check", "Client", "Amount", "Note"):
        table.add_column(column, overflow="fold")
    for created, found, chain, addr, check_id, amount, note, for_client in rows:
        table.add_row(
            local(datetime.fromisoformat(created)),
            Text(found, VERDICT_STYLE[Verdict(found)]),
            chain.upper(),
            addr,
            check_id,
            for_client or "",
            amount or "",
            note or "",
        )
    natural_width = out.measure(table, options=out.options.update_width(10_000)).maximum
    if natural_width <= out.width:
        out.print(table)
        return
    # Too narrow for the table: one block per check, so no address or ID is broken across lines.
    for created, found, chain, addr, check_id, amount, note, for_client in rows:
        when = local(datetime.fromisoformat(created))
        verdict_label = (f" {found} ", VERDICT_STYLE[Verdict(found)])
        out.print(Text.assemble(verdict_label, f"  {when}  {chain.upper()}"), soft_wrap=True)
        out.print(Text(addr), soft_wrap=True)
        out.print(Text(f"check {check_id}"), soft_wrap=True)
        extras = [f"client {for_client}"] if for_client else []
        extras += [f"amount {amount}"] if amount else []
        extras += [f"note: {note}"] if note else []
        if extras:
            out.print(Text("  ".join(extras)), soft_wrap=True)
        out.print()


@audit_app.command("export")
def audit_export(
    start: Annotated[str | None, typer.Option("--from", help="First day, YYYY-MM-DD.")] = None,
    end: Annotated[str | None, typer.Option("--to", help="Last day, YYYY-MM-DD.")] = None,
    address: Annotated[str | None, typer.Option(help="Only checks of this address.")] = None,
    verdict: Annotated[
        str | None, typer.Option(help="BLOCK, REVIEW, INCOMPLETE or NO_HITS.")
    ] = None,
    client: Annotated[str | None, typer.Option(help="Only checks for this client.")] = None,
    export_format: Annotated[
        ExportFormat, typer.Option("--format", help="Output format.")
    ] = ExportFormat.csv,
    out_file: Annotated[
        Path | None, typer.Option("--out", help="Write to this file (required for PDF).")
    ] = None,
) -> None:
    """Export past checks as CSV, JSON or PDF, oldest first.

    JSON keeps every record exactly as stored, with its hashes, so each can be checked again. JSON
    and PDF say whether the whole audit log verified at export time; if it did not, the export is
    still written, and the exit status is 1.
    """
    filters = _filters(start, end, address, verdict, client)
    if export_format is ExportFormat.pdf and out_file is None:
        _fail("--format pdf needs --out, for example --out audit.pdf")
    with closing(_database()) as conn:
        found = list(audit.records(conn, **filters))
        verification = audit.verify(conn)
    scope = export.Scope(start, end, filters["address"], filters["verdict"], filters["client"])
    now = utcnow()
    content: str | bytes
    if export_format is ExportFormat.csv:
        text = io.StringIO()
        export.write_csv(found, text)
        content = text.getvalue()
    elif export_format is ExportFormat.json:
        content = json.dumps(
            export.to_json(found, scope, verification, now), indent=2, ensure_ascii=False
        )
    else:
        content = export.to_pdf(found, scope, verification, now)
    if out_file is None:
        typer.echo(content, nl=False)
    else:
        try:
            if isinstance(content, bytes):
                out_file.write_bytes(content)
            else:
                out_file.write_text(content, encoding="utf-8", newline="")
        except OSError as e:
            _fail(f"{out_file} could not be written: {e}")
        err.print(f"Exported {len(found):,} checks to {out_file}", soft_wrap=True)
    if not verification.intact:
        _fail(
            f"The audit log is BROKEN at check {verification.broken_check_id}:"
            f" {verification.reason}. The export says so. Run `amlcheck audit verify`."
        )


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
    client: Annotated[str | None, typer.Option(help="Client the address belongs to.")] = None,
    note: Annotated[str | None, typer.Option(help="Why it is watched.")] = None,
) -> None:
    """Watch an address. It starts from its latest check in the audit log, if it has one."""
    parsed = _parsed(address, chain)
    client_name = _client(client)
    with closing(_database()) as conn:
        watched, added = watchlist.add(
            conn, parsed, client_name, (note or "").strip() or None, utcnow()
        )
    verb = "Watching" if added else "Already watched, details updated:"
    if watched.last_verdict and watched.last_checked_at:
        since = f"last verdict {watched.last_verdict}, {local(watched.last_checked_at)}"
    else:
        since = "not checked yet, so its next `watch run` sets its first verdict"
    out.print(f"{verb} {parsed.display} ({parsed.chain.upper()}): {since}", soft_wrap=True)


@watch_app.command("remove")
def watch_remove(
    address: Annotated[str, typer.Argument(help="Address to stop watching.")],
    chain: Annotated[
        Chain | None, typer.Option(help="Set the chain instead of detecting it.")
    ] = None,
) -> None:
    """Stop watching an address. Its checks stay in the audit log."""
    parsed = _parsed(address, chain)
    with closing(_database()) as conn:
        if not watchlist.remove(conn, parsed):
            _fail(f"{parsed.display} is not on the watchlist")
    out.print(f"Stopped watching {parsed.display} ({parsed.chain.upper()})", soft_wrap=True)


@watch_app.command("list")
def watch_list() -> None:
    """Show watched addresses and their last verdicts."""
    with closing(_database()) as conn:
        watched = watchlist.entries(conn)
    if not watched:
        out.print("The watchlist is empty. Add an address with `amlcheck watch add`.")
        return
    table = Table(box=None, pad_edge=False, header_style="bold", padding=(0, 2, 0, 0))
    for column in ("Address", "Chain", "Last verdict", "Last checked", "Client", "Note"):
        table.add_column(column, overflow="fold")
    for w in watched:
        verdict = (
            Text(w.last_verdict.value, VERDICT_STYLE[w.last_verdict])
            if w.last_verdict
            else Text("not checked yet", "dim")
        )
        checked = local(w.last_checked_at) if w.last_checked_at else ""
        table.add_row(
            w.address.display,
            w.address.chain.upper(),
            verdict,
            checked,
            w.client or "",
            w.note or "",
        )
    out.print(table)


async def _watch_run(
    conn: sqlite3.Connection,
    watched: list[watchlist.Watched],
    config: Config,
    secrets: Secrets,
) -> list[watchlist.Outcome]:
    columns = (SpinnerColumn(), TextColumn("{task.description}"), MofNCompleteColumn())
    with Progress(*columns, console=err, transient=True) as progress:
        task = progress.add_task("Re-screening", total=len(watched))
        async with new_client(config.network.timeout_seconds) as http:
            return await watchlist.run(
                conn,
                watched,
                http=http,
                config=config,
                secrets=secrets,
                done=lambda _: progress.advance(task),
            )


@watch_app.command("run")
def watch_run(
    notify: Annotated[
        bool,
        typer.Option(
            "--notify/--no-notify", help="Show a macOS notification when a verdict changes."
        ),
    ] = True,
) -> None:
    """Re-screen every watched address and report the verdicts that changed.

    Each check goes to the audit log. Exit status: 0 when no verdict changed, 6 when one did,
    1 when the run could not start. To run it on a schedule, see docs/scheduling.md.
    """
    config = _config()
    secrets = load_secrets()
    with closing(_database()) as conn:
        watched = watchlist.entries(conn)
        if not watched:
            out.print("The watchlist is empty. Add an address with `amlcheck watch add`.")
            return
        outcomes = asyncio.run(_watch_run(conn, watched, config, secrets))
    table = Table(box=None, pad_edge=False, header_style="bold", padding=(0, 2, 0, 0))
    for column in ("Address", "Chain", "Before", "Now", "Findings", "Client"):
        table.add_column(column, overflow="fold")
    for o in outcomes:
        before = o.watched.last_verdict
        now = Text(o.result.verdict.value, VERDICT_STYLE[o.result.verdict])
        if o.changed:
            now.append("  changed", "bold")
        table.add_row(
            o.result.address.display,
            o.result.address.chain.upper(),
            before.value if before else Text("first check", "dim"),
            now,
            " ".join(dict.fromkeys(f.rule_id for f in o.result.findings)),
            o.watched.client or "",
        )
    out.print(table)
    changes = [o for o in outcomes if o.changed]
    count = (
        f"{len(changes)} verdict changed"
        if len(changes) == 1
        else f"{len(changes)} verdicts changed"
    )
    out.print(
        f"\nRe-screened {len(outcomes):,} addresses: {count if changes else 'no verdict changed'}."
    )
    for line in sorted({a for o in outcomes for a in attributions(o.result)}):
        out.print(Text(line, "dim"), soft_wrap=True)
    if changes:
        if notify:
            watchlist.notify(f"amlcheck: {count}", watchlist.alert_text(changes))
        raise typer.Exit(code=EXIT_CHANGED)


@app.command()
def web(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1.", min=1024, max=65535)] = 8765,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the page in your browser.")
    ] = True,
) -> None:
    """Open the local web page: a check form, the history, and each check's details.

    It is served on 127.0.0.1 only, so only this computer can reach it. Stop it with Ctrl+C.
    """
    from amlcheck.web import serve  # the web stack loads only for this command

    config = _config()
    secrets = load_secrets()
    url = f"http://127.0.0.1:{port}/"
    out.print(f"amlcheck web page at {url}  (Ctrl+C stops it)", soft_wrap=True)
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    serve(config, secrets, db_path(), port)


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
