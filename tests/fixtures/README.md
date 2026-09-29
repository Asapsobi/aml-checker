# Test fixtures

Real responses captured on 2026-09-28, during the Phase 0 verification and at the start of
Phase 1 (see `docs/verification.md`). Adapters are tested against these rather than invented
payloads (PRD §0 rule 3). No test reaches the network: `tests/conftest.py` fails any request
that a test has not mocked. When a provider changes its API, re-record the file and update the
date here.

## OFAC (`ofac/`)

| File | Contents |
|---|---|
| `sdn_sample.xml` | Four entries copied unchanged from the official `SDN.XML` of 2026-09-23, with its original header: LAZARUS GROUP (8 addresses labelled ETH, in mixed case), CHEIL CREDIT BANK (53 TRON addresses labelled USDT), Mingming WANG (including a TRON address that OFAC filed under XBT) and AEROCARIBBEAN AIRLINES (no addresses). OFAC data is US government work in the public domain. |

## TronGrid (`tron/`)

| File | Request |
|---|---|
| `usdt_getcontract.json` | `POST https://api.trongrid.io/wallet/getcontract` for the USDT contract `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` (bytecode removed to keep it small) |
| `events_AddedBlackList.json`, `events_RemovedBlackList.json`, `events_DestroyedBlackFunds.json` | `GET https://api.trongrid.io/v1/contracts/TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t/events?event_name=<name>&limit=2&order_by=block_timestamp,desc` |
| `trc20_transfers.json` | `GET https://api.trongrid.io/v1/accounts/TNiq9AXBp9EjUqhDhrwrfvAA8U3GUQZH81/transactions/trc20?limit=2&contract_address=TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` (an OFAC-listed address) |
| `isBlackListed_true.json` | `POST https://api.trongrid.io/wallet/triggerconstantcontract` calling `isBlackListed(address)` for `TAQM43owNJLZz3vh3PXxBu2qTWf2McMQwJ`, blacklisted by Tether on 2026-09-27 |
| `isBlackListed_false.json`, `deprecated_false.json` | The same call for an address that is not blacklisted, and for `deprecated()` |
| `solidity_nowblock.json` | `POST https://api.trongrid.io/walletsolidity/getnowblock`, the latest confirmed block (its transactions removed) |
| `transfers_TAjoXR.json` | `GET https://api.trongrid.io/v1/accounts/TAjoXRsomrsDDCXsxD1ELFQu4wHfF9HZSv/transactions/trc20?contract_address=TR7N…&only_confirmed=true&limit=200&order_by=block_timestamp,desc`: its whole history, 12 transfers, including 500,000 USDT from the Tether-frozen `TAQM43ow…` (the Phase 2 exit criterion) |
| `transfers_TAjoXR_first.json` | The same with `order_by=block_timestamp,asc&limit=1`: its first USDT transfer |
| `getaccount_TAjoXR.json` | `POST https://api.trongrid.io/wallet/getaccount` for the same address, activated on 2026-09-24 |
| `transfers_never_used.json`, `getaccount_never_used.json` | The same two requests for the never-used `TJwwz9NR37hjXdAV5gowj7src4avMuZZNW` |

Tests build further histories with `transfer_row` in `tests/conftest.py`, in the shape of these
recordings. Tests that run the command line on the real clock shift every history, unchanged
in shape, to just before the current date so it stays inside the 180-day lookback.

## Envio HyperSync (`hypersync/`)

Recorded on 2026-09-29 from `https://bsc.hypersync.xyz` with the owner's free-plan token, by
amlcheck's own client (`src/amlcheck/adapters/hypersync.py`), so each request is exactly one it sends.

| File | Request |
|---|---|
| `height.json` | `GET /height` |
| `block_time.json` | `POST /query` for one block's header (`include_all_blocks`) |
| `transfers.json` | `POST /query` for the USDT transfers of the Binance wallet `0x8894…d4e3` over 30 blocks: 20 transfers in 16 blocks |
| `transfers_none.json` | The same query for a random, never-used address: `"data": []` |
| `first_activity.json` | `POST /query` from block 0 for the first transaction or transfer of `0x6b01…c4eb`: found in block 63,682,798 |
| `error_no_token.json` | `POST /query` without a token: HTTP 401 |

None of them contains the token, which travels in a header. Tests build BSC histories with
`bsc_transfer` and `HyperSyncMock` in `tests/conftest.py`, in the shape of these recordings.

## Eagle Virtual (`eagle_virtual/`)

Data from Eagle Virtual, https://eaglevirtual.com/license. Their API description
(`https://eaglevirtual.com/v1/openapi.json`, version 1.2.0) is not copied here, because it is
their document.

| File | Request |
|---|---|
| `check_clear.json` | `GET /v1/check/TJwwz9NR37hjXdAV5gowj7src4avMuZZNW`, a never-used address |
| `check_frozen_tron.json`, `address_frozen_tron.json` | `GET /v1/check/…` and `GET /v1/address/…` for `TAQM43owNJLZz3vh3PXxBu2qTWf2McMQwJ` |
| `check_frozen_evm.json`, `address_frozen_evm.json` | The same for the Lazarus Group address `0x098B…2f96`. The record is trimmed to its 7 newest restriction records and 4 chains of coverage |
| `usage.json` | `GET /v1/usage` |
| `error_invalid_key.json` | The body of a `401` for an unknown key |

**Derived, not recorded:** today's live data offered no released or unvouched address, so
these three were made from the recordings above, changing only the fields named.

| File | Made from | Changed |
|---|---|---|
| `check_unfrozen_tron.json` | `check_frozen_tron.json` | `verdict` is `UNFROZEN` |
| `address_unfrozen_tron.json` | `address_frozen_tron.json` | `verdict` is `UNFROZEN`, plus one release record (`RemovedBlackList`, `is_release: true`) with a made-up block, time and transaction |
| `check_unvouched.json` | `check_clear.json` | `verdict` is `null` with `verdict_reason: "coverage_unvouched"`, and Polygon is listed in `not_vouched_for` as `scan behind`, using the fields the API spec defines |
