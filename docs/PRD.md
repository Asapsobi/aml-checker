# AML Checker — PRD & Roadmap

> **Working name:** `amlcheck`
> **Owner:** Sobi
> **Status:** Draft v0.1 — 2026-09-28
> **Audience:** AI coding agent (primary), internal team (secondary)

---

## 0. Instructions for the AI coding agent (read first)

| # | Rule |
|---|------|
| 1 | Build **phase by phase** (see §12). Do not start a phase until the previous phase's exit criteria pass. |
| 2 | **Verify every external fact before coding against it.** This includes API URLs, response fields, rate limits, contract addresses, event names and ABIs. Fetch the provider's current docs or OpenAPI spec. Where this PRD says *"verify"*, it means the detail is known to change or wasn't confirmed at time of writing. |
| 3 | **Never invent API fields.** If a doc or spec doesn't show a field, don't use it. Record real responses as test fixtures. |
| 4 | **Never return a clean result over a data gap.** If any required source fails, times out or is behind, the verdict is `INCOMPLETE`, not `NO_HITS`. |
| 5 | Every check is written to the audit log **before** the result is shown. |
| 6 | Keep secrets out of the repo. Use `.env` (git-ignored) or the OS keyring. |
| 7 | When something in this PRD is ambiguous, stop and list the question. Don't guess. |

---

## 1. Summary

A local, laptop-run tool that screens a **TRON (TRC20)** or **BNB Smart Chain (BEP20)** address before USDT is received from it or sent to it. It combines four signals into one verdict with evidence:

1. Government sanctions lists (local copy)
2. Stablecoin issuer freeze/seize history (third-party + local index)
3. On-chain exposure to flagged addresses (1–2 hops)
4. Behavioral heuristics

It supports the USDT BEP20→TRC20 settlement corridor. It is a **decision-support and record-keeping tool**, not a legal determination.

---

## 2. Assumptions

| Assumption | Impact if wrong |
|---|---|
| Primary use is **pre-transaction screening** (fast verdict on one address) | If investigation is primary, pull Phase 4 graph work forward |
| Single user, single laptop, no multi-tenant needs | Service mode (Phase 5) becomes required earlier |
| Chains in scope: TRON, BSC only | Adding a chain = new adapter + tests; architecture supports it |
| Python is acceptable | Stack in §8 changes |
| Eagle Virtual **Free** plan at start (1,000 checks/day, 1 req/s, attribution line required) | Upgrade to Business if volume grows or results are shown to clients |

---

## 3. Goals & non-goals

### Goals
| ID | Goal |
|---|---|
| G1 | Verdict for one address in **< 10 s** (Phase 1 sources), **< 60 s** with 1-hop exposure |
| G2 | Every verdict explains itself: each finding shows its source, a timestamp and evidence (tx hash, list entry, block) |
| G3 | Tamper-evident audit trail of every check, exportable |
| G4 | Works with degraded connectivity. Sanctions data is local, and there's a clear `INCOMPLETE` when a live source is down |
| G5 | Pluggable sources, so a commercial vendor (Chainalysis / TRM / Elliptic / Crystal) can be added later without a rewrite |

### Non-goals (for now)
| Item | Why |
|---|---|
| Entity attribution / clustering ("this is Exchange X") | Needs commercial data. Phase 4 integrates a vendor instead of building it |
| KYC / KYB of people or companies (name screening) | Different problem. Possible later extension |
| Automatic blocking of transactions | The tool advises. A human decides |
| Risk scores presented as legal conclusions | Output is findings plus a verdict band, with evidence |
| Cloud hosting / multi-user | Laptop-first |

---

## 4. Users & use cases

| ID | User | Use case | Phase |
|---|---|---|---|
| U1 | Operator | "Before I send 50k USDT to this TRON address, is it safe to proceed?" → single check | 1 |
| U2 | Operator | Screen a counterparty's **deposit source** address after funds arrive | 1 |
| U3 | Operator | Batch-screen a CSV of addresses (e.g. new OTC client wallets) | 3 |
| U4 | Operator | Re-screen previously approved addresses on a schedule (lists change) | 3 |
| U5 | Compliance / mgmt | Export the audit trail for a date range or a client | 3 |
| U6 | Operator | Investigate a flagged address: see its counterparties and flow graph | 4 |
| U7 | Corridor system | Call the checker programmatically before settlement | 5 |

---

## 5. Verdict model

### 5.1 Verdicts

| Verdict | Meaning | Operator action |
|---|---|---|
| `BLOCK` | Direct hit on a blocking rule | Do not transact. Escalate |
| `REVIEW` | One or more risk findings, none blocking | Manual review before transacting |
| `INCOMPLETE` | A required source failed or is stale. Result can't be trusted | Retry, or treat as REVIEW |
| `NO_HITS` | Nothing found **in the sources checked, as of the timestamps shown** | Proceed per policy. **This is not a clearance** |

**Precedence:** `BLOCK` > `INCOMPLETE` > `REVIEW` > `NO_HITS`
(A sanctions hit still blocks even if another source failed.)

### 5.2 Rules (defaults, all configurable in `config.toml`)

| Rule ID | Condition | Severity |
|---|---|---|
| R-SAN-01 | Address exactly matches a sanctioned digital-currency address | BLOCK |
| R-FRZ-01 | Issuer status for address is currently `FROZEN` or `SEIZED` | BLOCK |
| R-FRZ-02 | Address was frozen in the past and later `UNFROZEN` | REVIEW |
| R-EXP-01 | Direct (1-hop) counterparty is sanctioned or currently frozen | REVIEW (configurable → BLOCK) |
| R-EXP-02 | ≥ X% of USDT inflow value (lookback N days) came from flagged 1-hop sources | REVIEW (default X=5%, N=180) |
| R-EXP-03 | 2-hop exposure to sanctioned/frozen address above threshold | REVIEW (Phase 4) |
| R-HEU-01 | Address first activity < N days ago (default 7) | REVIEW (low) |
| R-HEU-02 | Pass-through: ≥ 90% of inflow leaves within T hours (default 24) | REVIEW |
| R-HEU-03 | Fan-in: > K distinct senders of small amounts in a window | REVIEW |
| R-HEU-04 | Fan-out: > K distinct recipients in a window | REVIEW |
| R-HEU-05 | Interaction with an address in the user-maintained `labels.csv` tagged `mixer`, `bridge`, `high_risk` | REVIEW |
| R-SYS-01 | Any required source errored, timed out, or reported lag | INCOMPLETE |

Each finding records: `rule_id`, `severity`, `source`, `evidence` (tx hash / list entry ID / block), `observed_at`.

---

## 6. Data sources

| Layer | Source | How | Notes |
|---|---|---|---|
| Sanctions | **OFAC SDN list** (official) | Daily download → parse all `Digital Currency Address - *` identifiers → local table | **Verify** current download URL (OFAC Sanctions List Service). Match on address string regardless of the currency label, because a "USDT"-labelled address may be on TRON or an EVM chain. Store the snapshot hash + publish date |
| Sanctions (optional) | Chainalysis free sanctions screening API | Per-address live call, second opinion | **Verify** current signup and terms. Disagreements with the local list → log + REVIEW |
| Sanctions (optional) | OpenSanctions | Aggregated lists | Free for non-commercial use only. **Verify** license before using commercially |
| Issuer freezes | **Eagle Virtual API** `GET /v1/check/{address}` | Per target address | Returns `CLEAR / FROZEN / SEIZED / UNFROZEN` with date and per-chain coverage. Returns `null` with a reason when a chain is behind → must map to `INCOMPLETE`. Free plan: 1 key, 1,000 checks/day, 1 req/s, must display "Data from Eagle Virtual" with a link where data is shown. Bearer key prefix `ev_`. Spec: `GET https://eaglevirtual.com/v1/openapi.json` (public). **Verify** BSC is in `GET /v1/chains`. Business plan needed for event lists and higher volume; resale/bundling needs a written agreement |
| Issuer freezes (local index) | **USDT on TRON contract events** | Index `AddedBlackList` / `RemovedBlackList` / `DestroyedBlackFunds` (**verify** names against the ABI) → local table | Used for **bulk counterparty screening** in exposure checks, so 1-hop lookups don't burn the Eagle Virtual quota. USDT-TRC20 contract: `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` (**verify**) |
| Issuer freezes (BSC) | BEP20 USDT contract | **Investigate first.** BEP20 USDT is Binance-Peg (`0x55d398326f99059fF775485246999027B3197955`, **verify**), not Tether-issued. Determine from the ABI whether it has any blacklist/freeze function. Document the finding; don't assume parity with TRON | |
| Chain data (TRON) | TronGrid API | TRC20 transfer history for address, contract events, `triggerconstantcontract` for `isBlackListed` | API key recommended. **Verify** rate limits |
| Chain data (BSC) | Etherscan V2 multichain API (`chainid=56`) or BscScan | BEP20 token transfer history | **Verify** which endpoint is current |
| Labels | `labels.csv` (user-maintained) | `address,chain,tag,note,source` | Known mixers, bridges, exchange hot wallets, own wallets (allowlist) |
| Attribution | Commercial vendor (Phase 4) | Adapter behind the same interface | Only called for REVIEW results or above an amount threshold, to control cost |

---

## 7. Architecture

```
            ┌──────────────── CLI (Typer) ────────────────┐   ┌─ Local Web UI (Phase 3) ─┐
            │ amlcheck check / batch / sync / audit / ... │   │ FastAPI + HTMX, 127.0.0.1 │
            └──────────────────────┬──────────────────────┘   └────────────┬──────────────┘
                                   ▼                                        ▼
                         ┌──────────────────── Core Engine ────────────────────┐
                         │ 1. Validate & normalize address (detect chain)      │
                         │ 2. Run source adapters concurrently (asyncio)       │
                         │ 3. Exposure walker (1-hop; 2-hop in Phase 4)        │
                         │ 4. Rules engine → findings → verdict                │
                         │ 5. Write audit record (hash-chained) → return       │
                         └───────┬────────────┬─────────────┬───────────┬──────┘
                                 ▼            ▼             ▼           ▼
                          SanctionsAdapter  FreezeAdapter  ChainAdapter  VendorAdapter
                          (local SQLite)    (Eagle Virtual (TronGrid,    (Phase 4)
                                            + local TRON   Etherscan V2)
                                            index)
                                 └────────────┴──────┬──────┴───────────┘
                                                     ▼
                                          SQLite (local, single file)
                                          + HTTP response cache (TTL)
```

### Adapter interface (all sources implement this)

```python
class SourceAdapter(Protocol):
    name: str
    required: bool                      # failure → INCOMPLETE if True
    async def check(self, addr: Address) -> SourceResult: ...
    async def health(self) -> SourceHealth: ...   # freshness, lag, quota left

@dataclass
class SourceResult:
    source: str
    status: Literal["ok", "error", "stale", "skipped"]
    findings: list[Finding]
    as_of: datetime                     # list snapshot time or chain block time
    evidence_meta: dict                 # snapshot hash, block height, http status
    raw_ref: str | None                 # pointer to cached raw response
```

---

## 8. Tech stack

| Concern | Choice |
|---|---|
| Language | Python 3.12+ |
| Packaging | `uv`, `pyproject.toml` |
| CLI | Typer + Rich (tables, colored verdicts) |
| HTTP | `httpx` (async) + `tenacity` retries + per-source rate limiter |
| Models | Pydantic v2 |
| Storage | SQLite (WAL mode). One file at `~/.amlcheck/amlcheck.db` |
| Web UI (Ph 3) | FastAPI + Jinja2 + HTMX, bound to `127.0.0.1` only |
| Base58 / checksums | `base58`, `eth-utils` (EIP-55) |
| Tests | `pytest`, `pytest-asyncio`, `respx` (HTTP mocks), recorded fixtures |
| Lint / types | `ruff`, `mypy --strict` on `core/` |
| Secrets | `.env` via `pydantic-settings`, optional OS keyring |

---

## 9. Data model (SQLite)

| Table | Key columns | Purpose |
|---|---|---|
| `list_snapshots` | id, source, fetched_at, published_at, sha256, entry_count | Which sanctions snapshot a check used |
| `sanctioned_addresses` | address_norm, chain_hint, currency_label, list_entry_id, program, snapshot_id | Local sanctions index |
| `issuer_events` | chain, token_contract, address_norm, event_type, tx_hash, block, block_time | Local TRON USDT blacklist index |
| `index_state` | source, last_block, updated_at | Resume point for event indexing |
| `labels` | address_norm, chain, tag, note, source | Loaded from `labels.csv` |
| `http_cache` | key, source, response_json, fetched_at, ttl_s | Avoid re-hitting APIs within TTL |
| `checks` | check_id (UUID), created_at, address_norm, chain, verdict, amount_hint, operator_note, tool_version, config_hash, prev_hash, record_hash | Audit log (append-only) |
| `check_sources` | check_id, source, status, as_of, evidence_meta_json | Source state per check |
| `check_findings` | check_id, rule_id, severity, source, evidence_json | Findings per check |
| `watchlist` | address_norm, chain, added_at, last_checked_at, last_verdict | Re-screening (Phase 3) |

**Normalization:** EVM → lowercase hex (validate EIP-55 if input is mixed case). TRON → base58check-validated `T…`. Optionally also store the hex form.

**Audit integrity:** `record_hash = sha256(prev_hash + canonical_json(check + sources + findings))`. `amlcheck audit verify` recomputes the chain and reports any break. There's no update/delete path in the code.

---

## 10. Interfaces

### 10.1 CLI (Phase 1–3)

| Command | Description |
|---|---|
| `amlcheck check <address> [--chain tron\|bsc] [--amount 50000] [--note "..."] [--json]` | Single check. Auto-detects chain from format |
| `amlcheck batch <file.csv> [--out results.csv]` | Batch check (Phase 3) |
| `amlcheck sync [sanctions\|tron-index\|all]` | Refresh local data |
| `amlcheck status` | Source health: snapshot age, index lag, API quota left |
| `amlcheck audit list [--from --to --address --verdict]` | Browse history |
| `amlcheck audit export --from --to --format csv\|json\|pdf` | Export (PDF in Phase 3) |
| `amlcheck audit verify` | Verify hash chain |
| `amlcheck watch add\|remove\|list\|run` | Watchlist re-screening (Phase 3) |
| `amlcheck labels import labels.csv` | Load user labels |

### 10.2 Human output (example)

```
Address   TXyz…abc  (TRON)          Check 7f3c…  2026-09-28 13:05 +0330
VERDICT   REVIEW

Source            Status  As of                 Result
OFAC SDN          ok      snapshot 2026-09-27   no match
Eagle Virtual     ok      2026-09-28 09:31 UTC  CLEAR
TRON USDT index   ok      block 71,234,567      address not blacklisted
Exposure (1-hop)  ok      last 180 d, 212 txs   1 flagged counterparty

Findings
 REVIEW  R-EXP-01  Received 1,200 USDT from TAbc…  (FROZEN, issuer event tx 9f2e…)
 low     R-HEU-01  First activity 3 days ago

Freeze data from Eagle Virtual — https://eaglevirtual.com/
```

### 10.3 JSON output (`--json`) — stable contract

```json
{
  "check_id": "uuid",
  "created_at": "ISO-8601",
  "address": "string", "chain": "tron|bsc",
  "verdict": "BLOCK|REVIEW|INCOMPLETE|NO_HITS",
  "sources": [{"source": "ofac_sdn", "status": "ok", "as_of": "ISO-8601", "meta": {}}],
  "findings": [{"rule_id": "R-EXP-01", "severity": "REVIEW", "source": "exposure", "evidence": {}}],
  "tool_version": "0.1.0", "config_hash": "sha256",
  "attribution": ["Data from Eagle Virtual"]
}
```

### 10.4 Local HTTP API (Phase 5)
`POST /v1/check` with the same JSON contract, bound to `127.0.0.1`, token-protected.

---

## 11. Non-functional requirements

| Area | Requirement |
|---|---|
| Correctness | No `NO_HITS` if any `required` source is not `ok`. Unit-tested |
| Freshness | Sanctions snapshot > 48 h old → source `stale` → `INCOMPLETE`. TRON index lag > 1 h → `stale` |
| Rate limits | Per-source token bucket. Eagle Virtual Free: ≤ 1 req/s, daily counter with warning at 80%. On HTTP 429, respect `Retry-After` |
| Quota budget | Exposure checks screen counterparties via the **local** index first. Eagle Virtual is used only for the target address, plus up to `max_remote_counterparty_lookups` (default 0 on Free plan) |
| Caching | Target-address results TTL 15 min (configurable). Never cache `error` results |
| Performance | Single check p95 < 10 s without exposure, < 60 s with 1-hop (≤ 1,000 transfers) |
| Privacy | Everything local. No telemetry. Addresses are only sent to the configured providers |
| Security | Secrets never logged. Web UI bound to localhost only |
| Portability | macOS + Linux. Windows best-effort |
| Observability | Structured logs (JSON) at `~/.amlcheck/logs/`, rotated |
| Testing | ≥ 85% coverage on `core/` (rules, verdict precedence, normalization, audit hashing). Every adapter tested against recorded fixtures |

---

## 12. Roadmap

| Phase | Goal | Deliverables | Exit criteria | Est. effort* |
|---|---|---|---|---|
| **0 — Foundations** | Skeleton + verification | Repo, `pyproject`, CLI stub, config loader, SQLite migrations, CI (lint/type/test). **Verification report** (`docs/verification.md`) confirming every "verify" item in §6 with links and dates, including the BSC USDT freeze-capability finding | `amlcheck --help` runs. CI green. Verification report complete | 1–2 days |
| **1 — MVP screening** | Answer "can I transact with this address?" | Address validation/normalization. OFAC sync + parser. Eagle Virtual adapter. TRON USDT event indexer + `isBlackListed` spot-check. Rules R-SAN-01, R-FRZ-01/02, R-SYS-01. Verdict engine. Hash-chained audit log. `check`, `sync`, `status`, `audit list/verify` | Known OFAC-listed address → `BLOCK`. Known frozen TRON address → `BLOCK`. Mocked source outage → `INCOMPLETE`. Audit verify detects a tampered row | ~1 week |
| **2 — Exposure & heuristics** | Catch indirect risk | TRON + BSC transfer fetchers (paginated, cached). 1-hop walker. R-EXP-01/02. R-HEU-01..05. `labels.csv` import. Amount-weighted exposure % | Fixture address with a known frozen sender → `REVIEW` with correct tx evidence. p95 < 60 s on 1,000-transfer fixture | ~1–1.5 weeks |
| **3 — Operator UX & ops** | Daily usability | Local web UI (check form, history, finding detail with explorer links). `batch`. Watchlist + scheduled re-screen (OS scheduler / cron instructions). Audit export CSV/JSON/PDF. Verdict-change alerts on watchlist | Batch of 100 addresses completes within rate limits. Export opens cleanly. Watchlist flags a changed verdict | ~1 week |
| **4 — Investigation & vendor** | Depth + attribution | 2-hop walker with budget limits. Graph view (counterparty network, flagged nodes highlighted). Commercial vendor adapter (Chainalysis/TRM/Elliptic/Crystal — chosen later), called only on `REVIEW` or amount > threshold. R-EXP-03 | Vendor adapter swappable via config. Graph renders a 2-hop view for a fixture | ~2 weeks |
| **5 — Service mode** | Plug into the corridor | Local HTTP API (§10.4). Idempotent check IDs. Optional Eagle Virtual Business key rotation (5 keys). Packaging (pipx / single binary) | Corridor mock calls API and gets the stable JSON contract | ~1 week |

\*Effort assumes an AI coding agent with human review. It's a sizing guide, not a commitment.

### Milestone view

```
Week:  1        2        3        4        5        6        7        8
P0     ██
P1       ██████
P2              █████████
P3                       ██████
P4                              ████████████
P5                                          ██████
       ▲ MVP usable end of wk 2   ▲ full screening wk 4   ▲ corridor-ready wk 8
```

---

## 13. Acceptance test matrix (MVP)

| ID | Input | Expected |
|---|---|---|
| AT-01 | Invalid address string | Error, no audit record, non-zero exit |
| AT-02 | Valid TRON address, all sources clean (mocked) | `NO_HITS`, disclaimer printed, audit record written |
| AT-03 | Address present in OFAC fixture | `BLOCK`, finding R-SAN-01 with list entry ID |
| AT-04 | Eagle Virtual returns `FROZEN` | `BLOCK`, finding R-FRZ-01 with tx hash |
| AT-05 | Eagle Virtual returns `UNFROZEN` | `REVIEW`, R-FRZ-02 |
| AT-06 | Eagle Virtual returns `null` for a chain (lagging) | `INCOMPLETE`, R-SYS-01 with reason |
| AT-07 | Eagle Virtual HTTP 429 | Retry after `Retry-After`. If still failing → `INCOMPLETE` |
| AT-08 | OFAC snapshot older than 48 h | `INCOMPLETE` (stale) unless a BLOCK finding exists |
| AT-09 | OFAC hit **and** Eagle Virtual down | `BLOCK` (precedence) |
| AT-10 | Mixed-case EVM address with bad checksum | Error |
| AT-11 | Tamper one `checks` row in the DB | `audit verify` reports the break at that record |

---

## 14. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Operators read `NO_HITS` as "clean" | Wording and disclaimer on every result. Name is deliberately not "CLEAR" |
| Third-party API changes / shuts down | Adapter pattern, local TRON index as fallback for freeze data, recorded fixtures catch drift |
| Free-tier quota exhausted mid-day | Quota tracking in `status`, warn at 80%, local index first, upgrade path |
| OFAC format/URL changes | Parser tests on the real file, snapshot sanity checks (entry count shouldn't drop > 20%) |
| BEP20 freeze coverage differs from TRON | Documented in Phase 0 verification report. Shown per chain in results |
| Heuristic false positives | All heuristics are `REVIEW` only, thresholds in config, labels allowlist for own/known wallets |
| Data licensing (showing results to B2B clients) | Free-plan attribution displayed. Redistribution needs a written agreement with the provider. Review before Phase 5 |

---

## 15. Open questions

| # | Question | Needed by |
|---|---|---|
| Q1 | Should R-EXP-01 (direct counterparty flagged) be `BLOCK` instead of `REVIEW`? | Phase 2 |
| Q2 | Which commercial attribution vendor, and what monthly budget? | Phase 4 |
| Q3 | Amount threshold above which a vendor check is mandatory? | Phase 4 |
| Q4 | Will screening results ever be shown to corridor clients? (affects licensing and plan) | Phase 5 |
| Q5 | Audit retention period required by the corridor's target jurisdiction? | Phase 3 |

---

## 16. Repo layout (suggested)

```
amlcheck/
├── pyproject.toml
├── README.md
├── config.example.toml
├── .env.example
├── docs/
│   ├── PRD.md                 ← this file
│   └── verification.md        ← Phase 0 output
├── src/amlcheck/
│   ├── cli.py
│   ├── config.py
│   ├── core/
│   │   ├── address.py         (validate / normalize / detect chain)
│   │   ├── engine.py          (orchestration)
│   │   ├── rules.py
│   │   ├── verdict.py
│   │   └── audit.py           (hash chain)
│   ├── adapters/
│   │   ├── base.py
│   │   ├── ofac.py
│   │   ├── eagle_virtual.py
│   │   ├── tron_index.py
│   │   ├── tron_chain.py
│   │   ├── bsc_chain.py
│   │   └── vendor_stub.py
│   ├── exposure/walker.py
│   ├── storage/{db.py, migrations/}
│   └── web/ (Phase 3)
└── tests/
    ├── fixtures/              (recorded API responses, sample OFAC file)
    ├── unit/
    └── integration/
```
