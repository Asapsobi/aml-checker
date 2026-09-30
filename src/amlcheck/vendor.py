"""A commercial attribution vendor (PRD Phase 4, G5): who is behind an address, from a paid service
such as Chainalysis, TRM, Elliptic or Crystal.

None is set up: the owner chose free sources for now (Q14). This is the plug-in point, so one can be
added without changing amlcheck. Write a class with a `name` and an async `attribute(address)`, and
name it in config.toml:

    [vendor]
    adapter = "my_vendor:MyVendor"   # an importable module, and the class in it
    min_amount_usdt = 50000          # also ask for checks of this amount or more (PRD Q3)

The PRD's cost rule applies: the vendor is asked only when a check comes out REVIEW, or its amount
is at least `min_amount_usdt`. Its answer is attribution, shown and kept in the audit log as a
source. It adds no finding, so it never changes the verdict, and a vendor that fails leaves the
verdict as it was.
"""

import importlib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from amlcheck.adapters.base import SourceAdapter
from amlcheck.config import Config
from amlcheck.core.clock import utcnow
from amlcheck.core.models import Address, SourceHealth, SourceResult, SourceStatus, Verdict

SOURCE = "vendor"


@dataclass(frozen=True)
class Attribution:
    entity: str | None = None  # who: "Binance", "Tornado Cash"
    category: str | None = None  # what kind: "exchange", "mixer"
    risk: str | None = None  # the vendor's own rating, as it gives it
    detail: dict[str, Any] = field(default_factory=dict)  # anything else, kept as evidence


class Vendor(Protocol):
    name: str

    async def attribute(self, address: Address) -> Attribution | None:
        """What the vendor knows of the address; None when it knows nothing."""
        ...


class VendorError(Exception):
    pass


def label(name: str | None) -> str:
    """The vendor source's label. Its evidence keeps the vendor's name, so a stored check can be
    labelled as it was shown."""
    return f"Vendor ({name})" if name else "Vendor"


def load(spec: str) -> Vendor | None:
    """The vendor that `[vendor] adapter` names as "module:Class", or None when it names none."""
    if not spec:
        return None
    module, _, name = spec.partition(":")
    try:
        found = getattr(importlib.import_module(module), name)
        vendor: Vendor = found()
    except (ImportError, AttributeError, TypeError, ValueError) as e:
        raise VendorError(f"[vendor] adapter {spec!r} could not be loaded: {e}") from None
    return vendor


class VendorSource:
    """The vendor as a source of a check: not required, so it never makes a result INCOMPLETE."""

    source = SOURCE
    required = False

    def __init__(self, vendor: Vendor) -> None:
        self._vendor = vendor
        self.label = label(vendor.name)

    async def check(self, address: Address) -> SourceResult:
        named = {"vendor": self._vendor.name}
        try:
            found = await self._vendor.attribute(address)
        except Exception as e:  # anything a vendor's code raises is its failure, not the check's
            summary = f"the vendor failed ({type(e).__name__}: {e}); the verdict stands without it"
            return SourceResult(
                SOURCE, self.label, False, SourceStatus.error, summary, evidence_meta=named
            )
        if found is None:
            return SourceResult(
                SOURCE,
                self.label,
                False,
                SourceStatus.ok,
                "nothing known",
                as_of=utcnow(),
                evidence_meta=named,
            )
        parts = [found.entity or "an unnamed entity"]
        if found.category:
            parts.append(f"({found.category})")
        if found.risk:
            parts.append(f"risk {found.risk}")
        return SourceResult(
            SOURCE,
            self.label,
            False,
            SourceStatus.ok,
            " ".join(parts),
            as_of=utcnow(),
            evidence_meta=named | asdict(found),
        )

    async def health(self) -> SourceHealth:
        return SourceHealth(SOURCE, self.label, SourceStatus.ok, "set up in [vendor] adapter")


def stage(
    config: Config, amount: str | None
) -> Callable[[Verdict], Sequence[SourceAdapter]] | None:
    """For engine.screen's `then`: the vendor, for a REVIEW or a large amount; None without one."""
    vendor = load(config.vendor.adapter)
    if vendor is None:
        return None
    least = Decimal(str(config.vendor.min_amount_usdt))
    large = amount is not None and least > 0 and Decimal(amount) >= least
    source = VendorSource(vendor)

    def after(verdict: Verdict) -> Sequence[SourceAdapter]:
        return [source] if verdict is Verdict.REVIEW or large else []

    return after
