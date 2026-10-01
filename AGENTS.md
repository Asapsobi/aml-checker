# AGENTS.md

Instructions for AI coding agents working in this repository.

**amlcheck** is a local AML screening tool for USDT on TRON and BSC. Version 0.5.1 is in use. The
current work is **v1.0, counterparty intelligence**, built phase by phase (phases 6–12).

## Start here

1. Read [`docs/v1/AGENT_BRIEF.md`](docs/v1/AGENT_BRIEF.md). It gives the reading order, the rules,
   the repo conventions, the per-phase workflow and the definition of done.
2. The spec is in [`docs/v1/`](docs/v1/): PRD, methodology, architecture, data model, roadmap,
   acceptance tests.
3. Every external fact and every decision so far is in
   [`docs/verification.md`](docs/verification.md). Don't contradict a decision (D1–D49) without
   asking.

## Commands

```bash
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
uv run mypy
AMLCHECK_HOME=$(mktemp -d) uv run amlcheck check <address>   # live run without touching real data
```

## Non-negotiables

- Never a clean result (`NO_HITS`) over missing or failed data.
- The audit log is append-only and hash-chained; records written by earlier versions must keep verifying.
- Tests never touch the network: recorded fixtures only.
- No secrets in the repo; keys live in `.env`.
- One phase = one branch, one PR reviewed by the owner, one release tag. No code pushed to `main` directly.
