import csv
import io
import json
import runpy
import sqlite3
import sys
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
import respx
from conftest import (
    BLACKLISTED,
    CHEIL_TRON,
    CLEAN_BSC,
    CLEAN_TRON,
    CREDIT_LINE,
    FROZEN_TRON,
    FUNNEL,
    LAZARUS,
    NEVER_USED,
    Services,
    TronGridMock,
    bsc_transfer,
    load,
    mock_ofac,
)
from pypdf import PdfReader
from typer.testing import CliRunner

from amlcheck import __version__, cli, watchlist
from amlcheck.cli import app
from amlcheck.core.clock import utcnow
from amlcheck.storage import db

runner = CliRunner()


def line_for(output: str, label: str) -> str:
    return next(line for line in output.splitlines() if line.startswith(label))


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner has no terminal, so Rich would fold long addresses at 80 columns."""
    for console in (cli.out, cli.err):
        monkeypatch.setattr(console, "width", 250)


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


def test_batch_screens_every_row_and_writes_the_results(synced: Services, tmp_path: Path) -> None:
    source = tmp_path / "clients.csv"
    source.write_text(
        "Address,Chain,Amount,Note,Client,Name\n"
        f"{CHEIL_TRON},,,,,Cheil\n"
        f'{CLEAN_TRON},tron,"1,000",first deal,,Alice\n'
        "\n"
        f"{CLEAN_BSC},bsc,,,Other Ltd,Bob\n"
    )
    results = tmp_path / "results.csv"
    run = ["batch", str(source), "--out", str(results), "--client", "ACME"]
    result = runner.invoke(app, run)
    assert result.exit_code == 5, result.output  # the worst verdict: BLOCK
    assert "Screened 3 addresses: 1 BLOCK, 2 NO_HITS." in result.output
    assert CREDIT_LINE in result.output
    with results.open(newline="") as file:
        written = list(csv.DictReader(file))
    assert [(r["line"], r["verdict"], r["client"]) for r in written] == [
        ("2", "BLOCK", "ACME"),
        ("3", "NO_HITS", "ACME"),
        ("5", "NO_HITS", "Other Ltd"),
    ]
    assert written[0]["findings"].startswith("R-SAN-01")
    assert written[1]["amount"] == "1000"
    listed = runner.invoke(app, ["audit", "list", "--client", "acme"]).output
    for row in written[:2]:
        assert row["check_id"] in listed
    assert runner.invoke(app, ["audit", "verify"]).exit_code == 0


def test_batch_screens_nothing_when_a_row_is_wrong(synced: Services, tmp_path: Path) -> None:
    source = tmp_path / "clients.csv"
    source.write_text(f"address,chain\n{CHEIL_TRON},tron\n{CLEAN_BSC},tron\n")
    result = runner.invoke(app, ["batch", str(source)])
    assert result.exit_code == 1
    assert "Nothing was screened" in result.output
    assert "line 3:" in result.output
    assert "No checks match." in runner.invoke(app, ["audit", "list"]).output


@pytest.fixture
def notices(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Notifications `watch run` would show, recorded instead of shown."""
    shown: list[tuple[str, str]] = []

    def record(title: str, message: str) -> bool:
        shown.append((title, message))
        return True

    monkeypatch.setattr(watchlist, "notify", record)
    return shown


def test_the_watchlist_starts_from_the_audit_log(synced: Services) -> None:
    assert runner.invoke(app, ["check", CHEIL_TRON]).exit_code == 5
    added = runner.invoke(app, ["watch", "add", CHEIL_TRON, "--client", "ACME"]).output
    assert f"Watching {CHEIL_TRON} (TRON): last verdict BLOCK" in added
    fresh = runner.invoke(app, ["watch", "add", CLEAN_TRON]).output
    assert "not checked yet" in fresh
    again = runner.invoke(app, ["watch", "add", CLEAN_TRON, "--note", "approved 2026-09"]).output
    assert "Already watched, details updated" in again
    listed = runner.invoke(app, ["watch", "list"]).output
    assert "ACME" in line_for(listed, CHEIL_TRON)
    assert "approved 2026-09" in line_for(listed, CLEAN_TRON)
    assert runner.invoke(app, ["watch", "remove", CLEAN_TRON]).exit_code == 0
    gone = runner.invoke(app, ["watch", "remove", CLEAN_TRON])
    assert gone.exit_code == 1
    assert "is not on the watchlist" in gone.output


def test_watch_run_reports_a_changed_verdict(
    synced: Services, isolated: Path, notices: list[tuple[str, str]]
) -> None:
    (isolated / "config.toml").write_text(
        "[eagle_virtual]\nrequests_per_second = 1000\n[cache]\ntarget_ttl_seconds = 0\n"
    )
    assert "The watchlist is empty" in runner.invoke(app, ["watch", "run"]).output
    runner.invoke(app, ["watch", "add", CLEAN_TRON, "--client", "ACME"])
    first = runner.invoke(app, ["watch", "run"])
    assert first.exit_code == 0, first.output
    assert "first check" in line_for(first.output, CLEAN_TRON)
    assert "no verdict changed" in first.output
    same = runner.invoke(app, ["watch", "run"])
    assert same.exit_code == 0, same.output
    synced.eagle.answers[CLEAN_TRON] = FROZEN_TRON  # Tether froze it since the last run
    changed = runner.invoke(app, ["watch", "run"])
    assert changed.exit_code == 6, changed.output
    row = line_for(changed.output, CLEAN_TRON).split()
    assert row[2:5] == ["NO_HITS", "BLOCK", "changed"]
    assert "1 verdict changed" in changed.output
    assert notices == [("amlcheck: 1 verdict changed", f"{CLEAN_TRON[:10]}… NO_HITS → BLOCK")]
    listed = runner.invoke(app, ["watch", "list"]).output
    assert "BLOCK" in line_for(listed, CLEAN_TRON)
    checks = runner.invoke(app, ["audit", "list", "--client", "ACME"]).output
    assert checks.count("watchlist re-screen") == 3
    quiet = runner.invoke(app, ["watch", "run", "--no-notify"])
    assert quiet.exit_code == 0  # BLOCK again: no change
    assert len(notices) == 1


def test_audit_export_in_every_format(synced: Services, tmp_path: Path) -> None:
    runner.invoke(app, ["check", CHEIL_TRON, "--client", "ACME"])
    runner.invoke(app, ["check", CLEAN_TRON, "--client", "Other"])
    shown = runner.invoke(app, ["audit", "export", "--client", "acme"])
    assert shown.exit_code == 0, shown.output
    rows = list(csv.DictReader(io.StringIO(shown.stdout)))
    assert [(r["address"], r["verdict"], r["client"]) for r in rows] == [
        (CHEIL_TRON, "BLOCK", "ACME")
    ]
    saved = tmp_path / "audit.json"
    written = runner.invoke(app, ["audit", "export", "--format", "json", "--out", str(saved)])
    assert written.exit_code == 0, written.output
    data = json.loads(saved.read_text())
    assert [r["check"]["client"] for r in data["records"]] == ["ACME", "Other"]
    assert data["audit_log"]["intact"]
    assert data["attribution"] == [CREDIT_LINE]
    refused = runner.invoke(app, ["audit", "export", "--format", "pdf"])
    assert refused.exit_code == 1
    assert "--format pdf needs --out" in refused.output
    pdf = tmp_path / "audit.pdf"
    made = runner.invoke(app, ["audit", "export", "--format", "pdf", "--out", str(pdf)])
    assert made.exit_code == 0, made.output
    text = PdfReader(pdf).pages[0].extract_text()
    assert CHEIL_TRON in text
    assert "Audit log intact at export: 2 records" in text


def test_audit_export_of_a_tampered_log_says_so(
    synced: Services, isolated: Path, tmp_path: Path
) -> None:
    runner.invoke(app, ["check", CHEIL_TRON])
    with closing(sqlite3.connect(isolated / "amlcheck.db")) as conn, conn:
        conn.execute("UPDATE checks SET verdict = 'NO_HITS'")
    saved = tmp_path / "audit.json"
    result = runner.invoke(app, ["audit", "export", "--format", "json", "--out", str(saved)])
    assert result.exit_code == 1
    assert "The audit log is BROKEN" in result.output
    assert json.loads(saved.read_text())["audit_log"]["intact"] is False


MIDDLEMAN = "0x3333333333333333333333333333333333333333"


def paid_through_a_middleman(services: Services) -> None:
    """LAZARUS (on the OFAC list) paid the middleman 5,000 USDT, who paid CLEAN_BSC 2,000."""
    now = utcnow()
    services.hypersync.transfers += [
        bsc_transfer("0xdirty", now - timedelta(days=20), LAZARUS, MIDDLEMAN, "5000"),
        bsc_transfer("0xpay", now - timedelta(days=3), MIDDLEMAN, CLEAN_BSC, "2000"),
    ]


def test_investigate_walks_two_hops(synced: Services) -> None:
    paid_through_a_middleman(synced)
    result = runner.invoke(app, ["investigate", CLEAN_BSC])
    assert result.exit_code == 3, result.output  # REVIEW
    assert "the 2-hop walk is included" in result.output
    assert "R-EXP-03" in line_for(result.output, " REVIEW           R-EXP-03")
    walk = line_for(result.output, MIDDLEMAN)
    assert "read" in walk
    assert f"{LAZARUS.lower()} 5,000.00" in walk
    assert "1 received USDT from a flagged wallet" in line_for(result.output, "Exposure (2-hop)")
    data = json.loads(runner.invoke(app, ["investigate", CLEAN_BSC, "--json"]).stdout)
    walked = next(s for s in data["sources"] if s["source"] == "exposure_2hop")
    assert walked["status"] == "ok"
    assert {n["id"] for n in walked["meta"]["graph"]["nodes"]} >= {MIDDLEMAN, LAZARUS.lower()}


def test_a_large_amount_includes_the_walk(synced: Services) -> None:
    paid_through_a_middleman(synced)
    large = runner.invoke(app, ["check", CLEAN_BSC, "--amount", "20,000"])
    assert large.exit_code == 3, large.output
    assert "The amount is large, so the 2-hop walk is included" in large.output
    assert "Exposure (2-hop)" in large.output
    small = runner.invoke(app, ["check", CLEAN_BSC, "--amount", "500"])
    assert small.exit_code == 0, small.output
    assert "Exposure (2-hop)" not in small.output


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


def test_labels_import_names_the_file_line_past_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text(LABELS + "\n\nTJwwz9NR37hjXdAV5gowj7src4avMuZZNW,eth,mixer,,\n")
    result = runner.invoke(app, ["labels", "import", str(path)])
    assert "line 7: the chain must be tron or bsc, not 'eth'" in result.output


def test_labels_import_needs_the_header(tmp_path: Path) -> None:
    path = tmp_path / "labels.csv"
    path.write_text("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t,tron,mixer\n")
    result = runner.invoke(app, ["labels", "import", str(path)])
    assert result.exit_code == 1
    assert "the header lacks" in result.output
