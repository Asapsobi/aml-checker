"""Command line (PRD §10.1).

Every command exists from Phase 0 so `amlcheck --help` shows the whole plan. Commands
from later phases say so and exit non-zero rather than pretend to screen anything.
"""

import sqlite3
import tomllib
from contextlib import closing
from enum import StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.text import Text

from amlcheck import __version__
from amlcheck.config import config_path, db_path, load_config, load_secrets
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


class Chain(StrEnum):
    tron = "tron"
    bsc = "bsc"


class SyncTarget(StrEnum):
    sanctions = "sanctions"
    tron_index = "tron-index"
    all = "all"


class ExportFormat(StrEnum):
    csv = "csv"
    json = "json"
    pdf = "pdf"


def _not_built(command: str, phase: int) -> NoReturn:
    err.print(f"amlcheck {command} is not built yet (planned for Phase {phase}).")
    raise typer.Exit(code=1)


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
    """Screen one address. The chain is detected from the address format."""
    _not_built("check", phase=1)


@app.command()
def batch(
    file: Annotated[Path, typer.Argument(help="CSV file of addresses.")],
    out_file: Annotated[
        Path | None, typer.Option("--out", help="Write results to this CSV.")
    ] = None,
) -> None:
    """Screen every address in a CSV file."""
    _not_built("batch", phase=3)


@app.command()
def sync(
    target: Annotated[SyncTarget, typer.Argument(help="What to refresh.")] = SyncTarget.all,
) -> None:
    """Refresh local data: the OFAC list and the TRON USDT blacklist index."""
    _not_built("sync", phase=1)


def _row(label: str, value: str, style: str = "") -> None:
    out.print(Text.assemble((f"{label:<24}", "bold"), (value, style)), soft_wrap=True)


@app.command()
def status() -> None:
    """Show the setup: config, database and API keys. Source health arrives in Phase 1."""
    path = config_path()
    try:
        config = load_config(path)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as e:
        err.print(Text(f"Config error in {path}:\n{e}"))
        raise typer.Exit(code=1) from e
    try:
        with closing(db.connect(db_path())) as conn:
            version = db.schema_version(conn)
    except (OSError, sqlite3.Error, RuntimeError) as e:
        err.print(Text(f"Database error in {db_path()}: {e}"))
        raise typer.Exit(code=1) from e
    secrets = load_secrets()

    _row("amlcheck", __version__)
    _row("Config", str(path) if path.is_file() else f"{path} (not found, using defaults)")
    _row("Config hash", config.hash()[:16])
    _row("Database", f"{db_path()} (schema v{version})")
    keys = {
        "EAGLE_VIRTUAL_API_KEY": secrets.eagle_virtual_api_key,
        "TRONGRID_API_KEY": secrets.trongrid_api_key,
    }
    for name, key in keys.items():
        _row(name, "set" if key else "missing", "green" if key else "yellow")
    _row("Sources", "health checks arrive in Phase 1")


@audit_app.command("list")
def audit_list(
    start: Annotated[str | None, typer.Option("--from", help="First day, YYYY-MM-DD.")] = None,
    end: Annotated[str | None, typer.Option("--to", help="Last day, YYYY-MM-DD.")] = None,
    address: Annotated[str | None, typer.Option(help="Only checks of this address.")] = None,
    verdict: Annotated[
        str | None, typer.Option(help="BLOCK, REVIEW, INCOMPLETE or NO_HITS.")
    ] = None,
) -> None:
    """Browse past checks."""
    _not_built("audit list", phase=1)


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
    """Recompute the hash chain and report any break."""
    _not_built("audit verify", phase=1)


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
    """Load your own labels (mixers, bridges, your own wallets) from a CSV file."""
    _not_built("labels import", phase=2)
