import runpy
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from amlcheck import __version__
from amlcheck.cli import app

runner = CliRunner()


def line_for(output: str, label: str) -> str:
    return next(line for line in output.splitlines() if line.startswith(label))


def test_help_lists_every_prd_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("check", "batch", "sync", "status", "audit", "watch", "labels"):
        assert command in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_python_dash_m_runs_the_same_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", ["amlcheck", "--version"])
    with pytest.raises(SystemExit) as exited:
        runpy.run_module("amlcheck", run_name="__main__")
    assert exited.value.code == 0
    assert __version__ in capsys.readouterr().out


@pytest.mark.parametrize(
    ("args", "phase"),
    [
        (["check", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"], 1),
        (["sync"], 1),
        (["audit", "list"], 1),
        (["audit", "verify"], 1),
        (["labels", "import", "labels.csv"], 2),
        (["batch", "addresses.csv"], 3),
        (["audit", "export"], 3),
        (["watch", "add", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"], 3),
        (["watch", "remove", "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"], 3),
        (["watch", "list"], 3),
        (["watch", "run"], 3),
    ],
)
def test_later_phase_commands_fail_instead_of_pretending(args: list[str], phase: int) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert f"planned for Phase {phase}" in result.output


def test_status_on_a_fresh_install(isolated: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "not found, using defaults" in line_for(result.output, "Config ")
    assert "(schema v1)" in line_for(result.output, "Database")
    assert line_for(result.output, "EAGLE_VIRTUAL_API_KEY").split()[-1] == "missing"
    assert (isolated / "amlcheck.db").is_file()


def test_status_shows_a_key_is_set_without_printing_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EAGLE_VIRTUAL_API_KEY", "ev_live_do_not_print")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "ev_live_do_not_print" not in result.output
    assert line_for(result.output, "EAGLE_VIRTUAL_API_KEY").split()[-1] == "set"
    assert line_for(result.output, "TRONGRID_API_KEY").split()[-1] == "missing"


def test_status_explains_a_broken_config(isolated: Path) -> None:
    isolated.mkdir()
    (isolated / "config.toml").write_text("[freshness]\nsanctions_max_age_hours = 'soon'\n")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "Config error" in result.output
    assert "sanctions_max_age_hours" in result.output
