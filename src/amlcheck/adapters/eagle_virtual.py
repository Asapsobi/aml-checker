"""Eagle Virtual: stablecoin issuers' freezes, seizures and releases of one address (V4).

One call answers for the whole address family: a 0x address is checked on every EVM chain, and a
freeze on any of them counts for a BSC check (docs/verification.md, Q2). The full record, with the
transactions that prove a freeze, costs a second call and is only fetched when there is something
to prove. Only answers that vouch for every chain are cached: a gap is always asked again.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import SecretStr

from amlcheck.adapters.exposure import LookupFailed
from amlcheck.config import EagleVirtual
from amlcheck.core import rules
from amlcheck.core.clock import from_timestamp
from amlcheck.core.models import Address, SourceHealth, SourceResult, SourceStatus
from amlcheck.net import RateLimiter, Sleep, request, retry_after
from amlcheck.storage.cache import ResponseCache

SOURCE = "eagle_virtual"
LABEL = "Eagle Virtual"
EVIDENCE_RECORDS = 5
REFUSALS = {
    400: "Eagle Virtual cannot check this address",
    401: "Eagle Virtual refused the API key: check EAGLE_VIRTUAL_API_KEY",
    403: "Eagle Virtual does not know this key's plan",
    429: "Eagle Virtual's rate limit was reached",
    503: "Eagle Virtual's record is unavailable right now",
}
RESTRICTED = {"FROZEN": rules.FRZ_01, "SEIZED": rules.FRZ_01, "UNFROZEN": rules.FRZ_02}


class _Refused(Exception):
    pass


@dataclass(frozen=True)
class _Answer:
    body: dict[str, Any]
    credit_line: str | None
    cached: bool


def _explain(response: httpx.Response) -> str:
    try:
        code = response.json().get("error")
    except ValueError:
        code = None
    reason = REFUSALS.get(
        response.status_code, f"Eagle Virtual answered HTTP {response.status_code}"
    )
    wait = retry_after(response) if response.status_code == 429 else None
    if wait is not None:
        reason += f"; it asks to retry in {wait:.0f} s"
    return f"{reason} ({code})" if code else reason


def _record(r: dict[str, Any]) -> dict[str, Any]:
    token = r.get("token") or {}
    event = r.get("event") or {}
    amount = r.get("amount") or {}
    return {
        "chain": r.get("chain_name"),
        "token": token.get("symbol"),
        "company": r.get("company"),
        "event": event.get("kind"),
        "is_release": event.get("is_release"),
        "is_seizure": event.get("is_seizure"),
        "tx_hash": r.get("tx_hash"),
        "block": r.get("block_number"),
        "observed": r.get("observed"),
        "amount": f"{amount['value']} {amount['unit']}" if amount.get("value") else None,
    }


def _describe(verdict: str, record_count: int, records: list[dict[str, Any]]) -> str:
    if not records:
        return f"{verdict} according to Eagle Virtual ({record_count} records)"
    r = records[0]
    newest = (
        f"{r['event']} of {r['token']} by {r['company']} on {r['chain']}"
        f" (tx {r['tx_hash']}, {str(r['observed'])[:10]})"
    )
    if record_count <= 1:
        return f"{verdict}: {newest}"
    return f"{verdict}: {record_count} records, the newest a {newest}"


class EagleVirtualAdapter:
    source = SOURCE
    label = LABEL
    required = True

    def __init__(
        self,
        http: httpx.AsyncClient,
        key: SecretStr | None,
        settings: EagleVirtual,
        cache: ResponseCache,
        *,
        max_retry_after: float,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._key = key
        self._settings = settings
        self._cache = cache
        self._limiter = RateLimiter(settings.requests_per_second, sleep=sleep)
        self._max_retry_after = max_retry_after
        self._sleep = sleep

    async def _get(self, path: str, key: SecretStr) -> httpx.Response:
        return await request(
            self._http,
            "GET",
            self._settings.base_url + path,
            headers={"Authorization": f"Bearer {key.get_secret_value()}"},
            limiter=self._limiter,
            max_retry_after=self._max_retry_after,
            sleep=self._sleep,
        )

    async def _answer(
        self, path: str, key: SecretStr, cacheable: Callable[[dict[str, Any]], bool]
    ) -> _Answer:
        cache_key = f"{SOURCE}:{path}"
        hit = self._cache.get(cache_key)
        if hit is not None:
            return _Answer(hit["body"], hit["credit_line"], cached=True)
        response = await self._get(path, key)
        if response.status_code != 200:
            raise _Refused(_explain(response))
        body = response.json()
        credit_line = response.headers.get("x-ev-credit-line")
        if cacheable(body):
            self._cache.put(cache_key, SOURCE, {"body": body, "credit_line": credit_line})
        return _Answer(body, credit_line, cached=False)

    def _result(self, status: SourceStatus, summary: str, **kwargs: Any) -> SourceResult:
        return SourceResult(SOURCE, LABEL, self.required, status, summary, **kwargs)

    async def check(self, address: Address) -> SourceResult:
        if self._key is None:
            return self._result(
                SourceStatus.error, "EAGLE_VIRTUAL_API_KEY is not set (see .env.example)"
            )
        try:
            answer = await self._answer(
                f"/v1/check/{address.normalized}",
                self._key,
                cacheable=lambda body: body.get("verdict") is not None,
            )
        except _Refused as e:
            return self._result(SourceStatus.error, str(e))
        except httpx.HTTPError as e:
            return self._result(SourceStatus.error, f"could not reach Eagle Virtual: {e}")

        body = answer.body
        verdict = body.get("verdict")
        coverage = body.get("coverage") or {}
        as_of = from_timestamp(body["as_of"])
        common: dict[str, Any] = {
            "as_of": as_of,
            "attribution": answer.credit_line,
            "evidence_meta": {
                "verdict": verdict,
                "verdict_reason": body.get("verdict_reason"),
                "record_count": body.get("record_count"),
                "chains_claimed": coverage.get("chains_claimed"),
                "chains_vouched": coverage.get("chains_vouched"),
                "not_vouched_for": coverage.get("not_vouched_for") or [],
                "url": body.get("url"),
                "cached": answer.cached,
            },
        }
        if verdict is None:
            gaps = ", ".join(
                f"{chain.get('name') or chain.get('chain_id')} ({chain.get('reason')})"
                for chain in coverage.get("not_vouched_for") or []
            )
            reason = gaps or str(body.get("verdict_reason"))
            return self._result(
                SourceStatus.stale, f"cannot vouch for every chain right now: {reason}", **common
            )
        if verdict == "CLEAR":
            return self._result(SourceStatus.ok, "CLEAR", **common)
        rule = RESTRICTED.get(verdict)
        if rule is None:
            return self._result(
                SourceStatus.error, f"unknown verdict {verdict!r} from Eagle Virtual"
            )

        records, note = await self._records(address, self._key)
        record_count = int(body.get("record_count") or len(records))
        evidence: dict[str, Any] = {
            "verdict": verdict,
            "record_count": record_count,
            "records": records,
            "url": body.get("url"),
        }
        if note:
            evidence["records_note"] = note
        found = rules.finding(
            rule, SOURCE, _describe(verdict, record_count, records), evidence, as_of
        )
        return self._result(SourceStatus.ok, verdict, findings=(found,), **common)

    async def verdict(self, address: str) -> str | None:
        """The verdict for a counterparty (PRD §11 remote lookups), cached like any answer."""
        if self._key is None:
            raise LookupFailed("EAGLE_VIRTUAL_API_KEY is not set")
        try:
            answer = await self._answer(
                f"/v1/check/{address}",
                self._key,
                cacheable=lambda body: body.get("verdict") is not None,
            )
        except (_Refused, httpx.HTTPError) as e:
            raise LookupFailed(str(e)) from e
        verdict = answer.body.get("verdict")
        return str(verdict) if verdict is not None else None

    async def _records(
        self, address: Address, key: SecretStr
    ) -> tuple[list[dict[str, Any]], str | None]:
        """The newest records behind a verdict, to name the chain, token and transaction."""
        try:
            answer = await self._answer(
                f"/v1/address/{address.normalized}", key, cacheable=lambda body: True
            )
        except (_Refused, httpx.HTTPError) as e:
            return [], f"the full record could not be read: {e}"
        records = answer.body.get("restriction_records") or []
        newest = sorted(records, key=lambda r: r.get("observed_at") or 0, reverse=True)
        note = None
        if len(records) > EVIDENCE_RECORDS:
            note = f"{len(records)} records; the {EVIDENCE_RECORDS} newest are kept here"
        return [_record(r) for r in newest[:EVIDENCE_RECORDS]], note

    async def health(self) -> SourceHealth:
        if self._key is None:
            return SourceHealth(
                SOURCE, LABEL, SourceStatus.error, "EAGLE_VIRTUAL_API_KEY is not set"
            )
        try:
            response = await self._get("/v1/usage", self._key)
        except httpx.HTTPError as e:
            return SourceHealth(
                SOURCE, LABEL, SourceStatus.error, f"could not reach Eagle Virtual: {e}"
            )
        if response.status_code != 200:
            return SourceHealth(SOURCE, LABEL, SourceStatus.error, _explain(response))
        usage = response.json()
        used = int(usage.get("calls_today") or 0)
        limit = int(usage.get("daily_limit") or self._settings.daily_limit)
        warnings: tuple[str, ...] = ()
        if used >= limit * self._settings.quota_warning_ratio:
            warnings = (f"{used:,} of today's {limit:,} Eagle Virtual calls are used",)
        return SourceHealth(
            SOURCE,
            LABEL,
            SourceStatus.ok,
            f"{usage.get('plan')} plan, {used:,} of {limit:,} calls used today"
            " (resets at midnight UTC)",
            warnings,
        )
