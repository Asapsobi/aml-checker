"""BSC USDT. BEP20 USDT has no freeze or blacklist function (docs/verification.md, V6), so there is
nothing to check at the token level: the source reports `skipped`, which is not a data gap (Q1).
Freezes of the same address by other issuers still come from Eagle Virtual (Q2)."""

from amlcheck.core.models import Address, SourceHealth, SourceResult, SourceStatus

REASON = "not applicable: BEP20 USDT cannot freeze an address"


class BscUsdtAdapter:
    source = "bsc_usdt"
    label = "BSC USDT"
    required = False

    async def check(self, address: Address) -> SourceResult:
        return SourceResult(
            self.source,
            self.label,
            self.required,
            SourceStatus.skipped,
            REASON,
            evidence_meta={"reference": "docs/verification.md, V6 and Q1"},
        )

    async def health(self) -> SourceHealth:
        return SourceHealth(self.source, self.label, SourceStatus.skipped, REASON)
