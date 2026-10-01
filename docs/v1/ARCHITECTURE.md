# amlcheck v1 — Architecture

> How v1 fits into the 0.5.1 code. New code goes into new packages. Existing modules change only at the
> seams listed in §3. Read with [PRD.md](PRD.md) and [DATA_MODEL.md](DATA_MODEL.md).

---

## 1. Overview

```
 CLI (Typer)        Web (FastAPI+HTMX)        API (/v1/*)          monitor run (cron)
     │                     │                       │                       │
     └─────────────┬───────┴───────────┬───────────┘                       │
                   ▼                   ▼                                   ▼
           core/engine.screen     trace/jobs.TraceQueue  ◄──────── monitor/inbound.py
                   │                   │  (one worker, shared limiters)
   ┌───────────────┼───────────────────┼──────────────────────────────┐
   ▼               ▼                   ▼                              ▼
 Existing     ExposureAdapter      TraceAdapter ──► trace/engine.py   core/score.py
 sources      (1-hop, R-EXP/HEU)   (R-TRC-*)        │                 (after verdict,
 OFAC, EV,         │                                │                  before audit write)
 Tron/BSC USDT     │                                ▼
                   │                     profile/features.py ─► profile/classifier.py
                   │                                │                 │
                   ▼                                ▼                 ▼
          exposure/reader.HistoryReader ◄──── storage/transfers.TransferCache
                   │                          (persistent, incremental)
                   ▼
       TronHistory (TronGrid) · BscHistory (HyperSync, paced D33)
                                                    │
                    intel/store.py: labels · entities · counterparties · lookalike index
                                                    │
                    cases/: cases · decisions (own hash chain) ── core/audit.py (check chain)
                                                    │
                                            SQLite (WAL), ~/.amlcheck/amlcheck.db
```

---

## 2. New packages

| Package / module | Responsibility | Phase |
|---|---|---|
| `storage/transfers.py` | `TransferCache`: stores transfers once, tracks per-address covered windows, fetches only the gaps through a `HistorySource`, serves `History` objects with 0.5.1 semantics | 6 |
| `intel/categories.py` | The category table from METHODOLOGY §5 as an enum with weights, order, allowed provenance, `category_version` | 6 |
| `intel/store.py` | Labels (add, retract, list, resolve best label for an address), entities, membership, `labels.csv` mapping | 6 |
| `intel/registry.py` | Counterparty upsert from a `CheckResult`, queries, `rebuild` from the audit log | 6 |
| `intel/lookalike.py` | `lookalike_key()`, R-HEU-06 source adapter | 6 |
| `intel/packs.py` | Label-pack import with licence record | 6 |
| `profile/features.py` | `Profile` dataclass + `profile(address, transfers, window, now)` pure function | 7 |
| `profile/contracts.py` | `ContractLookup` for TRON (TronGrid) and BSC (`eth_getCode`), cached forever | 7 |
| `profile/classifier.py` | `classify(profile, context) → list[Classification]`, entity linking for `DEPOSIT` | 7 |
| `profile/adapter.py` | Source adapter for the screened address's own classification (R-HEU-07) | 7 |
| `trace/model.py` | `TraceItem`, `TraceNode`, `TraceEdge`, `Trace`, buckets, JSON (de)serialisation | 8 |
| `trace/engine.py` | The algorithm in METHODOLOGY §2, async, budget-aware, deterministic | 8 |
| `trace/annotate.py` | Peel-chain annotation (METHODOLOGY §4.1) | 8 |
| `trace/rules.py` | R-TRC-01…05 from a `Trace` | 8 |
| `trace/adapter.py` | `TraceAdapter`: a `SourceAdapter` (like `TwoHopAdapter`), required when it runs | 8 |
| `trace/jobs.py` | `TraceQueue`: persistent job table, one worker, progress callbacks, resume | 8 |
| `trace/graph.py` | Layered SVG (hop columns) built on the existing `graph.py` palette and marks (D38) | 8 |
| `core/score.py` | Score v1 from findings + trace (METHODOLOGY §7) | 9 |
| `report.py` | Case report PDF (reportlab, Helvetica as D32) with the trace graph | 9 |
| `cases/cases.py` | Open, list, show, close cases; confirm/reject inferences; name entities | 10 |
| `cases/decisions.py` | Append-only hash-chained decisions, verify | 10 |
| `monitor/wallets.py` | Own wallets | 11 |
| `monitor/inbound.py` | Poll own wallets, screen new senders, alert, lock file | 11 |

---

## 3. Changes to existing modules

| Module | Change | Phase |
|---|---|---|
| `exposure/reader.py` | `HistoryReader` reads through `TransferCache` instead of the 15-minute response cache. Same public `read()` signature, same in-flight de-duplication | 6 |
| `exposure/history.py` | `HistorySource.fetch` gains optional `until`/`after` bounds for incremental reads (V18, V19). `Transfer` gains `log_index` (or TRON event index) for uniqueness | 6 |
| `labels.py` | Unchanged: `labels import` still replaces the `labels` table whole (D17) and R-HEU-05 still reads it. `intel/store.py` reads that table as provenance `labels.csv`, mapping tags to categories | 6 |
| `adapters/__init__.py::build` | Adds the look-alike source (always), classification source (Phase 7), `TraceAdapter` when `wants_trace()` instead of `TwoHopAdapter` when `[trace] enabled` | 6, 7, 8 |
| `core/rules.py` | New rule IDs and defaults. `FIXED` gains a set of rules that may not be raised to BLOCK (R-HEU-07, R-TRC-05) | 6–8 |
| `core/engine.py::screen` | After the verdict, compute the score (Phase 9); after the audit write, upsert the registry (Phase 6). Neither may fail the check: errors are logged and the field is left unset | 6, 9 |
| `core/audit.py` | Hashes `score_json` only when set (D24 pattern). `verify` also verifies the decision chain | 9, 10 |
| `core/models.py` | `CheckResult.score: Score | None = None`. `Severity` unchanged | 9 |
| `output.py` | Human output: score line, exposure table, coverage, classifications | 8, 9 |
| `graph.py` | Shared palette and marks reused by `trace/graph.py`; 2-hop graph unchanged | 8 |
| `export.py` | Decisions in exports; case report entry point | 10 |
| `web/` | Pages: counterparties, counterparty detail, trace (progress + graph), case | 6–10 |
| `api.py` | Fields and endpoints from PRD F10 | 9, 11 |
| `cli.py` | Sub-apps `cp`, `intel`, `case`, `wallets`, `monitor`; commands `classify`, `trace` | 6–11 |
| `config.py`, `config.example.toml` | Sections `[trace]`, `[classifier]`, `[score]`, `[monitor]`, `[cache] history_keep_days`, `[operator] name` | 6–11 |

`cli.py` is already ~900 lines. New command groups go into `cli_intel.py`, `cli_trace.py`, `cli_cases.py`,
`cli_monitor.py` and are added to the Typer app, so `cli.py` does not grow further.

---

## 4. Key interfaces

```python
# storage/transfers.py
class TransferCache:
    def __init__(self, conn: sqlite3.Connection, source: HistorySource, now: Clock) -> None: ...
    async def history(self, address: str, since: datetime, until: datetime | None,
                      limit: int, *, first_activity: bool) -> History: ...
    def stats(self) -> CacheStats: ...          # rows, addresses, hit rate this process
    def prune(self, keep_days: int, keep: Callable[[str, Chain], bool]) -> int: ...

# profile/features.py
@dataclass(frozen=True)
class Profile:
    address: str; chain: Chain; window: tuple[datetime, datetime]
    n_in: int; n_out: int; distinct_senders: int; distinct_recipients: int
    volume_in: Decimal; volume_out: Decimal; retained_share: Decimal
    first_seen: datetime | None; last_seen: datetime | None
    median_hold_hours: Decimal | None; pass_through_share_24h: Decimal
    top_recipient: str | None; top_recipient_share_out: Decimal; top_sender_share_in: Decimal
    small_in_share: Decimal; round_share: Decimal
    max_senders_24h: int; max_recipients_24h: int; capped: bool; is_contract: bool | None

def profile(address: str, history: History, window: tuple[datetime, datetime],
            settings: ClassifierSettings) -> Profile: ...

# profile/classifier.py
@dataclass(frozen=True)
class Classification:
    address: str; chain: Chain; type: str; confidence: Decimal; primary: bool
    features: dict[str, Any]; classifier_version: int
    computed_at: datetime; expires_at: datetime; entity_id: int | None

def classify(p: Profile, ctx: ClassifyContext, settings: ClassifierSettings,
             now: datetime) -> list[Classification]: ...
# ClassifyContext answers: is X a HUB / labelled service? is X FRESH or PASS_THROUGH? (local only)

# trace/engine.py
class TraceEngine:
    def __init__(self, cache: TransferCache, intel: IntelStore, classifier: Classifier,
                 contracts: ContractLookup, settings: TraceSettings, now: Clock) -> None: ...
    async def run(self, target: Address, direction: Literal["in", "out"],
                  progress: Callable[[TraceProgress], None] | None = None) -> Trace: ...
# raises TraceFailed(partial: Trace, reason: str) on read failure or time budget

# intel/store.py
class IntelStore:
    def best_terminal(self, chain: Chain, address: str) -> Terminal | None: ...  # tests 2–5
    def add_label(self, label: NewLabel) -> int: ...
    def retract(self, label_id: int, reason: str, by: str) -> None: ...
    def entity_for(self, chain: Chain, address: str) -> Entity | None: ...
    def link(self, chain: Chain, address: str, entity_id: int, evidence: dict[str, Any]) -> None: ...
```

---

## 5. Flows

### 5.1 A check in v1

| Step | What | New in v1 |
|---|---|---|
| 1 | Parse and normalise address (unchanged) | |
| 2 | `build()` picks sources: OFAC, EV, USDT freeze, 1-hop exposure, look-alike, classification; `TraceAdapter` if amount ≥ `auto_amount_usdt` or asked | look-alike, classification, trace |
| 3 | `screen()` runs them concurrently; trace reads go through the same `TransferCache`, so the target's history is read once | shared cache |
| 4 | Rules → verdict (unchanged precedence) | R-HEU-06/07, R-TRC-* |
| 5 | Score from findings + trace | score |
| 6 | Audit write (hash covers score when set) | |
| 7 | Registry upsert, classifications and entity links persisted | registry |
| 8 | Output (CLI / JSON / web / API) | score, exposure, coverage |

### 5.2 A trace job (web or API)

```
POST /v1/traces ─► traces row (queued) ─► TraceQueue worker
                                             │ progress → traces.progress_json (poll / HTMX)
                                             ▼
                                   TraceEngine.run()  ─► reads via TransferCache (paced)
                                             │
                     ok ─► traces row (done, result_json) ─► optional: run a check with
                     │                                       trace result attached (check_id)
                     └── fail ─► traces row (failed, partial_json, reason)
```

A trace job never writes to the audit log by itself. A **check** that includes a trace does: its trace
source result carries the trace summary (partition, coverage, paths, budget) in `evidence_meta`, and
the full trace JSON is stored in `traces` linked by `check_id`.

### 5.3 Monitoring run

```
lock ─► for each own wallet: TransferCache.history(wallet, since=last_run) ─► new senders
     ─► skip senders screened within rescreen_days ─► check (with trace if amount ≥ threshold)
     ─► collect REVIEW/BLOCK ─► table + audit (already) + notification + optional webhook
     ─► save monitor_state ─► unlock ─► exit 0 or 6
```

---

## 6. Concurrency & budgets

| Concern | Rule |
|---|---|
| Rate limits | Every TronGrid and HyperSync call goes through the existing clients. HyperSync's pacer (D33) is process-wide; TronGrid gets a process-wide token bucket in Phase 6 (`[tron] requests_per_second`, default from V18) |
| Traces | One `TraceQueue` worker per process. CLI `trace` runs inline but takes the same lock |
| Checks | API and web keep D39/D26: one check at a time. A check that needs a trace waits for the worker |
| SQLite | WAL, one writer connection per process, short transactions. Trace progress written at most once a second |
| Cross-process | `monitor run`, `watch run`, `batch` take a file lock in `~/.amlcheck/` so two schedulers never overlap |
| Determinism | The engine never iterates a set or dict without sorting. Times come from the injected clock |

---

## 7. Testing strategy

| Layer | How |
|---|---|
| Features, classifier, score, trace algorithm | Pure functions over synthetic transfers. Property tests: partition sums to 1; adding a pruned sender never lowers coverage; determinism |
| Transfer cache | Fake `HistorySource` that records the windows asked for; assert only gaps are fetched |
| Adapters | Recorded fixtures (`tests/fixtures/`), `respx`, never the network (unchanged rule) |
| Migrations | A 0.5.1 database fixture (`tests/fixtures/db/v0_5_1.sqlite`) migrates and `audit verify` passes |
| Golden set | `tests/golden/` replayed offline from recorded histories (Phase 12) |
| Live | Each phase records live runs in `docs/acceptance.md`, as phases 1–5 did |
