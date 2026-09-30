"""The local web page (PRD Phase 3): a check form, the history of checks, and each check's details
with explorer links. FastAPI, Jinja2 and HTMX, served on 127.0.0.1 only.

Any web page the operator visits can send requests to 127.0.0.1, so:

- Only the host names 127.0.0.1 and localhost are served, which stops DNS rebinding.
- Every form carries a token made when the server starts. A POST without it is refused, so another
  site cannot run checks through the operator's browser.
- A strict Content-Security-Policy lets scripts and styles come only from this server; HTMX is
  shipped with amlcheck (2.0.10, checked against the npm registry's hash).
- Jinja2 escapes everything shown, and explorer links are built only from values that match an
  address or transaction format.
"""

import asyncio
import hmac
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from markupsafe import Markup
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from amlcheck import __version__, adapters, graph, vendor
from amlcheck.adapters import exposure, tron
from amlcheck.config import Config, Secrets
from amlcheck.core import audit, engine
from amlcheck.core.address import AddressError, parse
from amlcheck.core.clock import from_iso, iso, utcnow
from amlcheck.core.models import Address, Chain, CheckResult, Verdict
from amlcheck.explorer import explorer
from amlcheck.export import credits, source_label
from amlcheck.inputs import amount_hint, client_name
from amlcheck.net import RateLimiter, new_client
from amlcheck.output import MEANING, attributions
from amlcheck.storage import db

HOST = "127.0.0.1"
HERE = Path(__file__).parent
HISTORY_LIMIT = 200
HISTORY_COLUMNS = ("created", "verdict", "chain", "address", "check_id", "client", "amount", "note")
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self';"
    " form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}
# Findings whose evidence is on the checked address's own chain, so its hashes can be linked.
ON_CHAIN = {exposure.SOURCE, tron.SOURCE}


@dataclass
class Node:
    """One piece of evidence, for the template to draw as a tree."""

    key: str | None
    text: str = ""
    href: str | None = None
    children: list["Node"] = field(default_factory=list)


def evidence_tree(value: Any, chain: str | None, key: str | None = None) -> Node:
    """Evidence as a tree. With a chain, values that are its addresses or hashes become links."""
    if isinstance(value, dict):
        return Node(key, children=[evidence_tree(v, chain, str(k)) for k, v in value.items()])
    if isinstance(value, list):
        return Node(key, children=[evidence_tree(v, chain) for v in value])
    text = "" if value is None else str(value)
    return Node(key, text, explorer(chain, text) if chain else None)


@dataclass(frozen=True)
class Shown:
    """A stored check as the pages show it."""

    record: audit.Stored
    created: datetime
    findings: list[dict[str, Any]]
    sources: list[dict[str, Any]]


def shown(record: audit.Stored) -> Shown:
    chain = record.check["chain"]
    rank = {"BLOCK": 0, "INCOMPLETE": 1, "REVIEW": 2}
    findings = [
        {
            **f,
            "priority": json.loads(f["evidence_json"]).get("priority"),
            "tree": evidence_tree(
                json.loads(f["evidence_json"]), chain if f["source"] in ON_CHAIN else None
            ),
        }
        for f in sorted(record.findings, key=lambda f: rank.get(f["severity"], 9))
    ]
    sources = []
    for s in record.sources:
        meta = json.loads(s["evidence_meta_json"] or "{}")
        url = meta.get("url") if isinstance(meta, dict) else None
        sources.append(
            {
                **s,
                "label": source_label(s["source"], meta),
                "as_of": from_iso(s["as_of"]) if s["as_of"] else None,
                "url": url if isinstance(url, str) and url.startswith("https://") else None,
            }
        )
    return Shown(record, from_iso(record.check["created_at"]), findings, sources)


def stored_network(record: audit.Stored) -> dict[str, Any] | None:
    """The 2-hop network a stored check walked, or None when it walked none."""
    for source in record.sources:
        if source["source"] == graph.TWO_HOP:
            network = json.loads(source["evidence_meta_json"] or "{}").get("graph")
            return network if isinstance(network, dict) else None
    return None


def local(moment: datetime | None) -> str:
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %z") if moment else "-"


def create_app(
    *, config: Config, secrets_: Secrets, database: Path, token: str | None = None
) -> FastAPI:
    vendor.load(config.vendor.adapter)  # a wrong `[vendor] adapter` stops the page from starting
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[HOST, "localhost"])
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        local=local, explorer=explorer, version=__version__, meaning=MEANING
    )
    form_token = token or secrets.token_urlsafe(32)
    one_check = asyncio.Lock()  # checks run one at a time, as in a batch
    eagle = RateLimiter(config.eagle_virtual.requests_per_second)

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        return response

    def page(request: Request, name: str, **context: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"token": form_token, **context})

    @app.get("/", response_class=HTMLResponse)
    async def check_page(request: Request) -> HTMLResponse:
        return page(request, "check.html", form={}, result=None, error=None)

    @app.post("/check", response_class=HTMLResponse)
    async def run_check(
        request: Request,
        token: str = Form(""),
        address: str = Form(""),
        chain: str = Form(""),
        amount: str = Form(""),
        client: str = Form(""),
        note: str = Form(""),
        two_hop: str = Form(""),
    ) -> HTMLResponse:
        if not hmac.compare_digest(token, form_token):
            raise HTTPException(403, "This form has expired. Reload the page and try again.")
        form = {
            "address": address,
            "chain": chain,
            "amount": amount,
            "client": client,
            "note": note,
            "two_hop": two_hop,
        }
        fragment = request.headers.get("HX-Request") == "true"
        name = "_result.html" if fragment else "check.html"
        try:
            parsed = parse(address.strip(), Chain(chain) if chain else None)
            amount_value = amount_hint(amount) if amount.strip() else None
            client_value = client_name(client)
        except (AddressError, ValueError) as e:
            return page(request, name, form=form, result=None, error=str(e))
        walk = bool(two_hop) or adapters.wants_two_hop(config, amount_value)
        async with one_check:
            with closing(db.connect(database)) as conn:
                result = await _screen(
                    conn, config, secrets_, eagle, parsed, amount_value, note, client_value, walk
                )
        return page(request, name, form={}, result=result, error=None, credits=attributions(result))

    @app.get("/history", response_class=HTMLResponse)
    async def history(
        request: Request,
        start: str = "",
        end: str = "",
        address: str = "",
        verdict: str = "",
        client: str = "",
    ) -> HTMLResponse:
        filters: dict[str, Any] = {
            "start": None,
            "end": None,
            "address": None,
            "verdict": verdict if verdict in Verdict.__members__ else None,
            "client": client.strip() or None,
        }
        error = None
        try:
            if start:
                filters["start"] = iso(_day(start))
            if end:
                filters["end"] = iso(_day(end) + timedelta(days=1))
            if address.strip():
                filters["address"] = parse(address.strip()).normalized
        except (AddressError, ValueError) as e:
            error = str(e)
        rows = []
        if error is None:
            with closing(db.connect(database)) as conn:
                rows = conn.execute(
                    "SELECT created_at, verdict, chain, address_norm, check_id, client,"
                    " amount_hint, operator_note FROM checks"
                    " WHERE (:start IS NULL OR created_at >= :start)"
                    " AND (:end IS NULL OR created_at < :end)"
                    " AND (:address IS NULL OR address_norm = :address)"
                    " AND (:verdict IS NULL OR verdict = :verdict)"
                    " AND (:client IS NULL OR client = :client COLLATE NOCASE)"
                    " ORDER BY seq DESC LIMIT :limit",
                    {**filters, "limit": HISTORY_LIMIT},
                ).fetchall()
        asked = {
            "start": start,
            "end": end,
            "address": address,
            "verdict": verdict,
            "client": client,
        }
        checks = [
            dict(zip(HISTORY_COLUMNS, row, strict=True)) | {"created": from_iso(row[0])}
            for row in rows
        ]
        return page(
            request,
            "history.html",
            checks=checks,
            asked=asked,
            error=error,
            limit=HISTORY_LIMIT,
            verdicts=[v.value for v in Verdict],
        )

    @app.get("/checks/{check_id}", response_class=HTMLResponse)
    async def detail(request: Request, check_id: str) -> HTMLResponse:
        with closing(db.connect(database)) as conn:
            found = next(audit.records(conn, check_id=check_id), None)
        if found is None:
            raise HTTPException(404, "No check with that ID.")
        network = stored_network(found)
        picture = (
            Markup(graph.to_svg(network, found.check["chain"]))  # noqa: S704 - drawn by amlcheck, all text escaped
            if network
            else None
        )
        return page(
            request,
            "detail.html",
            check=shown(found),
            credits=credits([found]),
            network=picture,
            walk=graph.walk_rows(network) if network else [],
        )

    return app


def _day(text: str) -> datetime:
    """Local midnight at the start of a YYYY-MM-DD day."""
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()  # noqa: DTZ007 - only the date is used
    except ValueError:
        raise ValueError(f"{text!r} is not a date in the form YYYY-MM-DD") from None
    return datetime.combine(day, datetime.min.time(), tzinfo=utcnow().astimezone().tzinfo)


async def _screen(
    conn: sqlite3.Connection,
    config: Config,
    secrets_: Secrets,
    eagle: RateLimiter,
    address: Address,
    amount: str | None,
    note: str,
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
            note=note.strip() or None,
            client=client,
            then=vendor.stage(config, amount),
        )


def serve(config: Config, secrets_: Secrets, database: Path, port: int) -> None:
    """Serve the page on 127.0.0.1 until Ctrl+C."""
    app = create_app(config=config, secrets_=secrets_, database=database)
    uvicorn.run(app, host=HOST, port=port, log_level="warning")
