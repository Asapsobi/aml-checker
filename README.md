# aml-checker

Local, laptop-run AML screening tool for USDT addresses on **TRON (TRC20)** and **BNB Smart Chain (BEP20)**.

It combines sanctions lists, stablecoin issuer freeze/seize history, on-chain exposure and behavioral heuristics into one verdict (`BLOCK` / `REVIEW` / `INCOMPLETE` / `NO_HITS`) with evidence and a tamper-evident audit log.

> Decision-support and record-keeping tool, not a legal determination. `NO_HITS` is not a clearance.

## Docs

- [PRD & Roadmap](docs/PRD.md) — scope, verdict model, data sources, architecture, phased roadmap and acceptance tests. **AI coding agents: read §0 first.**
- [Verification report](docs/verification.md) — every data source checked against the live service, the decisions taken, and the open questions.
- [Acceptance tests](docs/acceptance.md) — where each PRD acceptance test is covered, and the live Phase 1 results.

## Status

Phase 1 (MVP screening) is done. `amlcheck check` screens an address against three sources:

- the OFAC SDN list
- Eagle Virtual's record of stablecoin freezes
- on TRON, Tether's USDT blacklist

Phase 2 (exposure and heuristics) needs answers to Q4, Q5 and Q7 in the verification report.

## Setup

Needs [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer. Copy `.env.example` to `.env` and add your API keys, then:

```bash
uv sync
uv run amlcheck sync
uv run amlcheck status
```

The first `sync` takes a few minutes, mostly the OFAC download. Settings go in `~/.amlcheck/config.toml` (copy `config.example.toml`); without that file, the PRD defaults apply. The database and logs live in `~/.amlcheck/` too.

## Use

```bash
uv run amlcheck check TA3941uFAvmVibSkQ6fMJXxmaSNovX86mz --amount 50000 --note "new OTC client"
```

| Verdict | Meaning | Exit status |
|---|---|---|
| `BLOCK` | Do not transact. Escalate | 5 |
| `REVIEW` | Review by hand before transacting | 3 |
| `INCOMPLETE` | A required source failed or is out of date. Retry, or treat it as REVIEW | 4 |
| `NO_HITS` | Nothing found in the sources checked. Not a clearance | 0 |

`check` exits with 1 when it could not run at all, for example because of an invalid address. Add `--json` for the machine-readable result. Every check is written to the audit log before its result is shown:

```bash
uv run amlcheck audit list --from 2026-09-01 --verdict block
uv run amlcheck audit verify
```

Run `amlcheck sync sanctions` every day: a list more than 48 hours old makes every result INCOMPLETE. The TRON blacklist index refreshes itself on every check.

## Development

```bash
uv run pytest
uv run ruff check
uv run ruff format
uv run mypy
```

Tests never touch the network: they run against recorded responses in `tests/fixtures/`.
