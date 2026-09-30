"""Eagle Virtual: stablecoin issuers' freezes, seizures and releases of one address (V4).

One call answers for the whole address family: a 0x address is checked on every EVM chain, and a
freeze on any of them counts for a BSC check (docs/verification.md, Q2). The full record, with the
transactions that prove a freeze, costs a second call and is only fetched when there is something
to prove. Only answers that vouch for every chain are cached: a gap is always asked again.

Several keys can be set, for a paid plan: the Business plan has 5, each with its own daily count
(V16). When a key's day is used up, Eagle Virtual answers 429 with a Retry-After that runs to
midnight UTC. That key then rests for as long as it asks, and the next one is used (Q19). Its terms
forbid getting around its limits, and the Free plan counts per account, so only the first key may
be on the Free plan: any other key is used only once /v1/usage, which costs nothing, says its plan
is paid.
"""

import asyncio
import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from amlcheck.adapters.exposure import LookupFailed
from amlcheck.config import EagleVirtual
from amlcheck.core import rules
from amlcheck.core.clock import from_timestamp, utcnow
from amlcheck.core.models import Address, SourceHealth, SourceResult, SourceStatus
from amlcheck.net import RateLimiter, Sleep, request, retry_after
from amlcheck.storage.cache import ResponseCache

SOURCE = "eagle_virtual"
LABEL = "Eagle Virtual"
# The credit line the Free plan sends in x-ev-credit-line (V4). Records written before the line was
# kept with them are credited with this text.
CREDIT_LINE = "Data from Eagle Virtual, https://eaglevirtual.com/license"
EVIDENCE_RECORDS = 5
REFUSALS = {
    400: "Eagle Virtual cannot check this address",
    401: "Eagle Virtual refused the API key: check EAGLE_VIRTUAL_API_KEY",
    403: "Eagle Virtual does not know this key's plan",
    429: "Eagle Virtual's rate limit was reached",
    503: "Eagle Virtual's record is unavailable right now",
}
RESTRICTED = {"FROZEN": rules.FRZ_01, "SEIZED": rules.FRZ_01, "UNFROZEN": rules.FRZ_02}
PAID_PLANS = frozenset({"business", "enterprise"})
# How long a key rests after a 429 that did not say how long to wait.
DEFAULT_REST = timedelta(seconds=60)


class _Refused(Exception):
    pass


def credit_line(meta: dict[str, Any]) -> str | None:
    """The credit line a stored answer owes: the one it came with, none on a paid plan. Records
    written before the line was kept with them are credited with the Free plan's."""
    return meta.get("credit_line") if "credit_line" in meta else CREDIT_LINE


def _fingerprint(key: SecretStr) -> str:
    return hashlib.sha256(key.get_secret_value().encode()).hexdigest()


class KeyRing:
    """What this process has learned about the keys: which ones rest, and until when, and which
    plan each one is on. It outlives one check, so the web page, the API and a batch skip a key
    whose day is used up. Keys are known by their sha256 only."""

    def __init__(self) -> None:
        self._resting: dict[str, datetime] = {}
        self._plans: dict[str, str | None] = {}

    def rests_until(self, key: SecretStr, now: datetime) -> datetime | None:
        until = self._resting.get(_fingerprint(key))
        return until if until is not None and until > now else None

    def rest(self, key: SecretStr, until: datetime) -> None:
        self._resting[_fingerprint(key)] = until

    def plan(self, key: SecretStr) -> str | None:
        return self._plans.get(_fingerprint(key))

    def learn_plan(self, key: SecretStr, plan: str | None) -> None:
        self._plans[_fingerprint(key)] = plan

    def forget(self) -> None:
        self._resting.clear()
        self._plans.clear()


KEYS = KeyRing()


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
        keys: SecretStr | Sequence[SecretStr] | None,
        settings: EagleVirtual,
        cache: ResponseCache,
        *,
        max_retry_after: float,
        sleep: Sleep = asyncio.sleep,
        limiter: RateLimiter | None = None,
        now: Callable[[], datetime] = utcnow,
        ring: KeyRing = KEYS,
    ) -> None:
        self._http = http
        if keys is None:
            keys = ()
        self._keys = (keys,) if isinstance(keys, SecretStr) else tuple(keys)
        self._now = now
        self._ring = ring
        self._settings = settings
        self._cache = cache
        # A batch passes one limiter to every check, so the plan's rate holds across them all.
        self._limiter = limiter or RateLimiter(settings.requests_per_second, sleep=sleep)
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

    async def _unusable(self, key: SecretStr, number: int) -> str | None:
        """Why a key after the first is not used, or None when its plan is paid (see the module's
        docstring). A plan is read once per process."""
        plan = self._ring.plan(key)
        if plan is None:
            try:
                plan = str((await self._usage(key)).get("plan"))
            except _Refused as e:
                return f"the plan of key {number} could not be read ({e})"
        if plan not in PAID_PLANS:
            return f"key {number} is on the {plan} plan, so it is not used"
        return None

    async def _send(self, path: str) -> httpx.Response:
        """GET with the first key that is not resting. With more than one key, a key answering
        429 rests for as long as its Retry-After says, and the next key is asked."""
        if len(self._keys) == 1:
            return await self._get(path, self._keys[0])
        back: list[datetime] = []
        unused: list[str] = []
        for number, key in enumerate(self._keys, 1):
            now = self._now()
            until = self._ring.rests_until(key, now)
            if until is not None:
                back.append(until)
                continue
            why = await self._unusable(key, number) if number > 1 else None
            if why is not None:
                unused.append(why)
                continue
            response = await self._get(path, key)
            if response.status_code != 429:
                return response
            wait = retry_after(response)
            until = now + (timedelta(seconds=wait) if wait is not None else DEFAULT_REST)
            self._ring.rest(key, until)
            back.append(until)
        reason = "every Eagle Virtual key that can be used is over its plan's limit"
        if back:
            reason += f"; the first answers again at {min(back):%Y-%m-%d %H:%M} UTC"
        raise _Refused("; ".join([reason, *unused]))

    async def _answer(self, path: str, cacheable: Callable[[dict[str, Any]], bool]) -> _Answer:
        cache_key = f"{SOURCE}:{path}"
        hit = self._cache.get(cache_key)
        if hit is not None:
            return _Answer(hit["body"], hit["credit_line"], cached=True)
        response = await self._send(path)
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
        if not self._keys:
            return self._result(
                SourceStatus.error, "EAGLE_VIRTUAL_API_KEY is not set (see .env.example)"
            )
        try:
            answer = await self._answer(
                f"/v1/check/{address.normalized}",
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
                # Kept with the record, so an export can credit the data as the licence asks (V4).
                "credit_line": answer.credit_line,
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

        records, note = await self._records(address)
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
        if not self._keys:
            raise LookupFailed("EAGLE_VIRTUAL_API_KEY is not set")
        try:
            answer = await self._answer(
                f"/v1/check/{address}",
                cacheable=lambda body: body.get("verdict") is not None,
            )
        except (_Refused, httpx.HTTPError) as e:
            raise LookupFailed(str(e)) from e
        verdict = answer.body.get("verdict")
        return str(verdict) if verdict is not None else None

    async def _records(self, address: Address) -> tuple[list[dict[str, Any]], str | None]:
        """The newest records behind a verdict, to name the chain, token and transaction."""
        try:
            answer = await self._answer(
                f"/v1/address/{address.normalized}", cacheable=lambda body: True
            )
        except (_Refused, httpx.HTTPError) as e:
            return [], f"the full record could not be read: {e}"
        records = answer.body.get("restriction_records") or []
        newest = sorted(records, key=lambda r: r.get("observed_at") or 0, reverse=True)
        note = None
        if len(records) > EVIDENCE_RECORDS:
            note = f"{len(records)} records; the {EVIDENCE_RECORDS} newest are kept here"
        return [_record(r) for r in newest[:EVIDENCE_RECORDS]], note

    async def _usage(self, key: SecretStr) -> dict[str, Any]:
        """/v1/usage for one key, which costs no call; _Refused when it cannot be read."""
        try:
            response = await self._get("/v1/usage", key)
        except httpx.HTTPError as e:
            raise _Refused(f"could not reach Eagle Virtual: {e}") from e
        if response.status_code != 200:
            raise _Refused(_explain(response))
        usage: dict[str, Any] = response.json()
        self._ring.learn_plan(key, str(usage.get("plan")))
        return usage

    def _counts(self, usage: dict[str, Any]) -> tuple[int, int]:
        used = int(usage.get("calls_today") or 0)
        return used, int(usage.get("daily_limit") or self._settings.daily_limit)

    async def health(self) -> SourceHealth:
        if not self._keys:
            return SourceHealth(
                SOURCE, LABEL, SourceStatus.error, "EAGLE_VIRTUAL_API_KEY is not set"
            )
        if len(self._keys) > 1:
            return await self._ring_health()
        try:
            usage = await self._usage(self._keys[0])
        except _Refused as e:
            return SourceHealth(SOURCE, LABEL, SourceStatus.error, str(e))
        used, limit = self._counts(usage)
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

    async def _ring_health(self) -> SourceHealth:
        """Each key's plan and calls, and which keys a check will not use."""
        used_total = limit_total = usable = 0
        plans: set[str] = set()
        warnings: list[str] = []
        now = self._now()
        for number, key in enumerate(self._keys, 1):
            try:
                usage = await self._usage(key)
            except _Refused as e:
                warnings.append(f"key {number}: {e}")
                continue
            plan = str(usage.get("plan"))
            if number > 1 and plan not in PAID_PLANS:
                warnings.append(
                    f"key {number} is on the {plan} plan, so it is never used: only a paid plan's"
                    " keys take turns"
                )
                continue
            usable += 1
            plans.add(plan)
            used, limit = self._counts(usage)
            used_total += used
            limit_total += limit
            if used >= limit * self._settings.quota_warning_ratio:
                warnings.append(f"key {number}: {used:,} of today's {limit:,} calls are used")
            until = self._ring.rests_until(key, now)
            if until is not None:
                warnings.append(f"key {number} rests until {until:%Y-%m-%d %H:%M} UTC")
        if not usable:
            return SourceHealth(
                SOURCE, LABEL, SourceStatus.error, "no Eagle Virtual key answered", tuple(warnings)
            )
        return SourceHealth(
            SOURCE,
            LABEL,
            SourceStatus.ok,
            f"{usable} of {len(self._keys)} keys in use ({', '.join(sorted(plans))} plan),"
            f" {used_total:,} of {limit_total:,} calls used today (resets at midnight UTC)",
            tuple(warnings),
        )
