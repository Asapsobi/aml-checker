# amlcheck v1 — Acceptance tests

> The v1 test matrix. Each test is automated against recorded fixtures (no network), unless marked
> **live**. When a phase is built, `docs/acceptance.md` maps each ID to its test names and records the
> live results with dates, as phases 0–5 did.

| ID | Phase | Input | Expected |
|---|---|---|---|
| AT-V1-01 | 6 | Check an address twice within the lookback; the source records the windows asked for | Second read asks only for transfers after the first read's `until`. Results identical |
| AT-V1-02 | 6 | 0.5.1 database fixture | Migrates to the current schema; `audit verify` passes before and after; old checks render unchanged |
| AT-V1-03 | 6 | Registry after 10 checks across 4 addresses and 2 clients; then `cp rebuild` | Same rows, same `clients_json`, same counts |
| AT-V1-04 | 6 | `intel label X mixer`, then `intel retract` | Label active then retracted; still listed with `--all`; R-HEU-05-style findings stop after retraction |
| AT-V1-05 | 6 | Registry holds `TJwwz9NR37…MuZZNW`; screen an address with the same first 4 and last 4 body characters | REVIEW, R-HEU-06, evidence shows both full addresses and the differing middle |
| AT-V1-06 | 6 | `intel import-pack pack.csv --source x` without `--licence` | Refused, nothing imported, exit 1 |
| AT-V1-07 | 7 | Synthetic history: 600 distinct counterparties | `HUB`, confidence 0.7; with `capped` → 0.95 |
| AT-V1-08 | 7 | Hub H and a deposit D that sweeps 99.5% to H within 6 h from 8 senders; then `intel entity name <H's entity> "Binance" --kind exchange_regulated` | D → `DEPOSIT` linked to H's entity; after naming, both resolve to `exchange_regulated` with provenance `operator` via the entity |
| AT-V1-09 | 7 | Synthetic history: 120 senders, 92% of inbound transfers < 100 USDT, 85% sent on to one address | `COLLECTOR` ≥ 0.7; screening it gives REVIEW (low) R-HEU-07 |
| AT-V1-10 | 7 | `[rules] severity = {"R-HEU-07" = "BLOCK"}` | Config refused at load with a clear error |
| AT-V1-11 | 7 | Each classifier condition at its threshold and one step past it | Fires / does not fire exactly at the boundary (one test per condition) |
| AT-V1-12 | 8 | METHODOLOGY §2.11 fixture | Partition `{exchange_regulated: 0.60, sanctioned: 0.20, suspicious_collector: 0.10, untraced:pruned: 0.10}`, coverage 0.90, R-TRC-01 (bottleneck 4,000), R-TRC-03, R-TRC-05 (low), 4 addresses read |
| AT-V1-13 | 8 | 500 random synthetic graphs | Partition sums to 1 ± 0.001; never more than `max_nodes` reads; identical JSON on re-run |
| AT-V1-14 | 8 | Node B's history read fails | Trace source `stale` → INCOMPLETE with R-SYS-01 naming B; partial trace saved |
| AT-V1-15 | 8 | `max_nodes = 3` on the §2.11 fixture | Remaining weight in `untraced:budget`; verdict not INCOMPLETE |
| AT-V1-16 | 8 | A sender that is already on the path (A → B → A) | Its weight goes to `untraced:cycle`; no infinite loop |
| AT-V1-17 | 9 | §2.11 fixture with R-HEU-01 | Score 66, band `high`, components `E 59.5, D 0, B 5, U 1` |
| AT-V1-18 | 9 | A BLOCK check; an INCOMPLETE check with E = 34 | 100; `≥ 34`, band `medium+` |
| AT-V1-19 | 9 | Audit log with records from 0.5.1, 0.8.0 and 0.9.0 | All verify; only 0.9.0 records hash `score_json` |
| AT-V1-20 | 9 | `POST /v1/check` from the 0.5.1 corridor mock | Every 0.5.1 field present and unchanged; new fields additive |
| AT-V1-21 | 10 | Tamper one `decisions` row | `audit verify` reports the break in the decision chain at that record; check chain still reported intact |
| AT-V1-22 | 10 | Reject `COLLECTOR` for X in a case; re-classify X; then bump `classifier_version` | Suppressed while the version is unchanged; returns after the bump |
| AT-V1-23 | 10 | Confirm D as `DEPOSIT`, name its entity "Binance" `exchange_regulated`, re-trace a target funded by D | D is a terminal at test 4 (no read), bucket `exchange_regulated` |
| AT-V1-24 | 10 | `audit export --format json` with decisions | Decisions included with their hashes; verifiable by recomputation |
| AT-V1-25 | 11 | Own wallet receives from a never-screened sender | Next `monitor run` screens it; REVIEW/BLOCK → exit 6 and audit record |
| AT-V1-26 | 11 | Sender screened 2 days ago, `rescreen_days = 7` | Skipped, logged as skipped |
| AT-V1-27 | 11 | Two `monitor run` processes at once | Second exits with "already running", exit 0, screens nothing |
| AT-V1-28 | 11 | `POST /v1/traces` with an Idempotency-Key, poll, repeat the POST | 202 + `trace_id`; progress then `done`; repeat returns the same `trace_id` (D41) |
| AT-V1-29 | 12 | **live** Golden set | Targets in ROADMAP Phase 12 met |
| AT-V1-30 | 12 | **live** 3 TRON + 3 BSC traces, cold and warm | Within G9 budgets; warm uses ≥ 50% fewer queries (G8) |
