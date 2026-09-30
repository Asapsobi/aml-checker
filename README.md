# aml-checker

Local, laptop-run AML screening tool for USDT addresses on **TRON (TRC20)** and **BNB Smart Chain (BEP20)**.

It combines sanctions lists, stablecoin issuer freeze/seize history, on-chain exposure and behavioral heuristics into one verdict (`BLOCK` / `REVIEW` / `INCOMPLETE` / `NO_HITS`) with evidence and a tamper-evident audit log.

> Decision-support and record-keeping tool, not a legal determination. `NO_HITS` is not a clearance.

## Docs

- [PRD & Roadmap](docs/PRD.md) — scope, verdict model, data sources, architecture, phased roadmap and acceptance tests. **AI coding agents: read §0 first.**
- [Verification report](docs/verification.md) — every data source checked against the live service, the decisions taken, and the open questions.
- [Acceptance tests](docs/acceptance.md) — where each PRD acceptance test is covered, and the live results of each phase.
- [Scheduling](docs/scheduling.md) — re-screening the watchlist every day with launchd (macOS) or cron (Linux).

## Status

Phases 0 to 4 are done. `amlcheck check` screens an address against:

- the OFAC SDN list
- Eagle Virtual's record of stablecoin freezes
- on TRON, Tether's USDT blacklist
- the address's own USDT transfers over 180 days: who it dealt with (R-EXP) and how it moved money (R-HEU)

On BSC, the transfer history comes from Envio HyperSync and needs a free token: create one at https://envio.dev/app/api-tokens and set `HYPERSYNC_API_TOKEN` (Q4 in the verification report). Without it, a BSC check ends INCOMPLETE.

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
uv run amlcheck check TA3941uFAvmVibSkQ6fMJXxmaSNovX86mz --amount 50000 --client "ACME Ltd" --note "new OTC client"
```

| Verdict | Meaning | Exit status |
|---|---|---|
| `BLOCK` | Do not transact. Escalate | 5 |
| `REVIEW` | Review by hand before transacting | 3 |
| `INCOMPLETE` | A required source failed or is out of date. Retry, or treat it as REVIEW | 4 |
| `NO_HITS` | Nothing found in the sources checked. Not a clearance | 0 |

`check` exits with 1 when it could not run at all, for example because of an invalid address. Add `--json` for the machine-readable result. Every check is written to the audit log before its result is shown:

```bash
uv run amlcheck audit list --from 2026-09-01 --verdict block --client "acme ltd"
uv run amlcheck audit verify
```

Run `amlcheck sync sanctions` every day: a list more than 48 hours old makes every result INCOMPLETE. The TRON blacklist index refreshes itself on every check.

### Many addresses at once

Put the addresses in a CSV file with an `address` column; `chain`, `amount`, `note` and `client` are optional, and other columns are ignored:

```bash
uv run amlcheck batch new-wallets.csv --out results.csv --client "ACME Ltd"
```

Every row is checked first, and nothing is screened if any row is wrong. The addresses are then screened one at a time, within every free plan's rate limit, and each result is written to `results.csv` as soon as it is ready. The exit status is the worst verdict found.

### Re-screening approved addresses

```bash
uv run amlcheck watch add TJwwz9NR37hjXdAV5gowj7src4avMuZZNW --client "ACME Ltd"
uv run amlcheck watch list
uv run amlcheck watch run
```

`watch run` screens every watched address again and reports each verdict that changed: in a table, in the audit log, with exit status 6, and on macOS with a notification. [docs/scheduling.md](docs/scheduling.md) sets it to run every day.

### Exporting the audit log

```bash
uv run amlcheck audit export --from 2026-09-01 --to 2026-09-30 --client "ACME Ltd" --format pdf --out acme-september.pdf
```

`--format` is `csv`, `json` or `pdf`. JSON keeps every record exactly as stored, with its hashes, so anyone can check them again. JSON and PDF say whether the whole audit log verified at export time.

### The web page

```bash
uv run amlcheck web
```

This opens a page in your browser with a check form, the history of checks, and each check's details with links to Tronscan and BscScan. It runs on `127.0.0.1` only, so no other computer can reach it; stop it with Ctrl+C.

### Investigating an address

```bash
uv run amlcheck investigate 0xd5efbbd79fcdc2834b7e2dcc7a0c6279e1281e36 --graph network.svg
```

`investigate` adds the 2-hop walk to a check. It reads the address's 20 largest counterparties, and raises R-EXP-03 when one of them received at least 1,000 USDT from a sanctioned or frozen wallet. It takes up to two minutes, and it prints the walk as a table; `--graph` also draws it as a picture. A `check` with an `--amount` of 10,000 USDT or more includes the walk too, and so does the web page's "2-hop walk" box, whose details page shows the picture. The limits are under `[two_hop]` in `config.toml`.

### A paid vendor, later

None is set up. A commercial attribution vendor (Chainalysis, TRM, Elliptic, Crystal) can be added without changing amlcheck: see `src/amlcheck/vendor.py`, and name the class under `[vendor]` in `config.toml`. It is asked only for REVIEW results, or for amounts above a set value, and it adds attribution without ever changing the verdict.

### Your own labels

Keep known addresses in a CSV file with the columns `address,chain,tag,note,source`, then load it:

```bash
uv run amlcheck labels import labels.csv
```

Each import replaces all earlier labels, and imports nothing if any row is wrong. The tags `mixer`, `bridge` and `high_risk` raise R-HEU-05 when the screened address dealt with that address. The tag `allowlist`, for your own or known wallets, leaves that address out of the behaviour rules, though never out of a sanctions or freeze finding.

## Development

```bash
uv run pytest
uv run ruff check
uv run ruff format
uv run mypy
```

Tests never touch the network: they run against recorded responses in `tests/fixtures/`.
