"""The local HTTP API (PRD §10.4, Phase 5): `POST /v1/check` answers with the JSON contract of
`amlcheck check --json` (§10.3), so a corridor system can screen an address before it settles.

It listens on 127.0.0.1 only, so its caller runs on the same machine (Q18), and every request needs
the token in AMLCHECK_API_TOKEN as `Authorization: Bearer …`. Only the host names 127.0.0.1 and
localhost are served, which stops DNS rebinding. docs/api.md is the reference.

Idempotency follows the IETF draft "The Idempotency-Key HTTP Header Field" (rev 07, V16). A request
sent again with the same Idempotency-Key gets the check the first one made, rebuilt from the audit
log, with `Idempotent-Replayed: true`: no second check is made or logged. The same key with a
different request is refused with 422, and a repeat while the first is still running with 409.
Keys never expire.

Checks run one at a time, as in the web page and a batch, so the sources' rate limits hold.
"""

import asyncio
import hashlib
import hmac
import logging
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr
from pydantic import ValidationError as ModelError
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from amlcheck import __version__, adapters, vendor
from amlcheck.config import Config, Secrets
from amlcheck.core import audit, engine
from amlcheck.core.address import AddressError, parse
from amlcheck.core.clock import iso, utcnow
from amlcheck.core.models import Address, Chain, CheckResult
from amlcheck.inputs import amount_hint, client_name
from amlcheck.net import RateLimiter, new_client
from amlcheck.output import from_record, to_json
from amlcheck.storage import db

HOST = "127.0.0.1"
PORT = 8766
MIN_TOKEN = 32  # characters in AMLCHECK_API_TOKEN
MAX_BODY = 16 * 1024  # bytes in a request
MAX_NOTE = 1000  # characters in a note
KEY_FORMAT = re.compile(r"[A-Za-z0-9._:-]{8,128}")
CHECK_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
DOCS = "https://github.com/Asapsobi/aml-checker/blob/main/docs/api.md"
PROBLEM = "application/problem+json"

log = logging.getLogger(__name__)


class CheckRequest(BaseModel):
    """The body of POST /v1/check. Only `address` is needed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    address: StrictStr = Field(max_length=200)
    chain: Chain | None = None
    # USDT: a decimal string such as "50000" or "50,000.25", or a JSON number.
    amount: StrictStr | StrictInt | StrictFloat | None = None
    note: StrictStr | None = Field(default=None, max_length=MAX_NOTE)
    client: StrictStr | None = None
    two_hop: StrictBool = False


class Problem(Exception):
    """An error answer in the form of RFC 9457 (application/problem+json)."""

    def __init__(
        self, status: int, title: str, detail: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.title = title
        self.detail = detail
        self.headers = headers or {}


def problem(
    status: int, title: str, detail: str, headers: dict[str, str] | None = None
) -> Response:
    body = {"type": f"{DOCS}#errors", "title": title, "status": status, "detail": detail}
    return JSONResponse(body, status, headers=headers, media_type=PROBLEM)


def bearer(header: str | None) -> str | None:
    """The token of an `Authorization: Bearer <token>` header."""
    scheme, _, token = (header or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def idempotency_key(header: str | None) -> str | None:
    """The key of an Idempotency-Key header. The draft makes it a structured-field String, "…";
    a bare key is taken too. Keys have a fixed format, as the draft's security section asks."""
    if header is None:
        return None
    key = header.strip()
    if len(key) >= 2 and key[0] == key[-1] == '"':
        key = key[1:-1]
    if not KEY_FORMAT.fullmatch(key):
        raise Problem(
            400,
            "Idempotency-Key is malformed",
            "An Idempotency-Key is 8 to 128 letters, digits and . _ : - such as a UUID,"
            ' optionally in double quotes: Idempotency-Key: "8e03978e-40d5-43e8-bc93-6894a57f9324"',
        )
    return key


def fingerprint(address: Address, amount: str | None, ask: CheckRequest, client: str | None) -> str:
    """The request as understood, so that the same key cannot stand for a different request."""
    understood = {
        "address": address.normalized,
        "chain": address.chain.value,
        "amount": amount,
        "note": (ask.note or "").strip() or None,
        "client": client,
        "two_hop": ask.two_hop,
    }
    return hashlib.sha256(audit.canonical_json(understood).encode()).hexdigest()


def _field_errors(error: ModelError) -> str:
    parts = []
    for item in error.errors(include_url=False):
        where = ".".join(str(p) for p in item["loc"]) or "body"
        parts.append(f"{where}: {item['msg']}")
    return "; ".join(parts)


def create_app(*, config: Config, secrets_: Secrets, database: Path, token: str) -> FastAPI:
    if len(token) < MIN_TOKEN:
        raise ValueError(f"the API token must be at least {MIN_TOKEN} characters")
    vendor.load(config.vendor.adapter)  # a wrong `[vendor] adapter` stops the API from starting
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    expected = token.encode()
    one_check = asyncio.Lock()
    eagle = RateLimiter(config.eagle_virtual.requests_per_second)
    running: dict[str, str] = {}  # Idempotency-Key -> fingerprint, while its check runs

    @app.middleware("http")
    async def authorize(request: Request, call_next: Any) -> Response:
        given = bearer(request.headers.get("Authorization"))
        if given is None or not hmac.compare_digest(given.encode(), expected):
            return problem(
                401,
                "Unauthorized",
                "Send the token in AMLCHECK_API_TOKEN as Authorization: Bearer <token>.",
                {"WWW-Authenticate": 'Bearer realm="amlcheck"'},
            )
        response: Response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    # Added last, so it runs first: a request for another host name is refused before the token.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[HOST, "localhost"])

    @app.exception_handler(Problem)
    async def on_problem(request: Request, error: Problem) -> Response:
        return problem(error.status, error.title, error.detail, error.headers)

    @app.exception_handler(HTTPException)
    async def on_http_error(request: Request, error: HTTPException) -> Response:
        headers = dict(error.headers) if error.headers else None
        return problem(error.status_code, str(error.detail), str(error.detail), headers)

    @app.exception_handler(Exception)
    async def on_failure(request: Request, error: Exception) -> Response:
        log.error("api failure", exc_info=error)
        return problem(500, "Internal error", "The check could not be made; see the amlcheck log.")

    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "tool_version": __version__}

    @app.post("/v1/check")
    async def check(request: Request) -> Response:
        started = time.monotonic()
        ask, address, amount, client = await _read_check(request)
        key = idempotency_key(request.headers.get("Idempotency-Key"))
        wanted = fingerprint(address, amount, ask, client)
        if key is not None:
            replay = _replay(database, key, wanted, running)
            if replay is not None:
                log.info("api replay", extra={"fields": {"check_id": replay["check_id"]}})
                return _answer(replay, replayed=True)
            running[key] = wanted
        try:
            async with one_check:
                with closing(db.connect(database)) as conn:
                    walk = ask.two_hop or adapters.wants_two_hop(config, amount)
                    result = await _screen(
                        conn, config, secrets_, eagle, address, amount, ask.note, client, walk
                    )
                    if key is not None:
                        conn.execute(
                            "INSERT INTO api_requests (idempotency_key, fingerprint, check_id,"
                            " created_at) VALUES (?, ?, ?, ?)",
                            (key, wanted, result.check_id, iso(utcnow())),
                        )
                        conn.commit()
        finally:
            if key is not None:
                running.pop(key, None)
        log.info(
            "api check",
            extra={
                "fields": {
                    "check_id": result.check_id,
                    "verdict": result.verdict.value,
                    "seconds": round(time.monotonic() - started, 1),
                }
            },
        )
        return _answer(to_json(result), replayed=False)

    @app.get("/v1/checks/{check_id}")
    async def stored(check_id: str) -> Response:
        found = None
        if CHECK_ID.fullmatch(check_id):
            with closing(db.connect(database)) as conn:
                found = next(audit.records(conn, check_id=check_id), None)
        if found is None:
            raise Problem(404, "Not found", "No check has that ID.")
        return _answer(_verified(found), replayed=False)

    return app


async def _read_check(request: Request) -> tuple[CheckRequest, Address, str | None, str | None]:
    """The request's body, checked and understood, or a 4xx Problem."""
    kind = request.headers.get("Content-Type", "").partition(";")[0].strip().lower()
    if kind != "application/json":
        raise Problem(415, "Unsupported media type", "Send the body as application/json.")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY:
            raise Problem(413, "Request too large", f"A request is at most {MAX_BODY:,} bytes.")
    try:
        ask = CheckRequest.model_validate_json(body)
    except ModelError as e:
        raise Problem(400, "Invalid request", _field_errors(e)) from None
    try:
        address = parse(ask.address.strip(), ask.chain)
    except AddressError as e:
        raise Problem(400, "Invalid address", str(e)) from None
    try:
        amount = None if ask.amount is None else amount_hint(str(ask.amount))
    except ValueError as e:
        raise Problem(400, "Invalid request", f"amount: {e}") from None
    try:
        client = client_name(ask.client)
    except ValueError as e:
        raise Problem(400, "Invalid request", f"client: {e}") from None
    return ask, address, amount, client


def _replay(
    database: Path, key: str, wanted: str, running: dict[str, str]
) -> dict[str, Any] | None:
    """The check an earlier request with this key made; None for a new key. It runs without an
    await, so no other request can take the key between the look and the claim."""
    with closing(db.connect(database)) as conn:
        known = conn.execute(
            "SELECT fingerprint, check_id FROM api_requests WHERE idempotency_key = ?", (key,)
        ).fetchone()
        seen = known[0] if known else running.get(key)
        if seen is not None and seen != wanted:
            raise Problem(
                422,
                "Idempotency-Key is already used",
                "This Idempotency-Key was sent with a different request. Use a new key for a new"
                " request.",
            )
        if known is None and key in running:
            raise Problem(
                409,
                "A request is outstanding for this Idempotency-Key",
                "The first request with this Idempotency-Key is still running. Retry it shortly"
                " to get its result.",
            )
        if known is None:
            return None
        record = next(audit.records(conn, check_id=known[1]), None)
    if record is None:  # api_requests refers to checks, so this is a damaged database
        raise Problem(500, "Check missing", "The check this key names is not in the audit log.")
    return _verified(record)


def _verified(record: audit.Stored) -> dict[str, Any]:
    """A stored check's contract, if its record still hashes to its record_hash."""
    if record.recomputed() != record.record_hash:
        log.error("audit record fails its hash", extra={"fields": {"seq": record.seq}})
        raise Problem(
            500,
            "Audit record does not verify",
            "This check's audit record no longer matches its hash. Run amlcheck audit verify.",
        )
    return from_record(record)


def _answer(body: dict[str, Any], *, replayed: bool) -> Response:
    headers = {"Content-Location": f"/v1/checks/{body['check_id']}"}
    if replayed:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(body, headers=headers)


async def _screen(
    conn: sqlite3.Connection,
    config: Config,
    secrets_: Secrets,
    eagle: RateLimiter,
    address: Address,
    amount: str | None,
    note: str | None,
    client: str | None,
    two_hop: bool,
) -> CheckResult:
    async with new_client(config.network.timeout_seconds) as http:
        sources = adapters.build(
            address.chain,
            conn=conn,
            http=http,
            config=config,
            secrets=secrets_,
            eagle_limiter=eagle,
            two_hop=two_hop,
        )
        return await engine.screen(
            address,
            sources,
            conn=conn,
            config=config,
            amount=amount,
            note=(note or "").strip() or None,
            client=client,
            then=vendor.stage(config, amount),
        )


def serve(config: Config, secrets_: Secrets, database: Path, port: int, token: str) -> None:
    """Serve the API on 127.0.0.1 until stopped."""
    app = create_app(config=config, secrets_=secrets_, database=database, token=token)
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
