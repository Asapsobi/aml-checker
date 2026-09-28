# aml-checker

Local, laptop-run AML screening tool for USDT addresses on **TRON (TRC20)** and **BNB Smart Chain (BEP20)**.

It combines sanctions lists, stablecoin issuer freeze/seize history, on-chain exposure and behavioral heuristics into one verdict (`BLOCK` / `REVIEW` / `INCOMPLETE` / `NO_HITS`) with evidence and a tamper-evident audit log.

> Decision-support and record-keeping tool, not a legal determination. `NO_HITS` is not a clearance.

## Docs

- [PRD & Roadmap](docs/PRD.md) — scope, verdict model, data sources, architecture, phased roadmap and acceptance tests. **AI coding agents: read §0 first.**
- [Verification report](docs/verification.md) — every data source checked against the live service, with the open questions.

## Status

Phase 0 (foundations + verification): skeleton and verification report done. Still open: Eagle Virtual's BSC coverage, which needs an API key, and the questions at the end of the verification report. No command screens addresses yet; that starts in Phase 1.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```bash
uv sync
uv run amlcheck status
```

API keys go in `.env` (copy `.env.example`). Settings go in `~/.amlcheck/config.toml` (copy `config.example.toml`); without it, the PRD defaults apply. The database lives in `~/.amlcheck/` too.

## Development

```bash
uv run pytest
uv run ruff check
uv run ruff format
uv run mypy
```
