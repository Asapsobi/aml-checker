# amlcheck v1 — Brief for the AI coding agent

> Hand this file to a new coding agent. It says what to build, what to read, how this repo works and
> what "done" means. The owner is Sobi. You work phase by phase and the owner reviews every PR.

---

## 1. Mission

Take **amlcheck 0.5.1** (working, in production use on the owner's laptop and corridor server) to
**v1.0.0: counterparty intelligence**. In one line: for every address that deals with the business,
know who it probably is, where its money came from, how risky it is, and what the operator decided,
using only RPC providers and indexers, never own nodes, and never labelling the whole chain.

---

## 2. Read in this order

| # | File | Why |
|---|---|---|
| 1 | `README.md` | What 0.5.1 does today, every command |
| 2 | `docs/PRD.md` | The original spec. §0 rules 1–7 still bind you |
| 3 | `docs/verification.md` | Every external fact checked so far (V1–V16), decisions D1–D49, answers Q1–Q22. **Do not contradict a decision without asking** |
| 4 | `docs/v1/PRD.md` | What v1 adds. §0 rules 8–15 |
| 5 | `docs/v1/METHODOLOGY.md` | The algorithms, thresholds and formulas, exactly |
| 6 | `docs/v1/ARCHITECTURE.md` | Where new code goes, which existing modules change |
| 7 | `docs/v1/DATA_MODEL.md` | Migrations 0005–0010 |
| 8 | `docs/v1/ROADMAP.md` | Phase tasks and exit criteria |
| 9 | `docs/v1/ACCEPTANCE.md` | AT-V1 tests to implement |
| 10 | `src/amlcheck/core/`, `exposure/`, `adapters/__init__.py`, `adapters/two_hop.py` | The code v1 builds on |

---

## 3. Hard rules (summary)

| Rule | Source |
|---|---|
| Phase by phase. Exit criteria before the next phase | PRD §0.1 |
| Verify external facts live before coding against them; record as V17+ | PRD §0.2, v1 §0.15 |
| Never invent API fields. Record real responses as fixtures | PRD §0.3 |
| Never a clean result over a data gap. Pruned ≠ failed | PRD §0.4, v1 §0.11 |
| Audit record written before the result is shown; old records keep verifying | PRD §0.5, v1 §0.9 |
| No secrets in the repo | PRD §0.6 |
| Ambiguity → write a question, don't guess | PRD §0.7 |
| Extend 0.5.1, don't rewrite it | v1 §0.8 |
| Inferred never BLOCKs, always shows confidence | v1 §0.10 |
| Deterministic outputs | v1 §0.12 |
| No Eagle Virtual data stored past 15 min; no unlicensed label data | D46, v1 §0.13 |

---

## 4. How this repo works

### Setup

```bash
uv sync
cp .env.example .env            # owner's keys: EAGLE_VIRTUAL_API_KEY, TRONGRID_API_KEY,
                                # HYPERSYNC_API_TOKEN, AMLCHECK_API_TOKEN
uv run amlcheck sync            # OFAC + TRON index, a few minutes
uv run pytest && uv run ruff check && uv run ruff format --check && uv run mypy
```

Use a scratch home for live runs so the owner's real audit log is untouched:
`AMLCHECK_HOME=$(mktemp -d) uv run amlcheck …`

### Conventions already in the code

| Convention | Example |
|---|---|
| Amounts are `Decimal`, serialised as strings | `exposure/history.py` |
| Time comes from an injected clock (`now: Callable[[], datetime]`), always UTC | `core/clock.py` |
| Every source is a `SourceAdapter` returning `SourceResult`; failure → `failed()` | `adapters/base.py`, `core/engine.py::run_source` |
| A finding carries `rule_id`, severity, source, a plain-English summary, evidence, `observed_at` | `core/rules.py::finding` |
| Low priority is `"priority": "low"` in evidence, not a new severity | D20 |
| Migrations are numbered, append-only SQL files | `storage/migrations/` |
| New hashed audit fields are hashed only when set | D24 |
| Tests never use the network; `respx` + recorded fixtures | `tests/fixtures/README.md` |
| Docstrings and comments say *why*, citing PRD sections, V-items and D-numbers | everywhere |
| Docs are plain English, short sentences, tables first | `docs/verification.md` |
| `mypy --strict` on the whole package (D4); ruff rule set in `pyproject.toml` | |
| CI: Ubuntu + macOS × Python 3.12/3.14, plus Windows 3.12 (D5, D47) | `.github/workflows/ci.yml` |

### Workflow per phase

```
git switch -c phase-N-<slug> main
  … verify (V-items) → ask (Q-items) → build tasks → tests → live runs …
update docs/verification.md, docs/acceptance.md, README, config.example.toml
bump version to 0.N.0 in pyproject.toml (src/amlcheck/__init__.py reads it), uv lock
open PR "Phase N: <goal>" with: what changed, decisions taken, live results, open questions
owner reviews and merges → tag v0.N.0 on the merge commit → GitHub release
```

Never push to `main` directly with code. Never tag before the owner merges.

---

## 5. Asking the owner

Put questions in `docs/verification.md` (Q23+) **and** at the top of the PR description, each with:
the question, why it matters, the options, and your recommended answer. Keep building whatever the
question does not block. If it blocks the phase, stop and say so.

---

## 6. Definition of done for a phase

- [ ] Every task in the phase's ROADMAP table done, or moved to a question with the owner's agreement
- [ ] Every AT-V1 test of the phase implemented and green
- [ ] Exit criteria shown (tests or live results in `docs/acceptance.md`)
- [ ] A 0.5.1 database still migrates and verifies
- [ ] CI green on all jobs
- [ ] Docs updated (README, verification, acceptance, config example, api/scheduling/server where touched)
- [ ] PR open with a summary the owner can review in 10 minutes

---

## 7. Starter prompt

Paste this to start a new agent session in this repo:

> You are building amlcheck v1 in this repository. Read `docs/v1/AGENT_BRIEF.md` first and follow
> its reading order. Then start **Phase 6** from `docs/v1/ROADMAP.md`: begin with task 6.1 (copy
> Q23–Q30 into `docs/verification.md` and list what you need from me), then the verification tasks
> 6.2 before any code. Work on branch `phase-6-intel-foundations`. Stop and ask me when a decision
> in `docs/verification.md` would have to change, or when something in the v1 docs is ambiguous.
