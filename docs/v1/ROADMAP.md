# amlcheck v1 — Roadmap and task breakdown

> Phases 6–12 take amlcheck from 0.5.1 to 1.0.0. Each phase is one branch, one PR, one release (D49).
> Tasks are sized for an AI coding agent working with owner review. Do them in order; within a phase,
> the order given is the suggested one.

**Every phase ends the same way** (not repeated below):

- [ ] `uv run pytest`, `ruff check`, `ruff format --check`, `mypy` green; CI green on all 5 jobs
- [ ] A 0.5.1 database migrates and `amlcheck audit verify` passes before and after
- [ ] `docs/verification.md`: new V-items, decisions (D50+), questions (Q23+) with status
- [ ] `docs/acceptance.md`: the phase's AT-V1 tests mapped to test names, plus live results with dates
- [ ] README updated for every new command; `config.example.toml` for every new key
- [ ] Version bumped in `pyproject.toml` (and `uv.lock`), PR merged by the owner, tag `v0.N.0`, GitHub release

---

## Phase 6 — Intel foundations → v0.6.0 (~1 week)

**Goal:** stop re-reading histories, start remembering counterparties, give labels a home.

| # | Task | Files |
|---|---|---|
| 6.1 | Copy PRD §10 questions Q23–Q30 into `docs/verification.md`; ask the owner for Q27 and Q29 before coding | docs |
| 6.2 | **V18**: TronGrid TRC20 transfers endpoint: `min_timestamp`/`max_timestamp`, ordering, the event index field; measure a 31-address read on a free key. **V19**: HyperSync query from a block, cost per incremental read, the `log_index` field. Record fixtures | docs, `tests/fixtures/` |
| 6.3 | Build a 0.5.1 database fixture (`tests/fixtures/db/v0_5_1.sqlite`) with checks, findings, watchlist, API keys; test that it migrates and verifies | tests |
| 6.4 | Migration `0005_intel.sql` | `storage/migrations/` |
| 6.5 | `Transfer.idx`; `HistorySource.fetch(..., until=None)` bounds for TRON and BSC | `exposure/history.py`, `adapters/tron.py`, `adapters/hypersync.py` |
| 6.6 | `TransferCache`: store, window bookkeeping, gap computation, incremental fetch, D13/D22 semantics, `stats()`, `prune()` | `storage/transfers.py` |
| 6.7 | `HistoryReader` reads through `TransferCache` (same signature, same de-dup) | `exposure/reader.py` |
| 6.8 | TronGrid process-wide token bucket (`[tron] requests_per_second`, default from V18) | `adapters/tron.py`, `config.py` |
| 6.9 | Categories module (METHODOLOGY §5) | `intel/categories.py` |
| 6.10 | `IntelStore`: labels add/retract/list, `labels.csv` mapping (F4.7), entities, membership, `best_terminal()` for tests 2–4 | `intel/store.py` |
| 6.11 | Label packs with mandatory licence (F4.5) | `intel/packs.py` |
| 6.12 | Registry: upsert after every audit write, `cp list/show/rebuild` | `intel/registry.py`, `core/engine.py`, `cli_intel.py` |
| 6.13 | Look-alike key + R-HEU-06 source, always on | `intel/lookalike.py`, `core/rules.py`, `adapters/__init__.py` |
| 6.14 | CLI: `intel label`, `intel retract`, `intel show`, `intel stats`, `intel prune`, `intel import-pack` | `cli_intel.py` |
| 6.15 | Web: Counterparties list and detail page (checks, labels, entity) | `web/` |

**Exit criteria**

| Criterion | How shown |
|---|---|
| A second check of the same address within the lookback reads only new transfers | Test with a recording fake source; live: query counts before/after in `acceptance.md` |
| A screened look-alike of a registry address gives REVIEW with R-HEU-06 naming both addresses | AT-V1-05 |
| `cp rebuild` from the audit log gives the same registry rows | AT-V1-03 |
| `labels import` behaves exactly as in 0.5.1 | existing tests unchanged and green |

---

## Phase 7 — Profiler & classifier → v0.7.0 (~1–1.5 weeks)

**Goal:** say who an unknown address probably is, with evidence.

| # | Task | Files |
|---|---|---|
| 7.1 | **V20**: a free BSC JSON-RPC endpoint for `eth_getCode` without a key, its limits. TRON contract lookup via the existing `getcontract` fixture | docs, fixtures |
| 7.2 | Migration `0006_classifier.sql` | migrations |
| 7.3 | `profile()` pure function with every feature in METHODOLOGY §3; reuse `heuristics.pass_through` and `busiest_window` | `profile/features.py` |
| 7.4 | `ContractLookup` (TRON, BSC), cached in `contracts` | `profile/contracts.py` |
| 7.5 | `classify()`: every type, confidence bonuses, primary type, `FRESH` tag, versioning, expiry | `profile/classifier.py` |
| 7.6 | Entity linking for `DEPOSIT` → hub's entity, auto-named entity creation | `profile/classifier.py`, `intel/store.py` |
| 7.7 | `[classifier]` config: `window_days` 90, `ttl_days` 14, every threshold in METHODOLOGY §4 | `config.py` |
| 7.8 | Classification source for the screened address + R-HEU-07 (may not be raised to BLOCK) | `profile/adapter.py`, `core/rules.py` |
| 7.9 | `amlcheck classify <address> [--json]` | `cli_intel.py` |
| 7.10 | `intel entity list/show/name` | `cli_intel.py` |
| 7.11 | Web: profile and classification panel on the counterparty page, entity page | `web/` |
| 7.12 | One unit test per type per condition (fires / does not fire at the boundary) | `tests/unit/test_classifier.py` |

**Exit criteria**

| Criterion | How shown |
|---|---|
| A known exchange hot wallet on each chain → `HUB`; one of its deposit addresses → `DEPOSIT` linked to the same entity | Live, recorded in `acceptance.md` |
| Naming the entity once labels both | AT-V1-08 |
| A synthetic collector history → `COLLECTOR` ≥ 0.7 → R-HEU-07 REVIEW (low) | AT-V1-09 |
| Config cannot raise R-HEU-07 to BLOCK | AT-V1-10 |

---

## Phase 8 — Source-of-funds trace → v0.8.0 (~2 weeks)

**Goal:** answer "where did this money come from" within budget.

| # | Task | Files |
|---|---|---|
| 8.1 | Owner answers Q23, Q24, Q28 (after a cost measurement in 8.12) | docs |
| 8.2 | Migration `0007_trace.sql` | migrations |
| 8.3 | Trace model, buckets, JSON round trip with `Decimal` as strings | `trace/model.py` |
| 8.4 | Engine: windows, weights, pruning, terminal tests 1–11, cycles, revisits, budgets, failures (METHODOLOGY §2) | `trace/engine.py` |
| 8.5 | Property tests: partition = 1 ± 0.001; determinism; never exceeds `max_nodes`; pruning only adds to `untraced:pruned` | `tests/unit/test_trace_*.py` |
| 8.6 | Peel-chain annotation | `trace/annotate.py` |
| 8.7 | R-TRC-01…05 with evidence (paths, bottleneck, estimated amount) | `trace/rules.py`, `core/rules.py` |
| 8.8 | `TraceAdapter`, `wants_trace()`, replace auto 2-hop when `[trace] enabled`; keep `investigate --two-hop` | `trace/adapter.py`, `adapters/__init__.py`, `cli.py` |
| 8.9 | `TraceQueue`: persistent jobs, single worker, progress, resume after restart | `trace/jobs.py` |
| 8.10 | `amlcheck trace <address> [--direction in|out] [--hops N] [--graph file.svg] [--json]` with live progress | `cli_trace.py` |
| 8.11 | Layered SVG graph (hop columns, category marks + legend, edge width, weight %), reusing D38 palette and 8+6 labels | `trace/graph.py` |
| 8.12 | Live budget run: 3 TRON + 3 BSC counterparties, cold and warm; record queries, seconds, cache hits; compare with G9 | `acceptance.md` |
| 8.13 | Human output: exposure table, coverage line, top paths | `output.py` |
| 8.14 | Web: trace box on the check form, trace page with progress (HTMX polling) and graph | `web/` |

**Exit criteria**

| Criterion | How shown |
|---|---|
| The METHODOLOGY §2.11 example reproduces exactly from fixtures | AT-V1-12 |
| A failed node read → INCOMPLETE; `max_nodes` hit → `untraced:budget`, not INCOMPLETE | AT-V1-14, AT-V1-15 |
| Default trace within G9 budget, p95 cold ≤ 5 min on BSC | 8.12 results |
| Old `investigate --two-hop` output unchanged | existing tests |

---

## Phase 9 — Risk score & reports → v0.9.0 (~1 week)

**Goal:** one number with a breakdown, everywhere the verdict appears.

| # | Task | Files |
|---|---|---|
| 9.1 | Owner answers Q26 | docs |
| 9.2 | Migration `0008_score.sql`; audit hashes `score_json` only when set | migrations, `core/audit.py` |
| 9.3 | `core/score.py`: METHODOLOGY §7, lower bound for INCOMPLETE, half-up rounding, components | `core/score.py` |
| 9.4 | Engine computes the score after the verdict; failure leaves it unset and logs | `core/engine.py` |
| 9.5 | Optional R-SCR-01 (`[score] review_at`, 0 = off) | `core/rules.py` |
| 9.6 | CLI, JSON (`score`, `band`, `components`, `exposure`, `coverage`, `classifications`), web, batch CSV columns | `output.py`, `batch.py`, `web/` |
| 9.7 | API: additive fields, `"trace"` parameter on `POST /v1/check`; `docs/api.md` updated | `api.py`, docs |
| 9.8 | Case report PDF per counterparty: verdict, score, sources, findings, trace graph, exposure table, classifications | `report.py`, `cli_intel.py` (`cp report`) |
| 9.9 | Unit tests for every component and the band boundaries | tests |

**Exit criteria**

| Criterion | How shown |
|---|---|
| §2.11 example scores 66, `high` | AT-V1-17 |
| BLOCK → 100; INCOMPLETE → lower bound shown | AT-V1-18 |
| Records written before 0.9.0 still verify; new ones hash the score | AT-V1-19 |
| 0.5.1 API clients see their fields unchanged | `test_api.py` contract test |

---

## Phase 10 — Cases & feedback → v0.10.0 (~1–1.5 weeks)

**Goal:** record the human decision, and learn from it.

| # | Task | Files |
|---|---|---|
| 10.1 | Owner answers Q30; `[operator] name` | docs, `config.py` |
| 10.2 | Migration `0009_cases.sql` | migrations |
| 10.3 | Decisions hash chain + verify; `audit verify` covers both chains and prints both heads | `cases/decisions.py`, `core/audit.py`, `cli.py` |
| 10.4 | Cases: open, list, show, close with decision | `cases/cases.py`, `cli_cases.py` |
| 10.5 | Confirm/reject inferences inside a case; confirmation → operator label; rejection suppression in `classify()` | `cases/cases.py`, `profile/classifier.py` |
| 10.6 | Name an entity from a case | `cases/cases.py`, `intel/store.py` |
| 10.7 | Exports include decisions (CSV/JSON/PDF); case report includes the decision | `export.py`, `report.py` |
| 10.8 | `case export --jsonl` (features + outcome) for a later model | `cli_cases.py` |
| 10.9 | Web: "Open case" on REVIEW/BLOCK, case page with decision form (start-up token, D31) | `web/` |

**Exit criteria**

| Criterion | How shown |
|---|---|
| A tampered decision row is reported by `audit verify` | AT-V1-21 |
| A rejected `COLLECTOR` stays suppressed for that address until `classifier_version` changes | AT-V1-22 |
| Confirming a `DEPOSIT` and naming its entity "Binance" (`exchange_regulated`) makes the next trace end there with no read | AT-V1-23 |

---

## Phase 11 — Inbound monitoring & async API → v0.11.0 (~1 week)

**Goal:** counterparties are found and screened without anyone typing an address.

| # | Task | Files |
|---|---|---|
| 11.1 | Owner answers Q25 | docs |
| 11.2 | Migration `0010_monitor.sql` | migrations |
| 11.3 | `wallets add/remove/list`; own wallets get `own_or_trusted` labels | `monitor/wallets.py`, `cli_monitor.py` |
| 11.4 | `monitor run`: lock, read new inbound transfers, rescreen policy, trace threshold, report, exit 6 on REVIEW/BLOCK, optional local webhook | `monitor/inbound.py` |
| 11.5 | Scheduling: launchd plist, cron line, systemd timer | `docs/scheduling.md`, `docs/server.md` |
| 11.6 | API: `POST /v1/traces`, `GET /v1/traces/{id}`, `GET /v1/counterparties/{chain}/{address}` with D39/D41/D43 | `api.py`, `docs/api.md` |
| 11.7 | `scripts/corridor_mock.py` extended with a trace request and polling | scripts |

**Exit criteria**

| Criterion | How shown |
|---|---|
| A new sender to an own wallet is screened in the next run; a sender screened 2 days ago is skipped | AT-V1-25, AT-V1-26 |
| Two overlapping `monitor run`s: the second exits without screening | AT-V1-27 |
| Trace job via API: 202 → progress → result; same Idempotency-Key replays | AT-V1-28 |

---

## Phase 12 — Calibration & 1.0 → v1.0.0 (~1 week)

**Goal:** prove the thresholds on real addresses, then freeze the contract.

| # | Task | Files |
|---|---|---|
| 12.1 | Golden set (METHODOLOGY §8): ≥ 40 addresses per chain, histories recorded for offline replay | `tests/golden/` |
| 12.2 | Live run; precision per type, band agreement, false positives on clean addresses | `acceptance.md` |
| 12.3 | Tune thresholds in config defaults only; each change a decision. Formula changes bump versions | `config.py`, docs |
| 12.4 | Performance and budget report: check p95, trace p95 cold/warm, queries per trace, cache hit rate after 2 weeks of use | `acceptance.md` |
| 12.5 | Review every R-rule's default severity with the owner | docs |
| 12.6 | Docs: README v1 sections, `docs/PRD.md` points to v1, operator guide for cases and traces | docs |
| 12.7 | Declare the JSON contract v1 stable in `docs/api.md` | docs |

**Exit criteria**

| Criterion | Target |
|---|---|
| `HUB`, `DEPOSIT` precision | ≥ 0.9 |
| `COLLECTOR` precision | ≥ 0.8 |
| Golden clean addresses in `high`/`severe` | 0 |
| Trace budget | Meets G9 |
| Owner sign-off | Recorded in `acceptance.md` |

---

## Dependencies

```
6 (cache, store, registry) ──► 7 (profile, classify) ──► 8 (trace) ──► 9 (score) ──► 10 (cases)
          │                                                  │                         │
          └──────────────────────────────────────────────────┴──────► 11 (monitor) ◄───┘
                                                                              │
                                                                              ▼
                                                                        12 (calibrate, 1.0)
```
