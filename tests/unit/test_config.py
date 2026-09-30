import tomllib
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from amlcheck.config import Config, Secrets, config_path, db_path, load_config, load_secrets

REPO = Path(__file__).parents[2]


def value(secret: SecretStr | None) -> str | None:
    return secret.get_secret_value() if secret else None


def test_defaults_match_the_prd() -> None:
    config = load_config()
    assert config.freshness.sanctions_max_age_hours == 48
    assert config.freshness.tron_index_max_lag_minutes == 60
    assert config.cache.target_ttl_seconds == 900
    assert config.eagle_virtual.requests_per_second == 1
    assert config.eagle_virtual.daily_limit == 1000
    assert config.eagle_virtual.quota_warning_ratio == 0.8
    assert config.eagle_virtual.max_remote_counterparty_lookups == 0


def test_files_live_in_amlcheck_home(isolated: Path) -> None:
    assert config_path() == isolated / "config.toml"
    assert db_path() == isolated / "amlcheck.db"


def test_config_file_changes_only_what_it_sets(isolated: Path) -> None:
    isolated.mkdir()
    (isolated / "config.toml").write_text("[freshness]\nsanctions_max_age_hours = 24\n")
    config = load_config()
    assert config.freshness.sanctions_max_age_hours == 24
    assert config.freshness.tron_index_max_lag_minutes == 60


def test_amlcheck_config_points_at_another_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "custom.toml"
    custom.write_text("[cache]\ntarget_ttl_seconds = 60\n")
    monkeypatch.setenv("AMLCHECK_CONFIG", str(custom))
    assert load_config().cache.target_ttl_seconds == 60


def test_misspelt_setting_is_an_error_not_a_silent_default(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[freshness]\nsanction_max_age_hours = 24\n")
    with pytest.raises(ValidationError, match="sanction_max_age_hours"):
        load_config(path)


@pytest.mark.parametrize(
    "toml",
    [
        "[freshness]\nsanctions_max_age_hours = 0\n",
        "[cache]\ntarget_ttl_seconds = -1\n",
        "[eagle_virtual]\nquota_warning_ratio = 1.5\n",
        "[eagle_virtual]\nrequests_per_second = 0\n",
    ],
)
def test_out_of_range_values_are_rejected(tmp_path: Path, toml: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(toml)
    with pytest.raises(ValidationError):
        load_config(path)


def test_hash_is_stable_and_changes_with_any_setting() -> None:
    assert Config().hash() == Config().hash()
    changed = Config.model_validate({"cache": {"target_ttl_seconds": 60}})
    assert changed.hash() != Config().hash()


def test_example_config_is_valid_and_shows_the_defaults() -> None:
    assert load_config(REPO / "config.example.toml") == Config()


def test_example_env_lists_exactly_the_keys_amlcheck_reads() -> None:
    lines = (REPO / ".env.example").read_text().splitlines()
    names = {line.split("=")[0] for line in lines if "=" in line and not line.startswith("#")}
    assert names == {field.upper() for field in Secrets.model_fields}


def test_no_keys_by_default() -> None:
    secrets = load_secrets()
    assert secrets.eagle_virtual_api_key is None
    assert secrets.trongrid_api_key is None


def test_key_from_the_project_env_file() -> None:
    Path(".env").write_text("EAGLE_VIRTUAL_API_KEY=ev_live_project\n")
    assert value(load_secrets().eagle_virtual_api_key) == "ev_live_project"


def test_environment_beats_project_env_file_beats_home_env_file(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isolated.mkdir()
    (isolated / ".env").write_text("EAGLE_VIRTUAL_API_KEY=home\nTRONGRID_API_KEY=home\n")
    Path(".env").write_text("TRONGRID_API_KEY=project\n")
    monkeypatch.setenv("EAGLE_VIRTUAL_API_KEY", "environment")
    secrets = load_secrets()
    assert value(secrets.eagle_virtual_api_key) == "environment"
    assert value(secrets.trongrid_api_key) == "project"


def test_key_left_empty_counts_as_missing() -> None:
    Path(".env").write_text("EAGLE_VIRTUAL_API_KEY=\n")
    assert load_secrets().eagle_virtual_api_key is None


def test_keys_never_appear_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRONGRID_API_KEY", "top-secret")
    assert "top-secret" not in repr(load_secrets())


def test_broken_toml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[freshness\n")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(path)


def test_several_eagle_virtual_keys_in_order_each_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """Q19: a paid plan's keys, separated by commas."""
    monkeypatch.setenv("EAGLE_VIRTUAL_API_KEY", " ev_live_a, ev_live_b,,ev_live_a ,ev_live_c ")
    keys = load_secrets().eagle_virtual_keys()
    assert [value(k) for k in keys] == ["ev_live_a", "ev_live_b", "ev_live_c"]
    assert "ev_live_a" not in repr(keys)
    monkeypatch.setenv("EAGLE_VIRTUAL_API_KEY", "ev_live_only")
    assert [value(k) for k in load_secrets().eagle_virtual_keys()] == ["ev_live_only"]
    monkeypatch.delenv("EAGLE_VIRTUAL_API_KEY")
    assert load_secrets().eagle_virtual_keys() == ()
