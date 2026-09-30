import json
from contextlib import closing
from pathlib import Path

import pytest
from conftest import CLEAN_TRON, NEVER_USED, Services, runner
from typer.testing import Result

from amlcheck import cli, output, vendor
from amlcheck.cli import app
from amlcheck.config import Config, Vendor
from amlcheck.core import audit
from amlcheck.core.address import parse
from amlcheck.core.models import SourceStatus, Verdict
from amlcheck.storage import db

TRON = parse(CLEAN_TRON)


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner has no terminal, so Rich would fold long lines at 80 columns."""
    for console in (cli.out, cli.err):
        monkeypatch.setattr(console, "width", 250)


def test_no_vendor_is_set_up_by_default() -> None:
    assert vendor.load(Config().vendor.adapter) is None
    assert vendor.stage(Config(), "1000000") is None


@pytest.mark.parametrize("spec", ["no_such_module:Vendor", "fake_vendors:Missing", "json:loads"])
def test_a_wrong_adapter_is_reported(spec: str) -> None:
    with pytest.raises(vendor.VendorError, match=f"adapter '{spec}' could not be loaded"):
        vendor.load(spec)


def test_the_vendor_is_asked_for_a_review_or_a_large_amount() -> None:
    config = Config(vendor=Vendor(adapter="fake_vendors:Knows", min_amount_usdt=50_000))
    small = vendor.stage(config, "100")
    assert small is not None
    assert [s.source for s in small(Verdict.REVIEW)] == ["vendor"]
    assert list(small(Verdict.NO_HITS)) == []
    assert list(small(Verdict.BLOCK)) == []
    large = vendor.stage(config, "50000")
    assert large is not None
    assert [s.source for s in large(Verdict.BLOCK)] == ["vendor"]


async def test_what_the_vendor_knows_is_kept_as_evidence() -> None:
    loaded = vendor.load("fake_vendors:Knows")
    assert loaded is not None
    result = await vendor.VendorSource(loaded).check(TRON)
    assert (result.status, result.summary) == (SourceStatus.ok, "Binance (exchange) risk low")
    assert result.evidence_meta["detail"] == {"cluster": f"cluster of {CLEAN_TRON}"}
    assert result.required is False


async def test_a_vendor_that_knows_nothing_or_fails_changes_nothing() -> None:
    nothing = vendor.load("fake_vendors:KnowsNothing")
    broken = vendor.load("fake_vendors:Broken")
    assert nothing is not None
    assert broken is not None
    assert (await vendor.VendorSource(nothing).check(TRON)).summary == "nothing known"
    failed = await vendor.VendorSource(broken).check(TRON)
    assert failed.status is SourceStatus.error
    assert "the vendor is down" in failed.summary


def configured(home: Path, adapter: str) -> None:
    (home / "config.toml").write_text(
        f'[eagle_virtual]\nrequests_per_second = 1000\n[vendor]\nadapter = "{adapter}"\n'
    )


def check(address: str) -> Result:
    return runner.invoke(app, ["check", address])


def test_the_vendor_is_swapped_in_config(synced: Services, isolated: Path) -> None:
    """Phase 4 exit criterion: the vendor adapter is swappable via config."""
    configured(isolated, "fake_vendors:Knows")
    review = check(NEVER_USED)  # a new address: REVIEW (low)
    assert review.exit_code == 3, review.output
    assert "Binance (exchange) risk low" in review.output
    assert "Vendor (Knows)" in review.output
    clean = check(CLEAN_TRON)  # NO_HITS: the vendor is not asked
    assert clean.exit_code == 0, clean.output
    assert "Vendor (" not in clean.output
    configured(isolated, "fake_vendors:KnowsNothing")
    assert "Vendor (Knows nothing)" in check(NEVER_USED).output
    configured(isolated, "fake_vendors:Broken")
    broken = check(NEVER_USED)
    assert broken.exit_code == 3, broken.output  # still REVIEW: the vendor never decides
    assert "the vendor is down" in broken.output
    status = runner.invoke(app, ["status"]).output
    assert "fake_vendors:Broken, asked for REVIEW results" in status


def test_a_wrong_adapter_stops_the_check(synced: Services, isolated: Path) -> None:
    configured(isolated, "fake_vendors:Missing")
    result = check(NEVER_USED)
    assert result.exit_code == 1
    assert "could not be loaded" in result.output


def test_a_stored_check_is_rebuilt_as_it_was_answered(synced: Services, isolated: Path) -> None:
    """A replay and GET /v1/checks/{id} rebuild a check from its audit record (D42), vendor label
    included: the vendor's name is kept in its evidence."""
    configured(isolated, "fake_vendors:Knows")
    printed = json.loads(runner.invoke(app, ["check", NEVER_USED, "--json"]).output)
    with closing(db.connect(isolated / "amlcheck.db")) as conn:
        [stored] = audit.records(conn)
    assert output.from_record(stored) == printed
    [asked] = [s for s in printed["sources"] if s["source"] == "vendor"]
    assert asked["label"] == "Vendor (Knows)"
