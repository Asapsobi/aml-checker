"""What every source implements (PRD §7, "Adapter interface")."""

from typing import Protocol

from amlcheck.core.models import Address, SourceHealth, SourceResult, SourceStatus


class SourceAdapter(Protocol):
    source: str
    label: str
    required: bool

    async def check(self, address: Address) -> SourceResult: ...

    async def health(self) -> SourceHealth: ...


def failed(adapter: SourceAdapter, reason: str) -> SourceResult:
    return SourceResult(adapter.source, adapter.label, adapter.required, SourceStatus.error, reason)


def unhealthy(adapter: SourceAdapter, reason: str) -> SourceHealth:
    return SourceHealth(adapter.source, adapter.label, SourceStatus.error, reason)
