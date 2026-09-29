import json
import runpy
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from conftest import (
    BLACKLISTED,
    CREDIT_LINE,
    FUNNEL,
    HYPERSYNC_TOKEN,
    EagleVirtualMock,
    HyperSyncMock,
    TronGridMock,
    bsc_transfer,
    load,
    mock_ofac,
    transfer_row,
)
from typer.testing import CliRunner

from amlcheck import __version__, cli
from amlcheck.cli import app
from amlcheck.core.clock import utcnow
from amlcheck.storage import db

runner = CliRunner()

CHEIL_TRON = "TA3941uFAvmVibSkQ6fMJXxmaSNovX86mz"  # on the OFAC sample list as USDT
CLEAN_TRON = "TJwwz9NR37hjXdAV5gowj7src4avMuZZNW"  # given an ordinary, years-old history below
NEVER_USED = "TWWfj8kFnxwJr34hn1sw57rCHXSKercQsb"
CLEAN_BSC = "0x7a3f9c2e8b1d4f6a0c5e9b2d7f1a3c8e6b4d2f90"
LAZARUS = "0x098B716B8Aaf21512996dC57EB0615e2383E2f96"
LAZARUS_NEIGHBOUR = "0x098b716b8aaf21512996dc57eb0615e2383e2f97"  # one digit off: not listed
USDT_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
FROZEN_TRON = (
    "eagle_virtual/check_frozen_tron.json",
    "eagle_virtual/address_frozen_tron.json",
)


def line_for(output: str, label: str) -> str:
    return next(line for line in output.splitlines() if line.startswith(label))


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner has no terminal, so Rich would fold long addresses at 80 columns."""
    for console in (cli.out, cli.err):
        monkeypatch.setattr(console, "width", 250)


@dataclass
class Services:
    tron: TronGridMock
    eagle: EagleVirtualMock
    hypersync: HyperSyncMock


@pytest.fixture
def services(
    network: respx.MockRouter, isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> Services:
    """Mocked OFAC, TronGrid and Eagle Virtual, a key for Eagle Virtual, and no rate limit
    to wait out: the CLI runs on the real clock, so the TRON index head is "now"."""
    mock_ofac(network)
    monkeypatch.setenv("EAGLE_VIRTUAL_API_KEY", "ev_live_test")
    monkeypatch.setenv("HYPERSYNC_API_TOKEN", HYPERSYNC_TOKEN)
    isolated.mkdir(exist_ok=True)
    (isolated / "config.toml").write_text("[eagle_virtual]\nrequests_per_second = 1000\n")
    now = utcnow()
    tron = TronGridMock(network, head_time=now)
    tron.add_history(
        CLEAN_TRON,
        [
            transfer_row("in", now - timedelta(days=10), USDT_CONTRACT, CLEAN_TRON, "1000"),
            transfer_row("out", now - timedelta(days=5), CLEAN_TRON, CHEIL_TRON[:-1] + "a", "100"),
        ],
        created=now - timedelta(days=700),
    )
    tron.move_histories(now)
    hypersync = HyperSyncMock(network, int(now.timestamp()))
    hypersync.transfers = [
        bsc_transfer("0xin", now - timedelta(days=10), LAZARUS_NEIGHBOUR, CLEAN_BSC, "1000"),
        bsc_transfer("0xout", now - timedelta(days=5), CLEAN_BSC, LAZARUS_NEIGHBOUR, "100"),
    ]
    hypersync.first_tx[CLEAN_BSC] = int((now - timedelta(days=700)).timestamp())
    return Services(tron, EagleVirtualMock(network), hypersync)


@pytest.fixture
def synced(services: Services) -> Services:
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    return services


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
        (["batch", "addresses.csv"], 3),
        (["audit", "export"], 3),
        (["watch", "add", CLEAN_TRON], 3),
        (["watch", "remove", CLEAN_TRON], 3),
        (["watch", "list"], 3),
        (["watch", "run"], 3),
    ],
)
def test_later_phase_commands_fail_instead_of_pretending(args: list[str], phase: int) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 1
    assert f"planned for Phase {phase}" in result.output


def test_sync_builds_the_list_and_the_index(synced: Services) -> None:
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 0, result.output
    assert "OFAC SDN: list of 2026-09-23, 65 addresses on 19,391 entries" in result.output
    assert "TRON USDT: 0 new blacklist events" in result.output


def test_failed_download_keeps_the_previous_list(network: respx.MockRouter) -> None:
    mock_ofac(network, status=500)
    TronGridMock(network, head_time=utcnow())
    result = runner.invoke(app, ["sync"])
    assert result.exit_code == 1
    assert "The previous list stays in use" in result.output
    assert "TRON USDT: 6 new blacklist events" in result.output


def test_known_ofac_listed_address_blocks(synced: Services) -> None:
    """Phase 1 exit criterion, and AT-03 from end to end."""
    result = runner.invoke(app, ["check", CHEIL_TRON])
    assert result.exit_code == 5, result.output
    assert "BLOCK" in result.output
    assert "R-SAN-01" in result.output
    assert "CHEIL CREDIT BANK (entry 22985" in result.output


def test_known_frozen_tron_address_blocks(synced: Services) -> None:
    """Phase 1 exit criterion: Tether's blacklist and Eagle Virtual both say FROZEN."""
    synced.eagle.answers[BLACKLISTED] = FROZEN_TRON
    result = runner.invoke(app, ["check", BLACKLISTED, "--amount", "50,000", "--note", "OTC"])
    assert result.exit_code == 5, result.output
    assert result.output.count("R-FRZ-01") == 2
    assert CREDIT_LINE in result.output


def test_clean_address_is_no_hits_with_the_disclaimer(synced: Services) -> None:
    """AT-02: NO_HITS, the disclaimer, and an audit record."""
    result = runner.invoke(app, ["check", CLEAN_TRON])
    assert result.exit_code == 0, result.output
    assert "NO_HITS" in result.output
    assert "This is not a clearance." in result.output
    listed = runner.invoke(app, ["audit", "list"])
    assert CLEAN_TRON in listed.output


def test_clean_bsc_address_is_no_hits(synced: Services) -> None:
    result = runner.invoke(app, ["check", CLEAN_BSC])
    assert result.exit_code == 0, result.output
    assert "not applicable: BEP20 USDT cannot freeze an address" in result.output
    assert "2 transfers with 1 counterparty; none flagged" in result.output


def test_bsc_without_a_hypersync_token_is_incomplete(
    synced: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HYPERSYNC_API_TOKEN")
    result = runner.invoke(app, ["check", CLEAN_BSC])
    assert result.exit_code == 4, result.output
    assert "HYPERSYNC_API_TOKEN is not set" in result.output


def test_bsc_with_a_refused_token_is_incomplete_and_says_why(
    synced: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HYPERSYNC_API_TOKEN", "hs-not-a-real-token")
    result = runner.invoke(app, ["check", CLEAN_BSC])
    assert result.exit_code == 4, result.output
    assert "HyperSync answered HTTP 401: Your token is malformed" in result.output
    assert "hs-not-a-real-token" not in result.output


def test_known_frozen_sender_gives_review_end_to_end(synced: Services) -> None:
    """Phase 2 exit criterion, from the command line: REVIEW with the transaction as evidence."""
    frozen_tx = next(
        r["transaction_id"]
        for r in load("tron/transfers_TAjoXR.json")["data"]
        if r["from"] == BLACKLISTED
    )
    human = runner.invoke(app, ["check", FUNNEL])
    assert human.exit_code == 3, human.output
    assert "R-EXP-01" in human.output
    assert "REVIEW (low)" in human.output
    data = json.loads(runner.invoke(app, ["check", FUNNEL, "--json"]).stdout)
    direct = next(f for f in data["findings"] if f["rule_id"] == "R-EXP-01")
    assert [t["tx_hash"] for t in direct["evidence"]["transfers"]] == [frozen_tx]
    assert {f["rule_id"] for f in data["findings"]} == {
        "R-EXP-01",
        "R-EXP-02",
        "R-HEU-01",
        "R-HEU-02",
    }


def test_never_used_address_is_a_low_priority_review(synced: Services) -> None:
    result = runner.invoke(app, ["check", NEVER_USED])
    assert result.exit_code == 3, result.output
    assert "REVIEW (low)" in result.output
    assert "new address: no activity on chain yet" in result.output


def test_eth_listed_address_blocks_on_bsc(synced: Services) -> None:
    """OFAC lists Lazarus under ETH; on BSC the same address is the same key (V1)."""
    synced.eagle.answers[LAZARUS.lower()] = (
        "eagle_virtual/check_frozen_evm.json",
        "eagle_virtual/address_frozen_evm.json",
    )
    result = runner.invoke(app, ["check", LAZARUS, "--json"])
    assert result.exit_code == 5
    rules = {f["rule_id"] for f in json.loads(result.stdout)["findings"]}
    assert rules == {"R-SAN-01", "R-FRZ-01", "R-HEU-01"}  # R-HEU-01: no BSC transfers in the mock


def test_json_output_is_the_stable_contract(synced: Services) -> None:
    """PRD §10.3."""
    synced.eagle.answers[BLACKLISTED] = FROZEN_TRON
    result = runner.invoke(app, ["check", BLACKLISTED, "--json"])
    data = json.loads(result.stdout)
    contract = {"check_id", "created_at", "address", "chain", "verdict", "sources", "findings"}
    contract |= {"tool_version", "config_hash", "attribution"}
    assert contract <= set(data)
    assert (data["address"], data["chain"], data["verdict"]) == (BLACKLISTED, "tron", "BLOCK")
    assert data["attribution"] == [CREDIT_LINE]
    assert {"source", "status", "as_of", "meta"} <= set(data["sources"][0])
    assert {"rule_id", "severity", "source", "evidence"} <= set(data["findings"][0])


def test_without_a_key_or_data_the_check_is_incomplete(
    services: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EAGLE_VIRTUAL_API_KEY")
    result = runner.invoke(app, ["check", CLEAN_TRON])
    assert result.exit_code == 4, result.output
    assert "INCOMPLETE" in result.output
    assert "EAGLE_VIRTUAL_API_KEY is not set" in result.output
    assert "run `amlcheck sync sanctions`" in result.output


def test_invalid_address_is_refused_and_not_recorded(isolated: Path) -> None:
    """AT-01."""
    result = runner.invoke(app, ["check", "hello"])
    assert result.exit_code == 1
    assert "is not a TRON address" in result.output
    assert not (isolated / "amlcheck.db").exists()


def test_bad_checksum_is_refused() -> None:
    """AT-10."""
    result = runner.invoke(app, ["check", LAZARUS[:-1] + "F"])
    assert result.exit_code == 1
    assert "EIP-55" in result.output


@pytest.mark.parametrize("amount", ["abc", "-5", "0"])
def test_bad_amount_is_refused(amount: str) -> None:
    result = runner.invoke(app, ["check", CLEAN_TRON, "--amount", amount])
    assert result.exit_code == 1


def test_audit_list_filters_and_verify_finds_tampering(synced: Services, isolated: Path) -> None:
    """AT-11 from the command line."""
    assert runner.invoke(app, ["check", CLEAN_TRON]).exit_code == 0
    assert runner.invoke(app, ["check", CHEIL_TRON, "--note", "new client"]).exit_code == 5

    blocked = runner.invoke(app, ["audit", "list", "--verdict", "block"]).output
    assert CHEIL_TRON in blocked
    assert CLEAN_TRON not in blocked
    only_clean = runner.invoke(app, ["audit", "list", "--address", CLEAN_TRON]).output
    assert CLEAN_TRON in only_clean
    assert CHEIL_TRON not in only_clean
    assert "No checks match" in runner.invoke(app, ["audit", "list", "--to", "2000-01-01"]).output

    intact = runner.invoke(app, ["audit", "verify"])
    assert intact.exit_code == 0
    assert "intact: 2 records" in intact.output

    with closing(sqlite3.connect(isolated / "amlcheck.db")) as conn:
        conn.execute("UPDATE checks SET verdict = 'NO_HITS' WHERE seq = 2")
        conn.commit()
    broken = runner.invoke(app, ["audit", "verify"])
    assert broken.exit_code == 1
    assert "BROKEN at record 2" in broken.output


def test_audit_list_stays_readable_in_a_narrow_terminal(
    synced: Services, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No address or check ID may be split across lines: people copy them."""
    args = ["check", CHEIL_TRON, "--note", "new client", "--client", "ACME"]
    assert runner.invoke(app, args).exit_code == 5
    wide = runner.invoke(app, ["audit", "list"]).output
    header = ["Time", "Verdict", "Chain", "Address", "Check", "Client", "Amount", "Note"]
    assert wide.splitlines()[0].split() == header
    monkeypatch.setattr(cli.out, "width", 60)
    lines = runner.invoke(app, ["audit", "list"]).output.splitlines()
    assert "BLOCK" in lines[0]
    assert lines[1] == CHEIL_TRON
    assert lines[2].startswith("check ")
    assert len(lines[2].removeprefix("check ")) == 36
    assert lines[3] == "client ACME  note: new client"


def test_a_check_can_name_its_client(synced: Services) -> None:
    result = runner.invoke(app, ["check", CHEIL_TRON, "--client", "  ACME Ltd ", "--json"])
    assert result.exit_code == 5, result.output
    assert json.loads(result.output)["client"] == "ACME Ltd"
    assert runner.invoke(app, ["check", NEVER_USED]).exit_code == 3
    listed = runner.invoke(app, ["audit", "list", "--client", "acme ltd"]).output
    assert CHEIL_TRON in listed
    assert NEVER_USED not in listed
    assert "No checks match." in runner.invoke(app, ["audit", "list", "--client", "Other"]).output
    shown = runner.invoke(app, ["check", CHEIL_TRON, "--client", "ACME Ltd"]).output
    assert line_for(shown, "Client").split(maxsplit=1)[1] == "ACME Ltd"
    assert runner.invoke(app, ["audit", "verify"]).exit_code == 0


def test_a_client_name_that_is_too_long_is_refused(synced: Services) -> None:
    result = runner.invoke(app, ["check", CHEIL_TRON, "--client", "x" * 201])
    assert result.exit_code == 1
    assert "--client is longer than 200 characters" in result.output


@pytest.mark.parametrize("args", [["--verdict", "maybe"], ["--from", "2026-13-01"]])
def test_audit_list_refuses_bad_filters(args: list[str]) -> None:
    assert runner.invoke(app, ["audit", "list", *args]).exit_code == 1


def test_status_on_a_fresh_install(isolated: Path) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "not found, using defaults" in line_for(result.output, "Config ")
    assert f"(schema v{len(db.migrations())})" in line_for(result.output, "Database")
    assert line_for(result.output, "EAGLE_VIRTUAL_API_KEY").split()[-1] == "missing"
    assert line_for(result.output, "OFAC SDN").split()[2] == "error:"
    assert (isolated / "amlcheck.db").is_file()


def test_status_shows_every_source_and_never_a_key(synced: Services) -> None:
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "ev_live_test" not in result.output
    assert line_for(result.output, "EAGLE_VIRTUAL_API_KEY").split()[-1] == "set"
    assert line_for(result.output, "TRONGRID_API_KEY").split()[-1] == "missing"
    assert "ok: list of 2026-09-23" in line_for(result.output, "OFAC SDN")
    assert "ok: free plan, 4 of 1,000 calls" in line_for(result.output, "Eagle Virtual")
    assert "ok: 6 blacklist events" in line_for(result.output, "TRON USDT")
    assert "skipped: not applicable" in line_for(result.output, "BSC USDT")
    assert "ok: transfers over 180 days" in line_for(result.output, "Exposure (TRON)")
    assert "ok: transfers over 180 days" in line_for(result.output, "Exposure (BSC)")
    assert line_for(result.output, "HYPERSYNC_API_TOKEN").split()[-1] == "set"


def test_status_explains_a_broken_config(isolated: Path) -> None:
    isolated.mkdir()
    (isolated / "config.toml").write_text("[freshness]\nsanctions_max_age_hours = 'soon'\n")
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 1
    assert "Config error" in result.output
    assert "sanctions_max_age_hours" in result.output


LABELS = """address,chain,tag,note,source
TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t,TRON,Mixer,known mixer,analyst
0x098B716B8Aaf21512996dC57EB0615e2383E2f96,bsc,high_risk,,
TJwwz9NR37hjXdAV5gowj7src4avMuZZNW,tron,allowlist,our own wallet,
"""


def test_labels_import_replaces_every_label(isolated: Path, tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(LABELS)
    result = runner.invoke(app, ["labels", "import", str(path)])
    assert result.exit_code == 0, result.output
    assert "Imported 3 labels: mixer 1, high_risk 1, allowlist 1" in result.output
    path.write_text("address,chain,tag\nTR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t,tron,bridge\n")
    assert runner.invoke(app, ["labels", "import", str(path)]).exit_code == 0
    with closing(sqlite3.connect(isolated / "amlcheck.db")) as conn:
        assert conn.execute("SELECT tag FROM labels").fetchall() == [("bridge",)]


def test_labels_import_takes_nothing_from_a_file_with_a_bad_row(
    isolated: Path, tmp_path: Path
) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(LABELS)
    assert runner.invoke(app, ["labels", "import", str(path)]).exit_code == 0
    path.write_text(LABELS + "0x1234,bsc,mixer,,\nTJwwz9NR37hjXdAV5gowj7src4avMuZZNW,eth,mixer,,\n")
    result = runner.invoke(app, ["labels", "import", str(path)])
    assert result.exit_code == 1
    assert "Nothing was imported" in result.output
    assert "line 5: '0x1234' is not a TRON address" in result.output
    assert "line 6: the chain must be tron or bsc, not 'eth'" in result.output
    with closing(sqlite3.connect(isolated / "amlcheck.db")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 3


def test_labels_import_needs_the_header(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t,tron,mixer\n")
    result = runner.invoke(app, ["labels", "import", str(path)])
    assert result.exit_code == 1
    assert "the header lacks" in result.output
