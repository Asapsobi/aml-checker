"""Settings: non-secret options from config.toml, API keys from the environment or .env.

Keys never live in config.toml, so the whole config can be hashed into every audit
record (PRD §9 `config_hash`) without leaking a secret.
"""

import hashlib
import json
import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

HOME_ENV = "AMLCHECK_HOME"
CONFIG_ENV = "AMLCHECK_CONFIG"


def home_dir() -> Path:
    """Where amlcheck keeps its database, config and logs: $AMLCHECK_HOME or ~/.amlcheck."""
    return Path(os.environ.get(HOME_ENV) or Path.home() / ".amlcheck").expanduser()


def config_path() -> Path:
    """config.toml location: $AMLCHECK_CONFIG, else inside the home directory."""
    return Path(os.environ.get(CONFIG_ENV) or home_dir() / "config.toml").expanduser()


def db_path() -> Path:
    return home_dir() / "amlcheck.db"


class _Section(BaseModel):
    # Unknown keys are errors, so a typo in config.toml cannot silently fall back to a default.
    model_config = ConfigDict(extra="forbid", frozen=True)


class Freshness(_Section):
    """PRD §11: an older source is `stale`, which makes the verdict INCOMPLETE.

    The sanctions list's age counts from its last successful download, not from OFAC's publish
    date: OFAC does not publish every day (docs/verification.md, Q3).
    """

    sanctions_max_age_hours: int = Field(default=48, gt=0)
    tron_index_max_lag_minutes: int = Field(default=60, gt=0)


class Cache(_Section):
    """PRD §11: target-address results are reused for this long. Errors are never cached."""

    target_ttl_seconds: int = Field(default=900, ge=0)


class Ofac(_Section):
    sdn_url: str = (
        "https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML"
    )


class EagleVirtual(_Section):
    """Free plan limits, verified 2026-09-28 (docs/verification.md)."""

    base_url: str = "https://eaglevirtual.com"
    requests_per_second: float = Field(default=1.0, gt=0)
    daily_limit: int = Field(default=1000, gt=0)
    quota_warning_ratio: float = Field(default=0.8, gt=0, le=1)
    max_remote_counterparty_lookups: int = Field(default=0, ge=0)


class Tron(_Section):
    trongrid_url: str = "https://api.trongrid.io"
    usdt_contract: str = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"


class Bsc(_Section):
    usdt_contract: str = "0x55d398326f99059fF775485246999027B3197955"
    # Envio HyperSync, for the transfer history (docs/verification.md, V12). The same service also
    # answers at https://56.hypersync.xyz.
    hypersync_url: str = "https://bsc.hypersync.xyz"


class Network(_Section):
    timeout_seconds: float = Field(default=30.0, gt=0)
    # A longer Retry-After is not waited out: the source is reported as failed instead (AT-07).
    max_retry_after_seconds: float = Field(default=10.0, ge=0)


OverridableRule = Literal[
    "R-SAN-01",
    "R-FRZ-01",
    "R-FRZ-02",
    "R-EXP-01",
    "R-EXP-02",
    "R-EXP-03",
    "R-HEU-01",
    "R-HEU-02",
    "R-HEU-03",
    "R-HEU-04",
    "R-HEU-05",
]


class Rules(_Section):
    """Severity overrides by rule ID (PRD §5.2). R-SYS-01 is not among them: a missing or
    outdated source always makes the result INCOMPLETE (PRD §0 rule 4)."""

    severity: dict[OverridableRule, Literal["BLOCK", "REVIEW"]] = Field(default_factory=dict)


class Exposure(_Section):
    """The 1-hop scan of the address's USDT transfers (PRD §5.2, R-EXP-01 and R-EXP-02)."""

    lookback_days: int = Field(default=180, gt=0)
    # More transfers than this in the lookback make the result INCOMPLETE, so a clean result never
    # rests on part of the history (decided 2026-09-28, docs/verification.md).
    max_transfers: int = Field(default=5000, gt=0)
    flagged_inflow_share: float = Field(default=0.05, gt=0, le=1)


class TwoHop(_Section):
    """2-hop exposure (PRD Phase 4, R-EXP-03): whom the address's largest counterparties received
    USDT from. It runs for `amlcheck investigate`, and for a check whose --amount is at least
    `auto_amount_usdt` (Q15; 0 keeps it to `investigate`)."""

    auto_amount_usdt: float = Field(default=10_000, ge=0)
    # R-EXP-03: a counterparty received at least this from one sanctioned or frozen wallet (Q16).
    min_flagged_usdt: float = Field(default=1_000, gt=0)
    counterparties: int = Field(default=20, gt=0)  # the largest ones are read
    # A counterparty with more transfers than this in the lookback is a hub, such as an exchange:
    # 2 hops through it reach almost everyone, so it is listed but not read.
    max_transfers: int = Field(default=1_000, gt=0)
    time_budget_seconds: float = Field(default=120, gt=0)
    parallel_reads: int = Field(default=3, gt=0, le=10)


class Vendor(_Section):
    """A commercial attribution vendor (PRD Phase 4): none by default (Q14). `adapter` names a
    class as "module:Class" (see amlcheck/vendor.py). It is asked when a check comes out REVIEW, or
    when its --amount is at least `min_amount_usdt` (PRD Q3; 0 keeps it to REVIEW)."""

    adapter: str = ""
    min_amount_usdt: float = Field(default=0, ge=0)


class Heuristics(_Section):
    """R-HEU-01 to R-HEU-05 (PRD §5.2, thresholds from Q7). Every heuristic is a REVIEW."""

    new_address_days: int = Field(default=7, gt=0)
    pass_through_share: float = Field(default=0.9, gt=0, le=1)
    pass_through_hours: int = Field(default=24, gt=0)
    fan_in_senders: int = Field(default=50, gt=0)
    fan_in_small_usdt: float = Field(default=100, gt=0)
    fan_in_window_hours: int = Field(default=24, gt=0)
    fan_out_recipients: int = Field(default=50, gt=0)
    fan_out_window_hours: int = Field(default=24, gt=0)
    risky_tags: tuple[str, ...] = ("mixer", "bridge", "high_risk")
    # Counterparties with this tag in labels.csv are left out of R-HEU-02 to R-HEU-04. They never
    # cancel a sanctions or freeze finding (decided 2026-09-28).
    allowlist_tag: str = "allowlist"


class Config(_Section):
    freshness: Freshness = Freshness()
    cache: Cache = Cache()
    network: Network = Network()
    rules: Rules = Rules()
    exposure: Exposure = Exposure()
    heuristics: Heuristics = Heuristics()
    two_hop: TwoHop = TwoHop()
    vendor: Vendor = Vendor()
    ofac: Ofac = Ofac()
    eagle_virtual: EagleVirtual = EagleVirtual()
    tron: Tron = Tron()
    bsc: Bsc = Bsc()

    def hash(self) -> str:
        """sha256 of the canonical JSON form: the same settings always give the same hash."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def load_config(path: Path | None = None) -> Config:
    """Read config.toml. Without the file every setting keeps its PRD default."""
    path = path or config_path()
    if not path.is_file():
        return Config()
    with path.open("rb") as f:
        return Config.model_validate(tomllib.load(f))


class Secrets(BaseSettings):
    """API keys. Real environment variables win over ./.env, which wins over ~/.amlcheck/.env."""

    model_config = SettingsConfigDict(extra="ignore", env_ignore_empty=True)

    eagle_virtual_api_key: SecretStr | None = None
    trongrid_api_key: SecretStr | None = None
    hypersync_api_token: SecretStr | None = None


def load_secrets() -> Secrets:
    return Secrets(_env_file=(home_dir() / ".env", Path(".env")))
