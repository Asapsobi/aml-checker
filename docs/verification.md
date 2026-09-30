# Phase 0 verification report

> **Checked:** 2026-09-28, with V11–V14 added on 2026-09-29 and V15–V16 on 2026-09-30 · **By:** AI coding agent, for review by Sobi · **Against:** PRD v0.1

This report checks every item that PRD §6 marks **verify**, plus the provider facts that §2 and §11
rely on. Each item says where it was checked, what was found and what it changes in the build.
Providers change: re-check an item before a later phase depends on it.

## Summary

| ID | Item | Result | Status |
|---|---|---|---|
| V1 | OFAC SDN download | Official Sanctions List Service URLs work without a key. The list published 2026-09-23 holds 1,059 digital currency addresses | Confirmed |
| V2 | Chainalysis free sanctions API | The API still answers, but its sign-up page now leads to a paid product | **Not available to new users** |
| V3 | OpenSanctions licence | Free for non-commercial use only | Confirmed, not free for this project |
| V4 | Eagle Virtual | API, Free plan limits and licence terms match the PRD. BSC (BNB Chain) and TRON are both covered | Confirmed |
| V5 | TRON USDT contract and events | Address, the three event names and `isBlackListed` confirmed live | Confirmed |
| V6 | BSC USDT freeze capability | The contract has no freeze, blacklist or pause function | **Finding: it cannot freeze** |
| V7 | TronGrid | Endpoints confirmed. Limits are set per key and not published | Confirmed |
| V8 | BSC chain data | Etherscan V2 is paid-only for BSC, and BscScan's API is retired. The history comes from Envio HyperSync's free plan (V12) | Decided (Q4) |

## V1. OFAC SDN list

**PRD:** verify the current download URL; match on the address string whatever the currency label;
store the snapshot hash and publish date.

**Checked:**

- `https://sanctionslistservice.ofac.treas.gov/api/PublicationPreview/exports/SDN.XML` redirects
  (302) to a short-lived signed S3 URL and downloads without a key: 29,089,607 bytes,
  `Last-Modified: Wed, 23 Sep 2026 14:07:28 GMT`, sha256 `d533a38e…01654227`.
- The file states its own publication: `<publshInformation><Publish_Date>09/23/2026</Publish_Date>`
  `<Record_Count>19391</Record_Count>`. The spelling `publsh` is OFAC's, and the date is `MM/DD/YYYY`.
- `SDN_ADVANCED.XML` from the same service is 127,051,135 bytes and defines the same 20
  "Digital Currency Address" types. It arrived at about 190 KB/s and a 5-minute download timed out
  halfway.
- Daily changes are published too: `https://sanctionslistservice.ofac.treas.gov/changes/latest`
  redirects to `DeltaArchive/2026-09-23_delta.xml`.

**How addresses appear:** inside each `<sdnEntry>` (with its `<uid>` and `<programList>`) as
`<id><idType>Digital Currency Address - TRX</idType><idNumber>T…</idNumber></id>`.
The 2026-09-23 list holds:

- 1,059 addresses (1,043 unique) on 99 entries, under 20 currency labels.
- 334 TRON addresses: 254 labelled TRX, 79 labelled USDT and 1 labelled XBT.
- 133 `0x` addresses (124 unique). Only one is labelled BSC; most are labelled ETH, 8 USDT,
  and 1 each USDC, ARB and ETC.

**The label cannot be trusted**, which confirms the PRD's rule:

- Mingming WANG (uid 45404) has TRON address `TUCsTq7TofTCJRRoHk6RvhMoS2mJLm5Yzq` filed under XBT.
- 7 USDT-labelled addresses are Bitcoin-format (USDT on Omni): SUEX, Chatex, Garantex Europe.
- The only BNB-labelled address is a retired BNB Beacon Chain `bnb1…` address, not BSC.

**Checksums:** 69 of the 133 `0x` entries are mixed case, and all 69 pass EIP-55. All 334 TRON
addresses pass base58check.

**What this changes:**

- Match on the address alone. For a BSC check, every `0x` address on the list counts, whatever its
  label.
- When loading the list, lowercase `0x` addresses. Keep an entry whose checksum fails and log it,
  because a typo in the list must not hide a sanctioned address. User input stays strict (AT-10).
- The tool downloads `SDN.XML` by default (decision D1).

## V2. Chainalysis free sanctions screening API

**PRD:** optional second opinion; verify the current sign-up and terms.

**Checked:**

- `GET https://public.chainalysis.com/api/v1/address/{address}` with an invalid `X-API-Key` answers
  `401 {"message":"Invalid API Key"}`, so the service is still running.
- The documented sign-up page `https://go.chainalysis.com/crypto-sanctions-screening.html` now
  redirects (301) to `https://www.chainalysis.com/product/address-screening/`. That page describes
  the paid Address Screening product and only offers "Request a demo".
- The API reference at `auth-developers.chainalysis.com/sanctions-screening/...` returns
  "Page Not Found".

**What this changes:** a new free key cannot be obtained. Drop this source unless you already
hold a key (Q6).

## V3. OpenSanctions

**PRD:** free for non-commercial use only; verify the licence before commercial use.

**Checked** (`https://www.opensanctions.org/licensing/`): the data is licensed CC BY-NC 4.0. Commercial
use needs one of three paid options: the Screening API (pay as you go, 30-day trial), a Screening
License (flat rate, internal use) or a Reseller License. No prices are published.

**What this changes:** screening for an OTC settlement business is commercial use, so this source
stays out unless a licence is bought.

## V4. Eagle Virtual

**Checked** against the public spec `https://eaglevirtual.com/v1/openapi.json` (version 1.2.0) and
`https://eaglevirtual.com/license`:

| PRD claim | Result |
|---|---|
| `GET /v1/check/{address}` returns `CLEAR / FROZEN / SEIZED / UNFROZEN` | Confirmed. A live seizure outranks a live freeze, which outranks a lifted one |
| `null` with a reason when a chain is behind | Confirmed: `verdict: null` with `verdict_reason: "coverage_unvouched"`, and `coverage.not_vouched_for[]` names each chain (`never scanned` or `scan behind`). The spec states "The API never says CLEAR over a gap" |
| Free plan: 1 key, 1,000 checks a day, 1 request a second | Confirmed |
| Attribution line required | Confirmed: the licence calls crediting "a condition of that license rather than a request". The exact text arrives in the `x-ev-credit-line` header of every Free plan answer, so the tool should print that header rather than a fixed string |
| Bearer key starting `ev_` | Confirmed: `Authorization: Bearer ev_live_…` |
| Spec is public | Confirmed |
| Business plan for event lists and volume | Confirmed: 5 keys, each 25,000 checks a day at 10 a second. Event rows need Business or Enterprise |
| Resale or bundling needs a written agreement | Confirmed. So do bulk republishing, building a dataset and training a model. Bundling means "building it into another product or data feed you supply", which matters for PRD Q4 |
| BSC appears in `GET /v1/chains` | Confirmed with the owner's key: `{"chain_id": "56", "name": "BNB Chain", "vouched_for": true}`, scanned to the current block. TRON is covered as `"Tron"` (chain id `1000000000195`). 41 chains in all, every one vouched for at the time |

**Checked live** with the owner's Free plan key, using 4 of the day's 1,000 calls:

- The credit line reads `Data from Eagle Virtual, https://eaglevirtual.com/license`.
- `GET /v1/usage` returns `plan`, `calls_today`, `daily_limit`, `requests_per_second`,
  `credit_line_required` and `credit_line`. It costs nothing and resets at midnight UTC. On the Free
  plan the counter is per account and shared with Eagle Virtual's MCP server. `GET /v1/chains` costs
  one call. `amlcheck status` can show the usage.
- `GET /v1/check` returns `address`, `address_display`, `address_family` (`evm`, `tron`, `solana` or
  `stellar`), `verdict`, `verdict_reason`, `as_of` and `checked_at` (UTC seconds), `record_count`,
  `coverage` and `url`, as the spec says.
- The TRON address Tether blacklisted on 2026-09-27 (`TAQM43owNJLZz3vh3PXxBu2qTWf2McMQwJ`) already
  reads `FROZEN` with one record. The data is at most a day behind the chain.
- A `0x` address is checked as a family: one call answers for every EVM chain (Q2). The Lazarus Group
  address reads `FROZEN` from 122 records on 25 chains. They come from many issuers and tokens, not
  only Tether and Circle: for example Coinbase (cbBTC), Bridge (pathUSD) and AllUnity (EURAU). On BNB
  Chain it has freezes of AUSD (Agora), XUSD (StraitsX) and USD0 (Usual). Other stablecoins on BSC
  can freeze even though BEP20 USDT cannot.
- `coverage` counts all 41 chains for every address, TRON ones included. A verdict is `null` only
  when nothing is recorded and some chain could not be vouched for. By Q2, a lagging chain therefore
  makes an unrecorded address INCOMPLETE even if that chain is unrelated. Phase 1 should log how
  often this happens.
- `GET /v1/address/{address}` adds `restriction_records`, one per event, with `chain_id`,
  `chain_name`, `token` (symbol, name, contract), `company`, `event` (signature, kind, category,
  `is_seizure`, `is_release`), `block_number`, `tx_hash` and `observed`. It also gives per-chain
  coverage with freeze, unfreeze and seize counts, and a `limitations` link. These fields supply the
  evidence for R-FRZ findings, at one extra call and only when the verdict is not CLEAR.

## V5. TRON USDT contract and blacklist events

**Checked** through TronGrid without a key:

- `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` is `TetherToken` (`POST /wallet/getcontract`), symbol
  `USDT`, 6 decimals.
- The ABI has exactly the events the PRD names: `AddedBlackList(address indexed _user)`,
  `RemovedBlackList(address indexed _user)` and
  `DestroyedBlackFunds(address indexed _blackListedUser, uint256 _balance)`.
- The read functions `isBlackListed(address)` and `getBlackListStatus(address)` both return a bool.
- All three events occur on chain (`GET /v1/contracts/{contract}/events?event_name=…`). The latest
  `AddedBlackList` was on 2026-09-27 at block 86,613,172, `RemovedBlackList` on 2026-09-25 and
  `DestroyedBlackFunds` on 2026-09-16.
- Spot check (`POST /wallet/triggerconstantcontract`): `isBlackListed` returns true for an address
  blacklisted on 2026-09-27.

**Not in the PRD:**

- **The contract can be retired.** It has `deprecate(address)`, `deprecated()`, `upgradedAddress()`
  and a `Deprecate` event. Today `deprecated()` is false. If Tether ever upgrades, the blacklist moves
  to the new contract, so the indexer must check `deprecated()` on every sync and report an `error`
  (making the verdict INCOMPLETE) rather than read a stale list.
- The contract can also pause every transfer (`pause()`). It is not paused today.
- TronGrid gives event addresses in `0x` hex form without the `41` prefix. They must be converted to
  `T…` base58 before matching.
- The USDT contract address is itself blacklisted.

Recorded responses are in `tests/fixtures/tron/`.

## V6. BSC USDT freeze capability

**PRD:** BEP20 USDT is Binance-Peg, not issued by Tether. Find out whether it can freeze or blacklist
at all, and don't assume parity with TRON.

**Checked** on a public BSC node (`https://bsc-dataseed.bnbchain.org`, chain id 56) without a key:

- `0x55d398326f99059fF775485246999027B3197955` reports `name()` "Tether USD", `symbol()` "USDT" and
  **`decimals()` 18**. TRON USDT uses 6, which matters for amounts in Phase 2.
- **It is not a proxy.** The EIP-1967 implementation and beacon slots and the older OpenZeppelin slot
  are all empty, so its code cannot be swapped.
- **Its complete function list** was read from the deployed bytecode: 20 functions, named through
  `api.4byte.sourcify.dev`. They are the BEP-20 basics plus `mint(uint256)`, `burn(uint256)`,
  `getOwner()`, `owner()`, `renounceOwnership()` and `transferOwnership(address)`. That is Binance's
  standard BEP20Token template.
- **None of 14 freeze, blacklist or pause functions** (Tether's, Circle's and common variants) is
  present, and the code emits no blacklist event. The same scan run on TRON USDT finds all five of
  its blacklist functions and all three events, so the method works.

**Conclusion: BEP20 USDT cannot freeze, blacklist or seize an address.** Its only privileged
functions mint or burn supply and manage ownership, and none of them takes a holder's address.

Reproduce with `uv run scripts/verify_bsc_usdt.py`.

**What this changes:** on BSC, rules R-FRZ-01 and R-FRZ-02 have nothing to check at the token level.
PRD §14 already asks for freeze coverage to be shown per chain. How the verdict should treat it is
Q1.

## V7. TronGrid

**Checked** (`https://developers.tron.network/reference/rate-limits` and `/reference/api-key`):

- The API key goes in the `TRON-PRO-API-KEY` header.
- No fixed limits are published. The docs say "Do not hard-code fixed limits into business logic".
  Limits are set per key in the TronGrid console, and requests without a key "may be limited by IP".
- A rate-limited request answers 429 **or 403**.

**Endpoints**, all answering without a key on 2026-09-28. The owner's key was also accepted (HTTP 200),
and no rate-limit headers came back:

- `GET /v1/accounts/{address}/transactions/trc20?contract_address=…` returns `transaction_id`,
  `block_timestamp` (ms), `from`, `to`, `value`, `type` and `token_info`. **It has no block number.**
  Evidence built from it carries the transaction ID and time; the block needs a separate lookup.
- `GET /v1/contracts/{address}/events?event_name=…` returns rows with `block_number`,
  `block_timestamp`, `transaction_id`, `event_index` and `result`, paged with `meta.fingerprint`.
- `POST /wallet/triggerconstantcontract` makes read-only contract calls.

**What this changes:** treat a TronGrid 403 like a 429, and read the rate limit from config rather
than code.

## V8. BSC chain data

**Checked:**

- The Etherscan V2 chain list (`https://docs.etherscan.io/supported-chains`) marks BNB Smart Chain
  Mainnet (56) **"Paid Tier Only"**. Only the source code and ABI endpoints are free on every chain.
- A live call, `https://api.etherscan.io/v2/api?chainid=56&module=account&action=tokentx…`, answers
  "Free API access is not supported for this chain. Please upgrade your api plan for full chain
  coverage."
- `https://api.bscscan.com/api` answers 301. BNB Chain's own blog (9 December 2025) confirms the
  BscScan API was merged into Etherscan V2 with no free tier for BNB Chain. It points to
  BSCTrace/MegaNode (NodeReal) as an alternative with a free tier; there, transfer history is the
  JSON-RPC method `nr_getAssetTransfers`, with the key in the URL path. That alternative was not
  checked further.
- PublicAML, the API behind `checker.py`, also serves BSC transfers and counterparties. Without a key
  it allows 20 addresses an hour on those endpoints.

**What this changes:** Phase 2 needs a BSC data source to be chosen (Q4). Chosen on 2026-09-29:
Envio HyperSync (V12).

## V9. Checked before building Phase 1

**Checked** on 2026-09-28 with the owner's keys, before the code relied on these facts:

- **TronGrid keeps the whole USDT blacklist history.** The first `AddedBlackList` and
  `DestroyedBlackFunds` events date from 2020-06-26 (block 20,972,943), and the first
  `RemovedBlackList` from 2021-08-01.
- **Paging reaches every event.** Paging 200 at a time through `meta.links.next` reaches the newest
  event with no duplicates: 8,628 `AddedBlackList`, 970 `RemovedBlackList` and 1,205
  `DestroyedBlackFunds` events. That is 10,803 events in 56 pages, fetched in about 24 seconds.
- **The index can stick to confirmed blocks.** `only_confirmed=true` and `min_block_timestamp`
  filter the event list, and `POST /walletsolidity/getnowblock` returns the latest confirmed block,
  which ran 19 blocks (about a minute) behind the latest block.
- **Eagle Virtual's clean answer and errors are as documented.** A never-used address comes back as
  `verdict: "CLEAR"` with `record_count: 0`. Answers carry a `ratelimit-policy: 1000;w=86400` header
  but no count of calls left; `/v1/usage` gives that. The spec lists 400 (not an address, costs no
  call), 401 (missing or invalid key), 403 (unknown plan), 429 (over the limit, with Retry-After) and
  503 (the record is unavailable: "We do not answer from stale data").
- **Two sources confirm each other.** For the address Tether blacklisted on 2026-09-27, Eagle
  Virtual's `/v1/address` record and TronGrid's `AddedBlackList` event name the same transaction and
  block.

## V10. Checked before building Phase 2

**TronGrid**, checked on 2026-09-28 with the owner's key:

- **Transfer history has the filters the scan needs.** `GET /v1/accounts/{address}/transactions/trc20`
  filters by `contract_address`, `only_confirmed`, `min_timestamp` and `order_by`, and pages 200 rows
  at a time through `meta.links.next`. Each row has `transaction_id`, `block_timestamp`, `from`,
  `to`, `value`, `type` and `token_info` (6 decimals). A never-used address answers
  `{"data": [], "success": true}`.
- **Creation time comes from the node API.** `GET /v1/accounts/{address}` has no creation time.
  `POST /wallet/getaccount` has `create_time` (in ms), and answers `{}` for an address that was
  never activated.
- **USDT does not need activation.** `TAQM43owNJLZz3vh3PXxBu2qTWf2McMQwJ`, frozen on 2026-09-27, has
  no `create_time` but six USDT transfers. First activity is therefore the earlier of `create_time`
  and the first USDT transfer (`order_by=block_timestamp,asc`, `limit=1`).
- **The exit-criterion fixture is real.** `TAjoXRsomrsDDCXsxD1ELFQu4wHfF9HZSv` was activated on
  2026-09-24. On 2026-09-26, the day before Tether froze `TAQM43ow…`, it received 500,000 USDT from
  it, and it passed 3,000,050 USDT on within minutes.
- **A busy address is read quickly.** The newest 5,000 transfers of a Bybit hot wallet took 25
  pages and about 20 seconds.

**NodeReal**, the first choice for Q4, checked on 2026-09-28:

- **Every request covers at most 100,000 blocks**, with or without an address filter. The docs for
  `nr_getAssetTransfers` (250 compute units a call) say: "If both fromBlock and toBlock are provided,
  their range must be no more than 100000 blocks".
- **That is only 12.5 hours of BSC.** BSC makes a block every 0.45 seconds (measured over its last
  1,000,000 blocks), so 180 days take 345 requests in each direction.
- **Neither plan is fast enough.** The pricing page gives the Free plan 10,000,000 compute units a
  month at 150 a second (the docs also say 100M and 300). The Growth plan costs $31 a month for 700
  a second. A check would take about 20 minutes on Free and about 4 on Growth.
- **So NodeReal does not hold up,** and Q4's fallback applies.

**Etherscan**, the fallback: the cheapest plan that covers BNB Smart Chain is Lite, at $49 a month,
with 5 calls a second and 100,000 a day. Its docs list `module=account&action=tokentx` with
`address`, `contractaddress`, `startblock`, `endblock`, `page`, `offset` and `sort`, returning
`blockNumber`, `timeStamp`, `hash`, `from`, `to`, `value` and `tokenDecimal` among others. This is to
be checked live once a key exists.

## V11. Etherscan, for BSC

**No longer used.** The owner chose not to buy the Lite plan, so the Etherscan code was removed on
2026-09-29 (Q4, D21). These facts are kept for reference.

**Checked** on 2026-09-29 with the owner's key, which is still on the free plan. It works on Ethereum
and refuses BSC with "Free API access is not supported for this chain. Please upgrade your api plan
for full chain coverage." The V2 API is the same on every chain, so its behaviour was checked on
Ethereum. BSC itself is still to be checked once the Lite plan is active.

- **Rows have no log index.** `module=account&action=tokentx` rows carry `blockNumber`, `timeStamp`
  (in seconds), `hash`, `from`, `to`, `value`, `tokenDecimal` and `contractAddress`, among others.
  Two identical transfers in one transaction therefore cannot be told apart.
- **"No transactions found" is an answer, not an error.** An address without transfers comes back as
  `status "0"`, `message "No transactions found"`, `result []`.
- **Results stop at 10,000 per query:** "Result window is too large, PageNo x Offset size must be
  less than or equal to 10000". On a busy address, one page of 1,000 ended with 53 transfers in the
  same block.
- **Two helper calls exist.** `module=block&action=getblocknobytime&closest=after` turns a time into a
  block number, and `action=txlist&sort=asc&offset=1` gives an address's first transaction.
- **BSC is past Etherscan's example end block.** The examples use `endblock=99999999`, but BSC is
  past block 124 million, so a fixed end block would silently drop every recent transfer. The first
  request therefore has no end block.

**What this changes:**

- **Paging moves the end block.** The BSC history reads pages of 1,000, newest first, and moves the
  end block down instead of turning pages. The block at a page's edge is dropped and read again
  whole with the next page, which works at any volume.
- **The key stays out of errors and logs.** It travels in the query string, so no error text
  includes a URL, and httpx's own request log is kept quiet.

## V12. Envio HyperSync, for BSC

**Checked** on 2026-09-29 with the owner's free-plan token, against raw chain logs from SQD's public
portal (V13).

- **Access.** `POST https://bsc.hypersync.xyz/query`, also served at `https://56.hypersync.xyz`, with
  `Authorization: Bearer <token>`. Without a token the answer is HTTP 401, "Your token is malformed.
  API Tokens can be created at https://app.envio.dev/api-tokens". `GET /height` answers
  `{"height": N}`, with or without a token.
- **Limits and terms.** The free plan has "fair-use based rate limiting". Every answer carried
  `x-ratelimit-cost: 0`. The docs say an exhausted budget answers 429 with `x-ratelimit-*` headers.
  Envio's terms of service cover HyperSync: UK law, and the data comes "as is".
- **Answers.** `{"data", "archive_height", "next_block", "total_execution_time", "rollback_guard"}`.
  `data` is a list holding one batch of `blocks` and `logs` (and `transactions` when asked for), or
  `[]` when nothing matched. **Block timestamps are hex strings** (`"0x6abb9411"`), block and log
  numbers are integers, and topics come as 32-byte hex in `topic1` and `topic2`.
- **Paging.** An answer stops at a time or size limit, near 1,000 transfers or after a few seconds of
  work, and `next_block` says where to go on. At the end of the data, `next_block` is
  `archive_height + 1`. `max_num_logs` stops an answer early, but only between blocks.
- **Complete.** Of 20 real USDT transfers picked at random from SQD's raw logs across April to
  September 2026, all 20 were found. PublicAML found 5 of 10 (V13). Rows matched the raw logs field
  for field (hash, log index, block, time, sender, recipient and amount): 2,106 of 2,106 for a
  Binance wallet, and every sampled window of a quiet wallet.
- **Fast.** A quiet wallet's 180 days: 81 transfers in 2 requests, 2.5 seconds. A scan from block 0
  for the first transaction or transfer: 0.3 to 6 seconds for an active address, 8 to 21 for a
  never-used one. The newest 5,000 transfers of a Binance hot wallet: 13.5 seconds.
- **Oldest first only.** A query has no descending order. HyperSync's own client reads "in reverse"
  by querying block windows from the head down.
- **DNS.** For a few minutes on 2026-09-29, `bsc.hypersync.xyz` did not resolve (SERVFAIL from
  8.8.8.8 and 4.2.2.4, while 1.1.1.1 answered). `56.hypersync.xyz` worked throughout.

**What this changes:**

- The BSC history is read oldest first. Once the part read shows more than `max_transfers` in the
  window, the newest are read instead, window by window from the head down (D22).
- The window's first block comes from the chain's pace, checked against block headers, so no
  transfer inside the lookback is missed. It lands about 1,000 blocks (7.5 minutes) early.
- First activity takes one scan (D23).
- An answer that makes no progress, or has no `next_block`, is an error, so the check is INCOMPLETE.
- `[bsc] hypersync_url` can point at `https://56.hypersync.xyz` if the other name fails.

## V13. Free BSC sources without an account

**Checked** on 2026-09-29, after the owner declined Etherscan's Lite plan and an Alchemy account.
None of these gives a complete 180-day history without an account:

| Source | Finding |
|---|---|
| SQD public portal (`portal.sqd.dev`, no key) | Accurate raw logs, but each answer covers only about 2,000 BSC blocks: a quiet address's 180 days take about 17,000 requests, and it answers 529 "overloaded" every few calls. Used here as the reference for checking other sources |
| 16 public BSC RPC nodes | BNB Chain's own nodes refuse `eth_getLogs` ("limit exceeded"). PublicNode and 48 Club allow 5,000 blocks a call, and PublicNode needs a personal token for older ranges. 1RPC allows 50. The rest are paid, blocked or down |
| Routescan | "chain not supported" for chain 56 |
| Etherscan without a key | The same refusal as the free plan |
| Ankr | Needs a key |
| Envio HyperSync without a token | HTTP 401 |
| PublicAML `POST /v1/address-transactions` | No key needed (20 requests an hour), and its rows matched the raw logs. But it **missed 5 of 10** real transfers picked at random: an answer can say `has_more: false` before reaching the dates asked for (`index_exhausted: false`), and a wallet's feed can lack a transfer that the other party's feed lists. Before about March 2026 it also lists some transfers twice (`edge_type: "internal"`), and its `timestamp` is a string where its spec says integer |

**What this changes:** the owner created a free Envio account, and Q4's answer is HyperSync (V12).

## V14. Checked before building Phase 3

**Checked** on 2026-09-29.

- **Web stack.** The current PyPI releases:
  - FastAPI 0.141.1 (MIT)
  - Starlette 1.7.0 (BSD-3-Clause)
  - Jinja2 3.1.6 (BSD)
  - python-multipart 0.0.32 (Apache-2.0)
  - uvicorn 0.54.0 (BSD-3-Clause)

  Starlette 1.x takes the request as the first argument of `TemplateResponse`. Its test client warns
  that using `httpx` there is deprecated, so the tests call the app through `httpx.ASGITransport`.
- **HTMX.** The newest release, 4.0.0 (2026-08-28), is a new major version. The 2.x line is still
  maintained, and 2.0.10 (2026-09-06) is its newest release.
  - amlcheck ships 2.0.10 taken from the npm registry. The tarball matches the registry's published
    SHA-512, and `htmx.min.js` has SHA-256 `71ea6718…c0de`.
  - Its licence is 0BSD.
- **PDF.** ReportLab 5.0.1 (BSD) makes the PDF.
  - fpdf2 2.8.8 is LGPL-3.0, which would add obligations to Phase 5's single-binary packaging.
  - Tests read the PDF back with pypdf 6.19.0 (BSD-3-Clause, tests only).
  - ReportLab's types come from types-reportlab.
- **Explorer links.** Tronscan's own front end (`tronscan/tronscan-frontend`, `src/routes.js`) routes
  `/transaction/:hash` and `/address/:id`, behind `#/`. BNB Chain's repositories link BscScan as
  `https://bscscan.com/tx/…` and `/address/…`. Both sites sit behind Cloudflare's bot check, so they
  were not fetched, and amlcheck never fetches them.
- **macOS notifications.** `/usr/bin/osascript` with `display notification` works on macOS 26.6.2.
  - The text is passed as arguments to an `on run argv` script, so it is never read as AppleScript.
  - macOS lists these notifications under Script Editor.
- **Scheduling.** launchd.plist(5) documents `Label`, `ProgramArguments`, `WorkingDirectory`,
  `StartCalendarInterval`, `StandardOutPath` and `StandardErrorPath`. launchctl(1) documents
  `bootstrap`, `bootout` and `kickstart`.
  - Unlike cron, launchd runs a job missed while the computer slept as soon as it wakes.
  - The example in docs/scheduling.md passes `plutil -lint`.

**What this changes:** the web page, the PDF export and the watchlist alerts are built on these
(D24–D32).

## V15. Checked while building Phase 4

**Checked** on 2026-09-30.

- **HyperSync's free plan is 30 queries a minute.** Every answer carries its budget:
  - `x-ratelimit-limit: 30000, 30000;w=60` (units per 60-second window)
  - `x-ratelimit-cost: 1000` (units per query)
  - `x-ratelimit-remaining`, and `x-ratelimit-reset` (seconds)

  On 2026-09-29 the same answers said `x-ratelimit-cost: 0`. A refused query is HTTP 429 with an
  empty body and no `Retry-After`.
- **A 2-hop walk broke that budget.** Its first live run, on `0xd5efbbd7…1e36`, had 6 of 7
  counterparty reads refused with 429. Each read asked about 10 queries: the block at the start of
  the lookback, and a whole-chain scan for first activity, which the walk does not use.
- **After the fix, the same walk worked** (D33). It took about 70 seconds: 1 counterparty flagged
  itself (R-EXP-01), 5 hubs and 1 read, with nothing flagged behind it. The graph drew it on the web
  page in dark mode.
- **Look-alike addresses.** Two of those hubs, `0x6990e7e9…bb3e4` and `0x6990b3b0…3b3e4`, differ
  only in the middle, the pattern of address poisoning. Graph labels of 6 + 4 characters showed them
  the same, so labels show 8 + 6.
- **Graph colours.** Checked with the dataviz palette validator: the reference status colours
  (critical `#d03b3b`, warning `#fab219`), the series blue and a neutral grey.
  - Every pair stays apart under colour blindness: ΔE ≥ 9.9 in both light and dark.
  - Normal-vision separation is ΔE ≥ 17.2.
  - All clear 3:1 contrast on the dark surface. On the light surface, amber does not, by design;
    the marks and the legend carry the meaning.

**What this changes:**

- HyperSync paces itself to its budget (D33).
- The block at the start of the lookback is found once an hour.
- The 2-hop reads skip the first-activity lookup.
- Graph labels are longer.

## V16. Checked before building Phase 5

**Checked** on 2026-09-30.

**Eagle Virtual, for key rotation.** Its spec, version 1.3.0, says:

- "The Business plan has 5 keys, each with 25,000 calls a day and 10 a second."
- `GET /v1/usage`: "On the Business and Enterprise plans the counter is the key's own. On the Free
  plan it is the account's", shared with its MCP server. So several keys help only on a paid plan.
- 429 is "Over your plan's limit. Retry-After says how long to wait." The body is not documented.
- Answers to a Business or Enterprise key carry no `x-ev-credit-line`: those plans owe no credit
  line. `/v1/usage` says so in `credit_line_required`.

The pricing page adds that a key whose checks for the day are used up gets no answers until
midnight UTC, "and it says so". The terms say credentials may not be shared "except as expressly
permitted by your plan or written agreement". The licence says its indexing permission does not
extend to getting around rate limits.

Not checked live: the answer of a key whose day is used up, which would spend the day's 1,000
calls. Rotation is tested with mocks built only from the words above (D40).

**Idempotency.** The IETF draft "The Idempotency-Key HTTP Header Field" is at revision 07
(October 2025). It is not an RFC yet, and this revision expired on 2026-04-18. Its rules:

- The key is a structured-field String, such as `"8e03978e-…"`. A UUID is recommended.
- A retry after the first request completed gets that request's result.
- A retry while it is still running gets 409.
- Reusing a key with a different payload gets 422.
- The server publishes the key's format and when keys expire.

**Packaging, with uv 0.11.16:**

- `uv build` makes a wheel that holds the migrations, templates and static files.
- `uv tool install` works from that wheel, and from `git+https://github.com/Asapsobi/aml-checker`,
  which built main at `f375af9`. The installed `amlcheck` created a new database at schema v4 and
  refused to serve the API without a token.
- pipx 1.17.8 (run through uvx, with Python 3.13.15) installs the same wheel.

**The API, live** on the Mac with the owner's keys, in a scratch amlcheck home:

- `scripts/corridor_mock.py` screened `TA3941uF…86mz` (on the OFAC list): BLOCK in 2.8 seconds.
- It screened `0xd5efbbd7…1e36`: REVIEW in 8.9 seconds (R-EXP-01, R-EXP-02, R-HEU-02).
- The same request again, with the address in capitals and the amount as a number, was a replay of
  the same check in 0.01 seconds. The same key with another amount was refused with 422.
- The audit log verified, with no record for the replay. A request without the token got 401.

**What this changes:**

- A paid plan's keys take turns, and only the first key may be on the Free plan (D40).
- The API follows the draft (D41).
- amlcheck installs as a tool from GitHub (D44).

## Decisions

These are easy to reverse. Say if you want any of them changed. D1–D5 were taken in Phase 0,
D6–D12 in Phase 1, D13–D23 in Phase 2, D24–D32 in Phase 3 and D33–D38 in Phase 4.

| # | Decision | Why |
|---|---|---|
| D1 | OFAC source is `SDN.XML`, not `SDN_ADVANCED.XML` | Same 20 digital currency address types at a quarter of the size. Change it with `[ofac] sdn_url` |
| D2 | Data, config and logs live in `~/.amlcheck/` (override with `AMLCHECK_HOME`). Config is `~/.amlcheck/config.toml` (override with `AMLCHECK_CONFIG`). Keys come from the environment, then `./.env`, then `~/.amlcheck/.env` | The PRD fixes the database location but not the rest |
| D3 | Two columns added to the §9 schema: `checks.seq` and `check_findings.observed_at` | `seq` fixes the order of the hash chain. `observed_at` is required by §5.2 but missing from §9 |
| D4 | `mypy --strict` covers the whole package, not only `core/` | Cheap while the code is small |
| D5 | CI runs on Ubuntu and macOS, with Python 3.12 and 3.14 | The oldest supported and newest Python, on the two platforms §11 requires |
| D6 | `amlcheck check` exits 0 for NO_HITS, 3 for REVIEW, 4 for INCOMPLETE, 5 for BLOCK, and 1 when the check could not run | A script can stop on anything but NO_HITS |
| D7 | Every TRON check first refreshes the blacklist index, which takes a few requests and about a second. `amlcheck sync tron-index` is needed only once, to build it. The index goes stale only when it cannot be refreshed for over an hour | The history stays current without a scheduler |
| D8 | A `Retry-After` longer than 10 seconds is not waited out: that source is reported as failed and the check is INCOMPLETE. Change the limit with `[network] max_retry_after_seconds` | Meets AT-07 without stalling the operator |
| D9 | Eagle Virtual's full record (`/v1/address`, one more call) is read only when the verdict is not CLEAR. Answers are cached for 15 minutes, but an answer with a gap is never cached | Every freeze finding names its chain, token and transaction, at the lowest cost in calls |
| D10 | A listed address that fails its checksum is kept, and a warning is logged (V1). The downloaded XML is parsed with `defusedxml` | A typo on the list must not hide an entry, and the parser refuses XML attacks |
| D11 | Migration 0002 adds the listed entity's name, a snapshot's address count, the balance destroyed by `DestroyedBlackFunds`, the time of the index's last block, and the summaries shown to the operator | Evidence in plain words, the §14 sanity check, and an audit hash that covers what the operator saw |
| D12 | `audit verify` prints the latest record hash, to keep a copy elsewhere | A hash chain cannot show records cut off its end from inside the file |
| D13 | Transfers of 0 USDT are left out of the history | On TRON anyone can send them to any address (address poisoning): spam, not dealings |
| D14 | An address with no activity at all counts as new (R-HEU-01) | It is as new as an address can be, so NO_HITS would hide that |
| D15 | First activity is the earlier of the account's activation and its first USDT transfer | An address can move USDT without ever being activated (V10) |
| D16 | R-EXP-02's "flagged sources" are those R-EXP-01 flags: sanctioned or frozen. Labels count only for R-HEU-05 | Keeps the two exposure rules consistent |
| D17 | `labels import` replaces every label, and imports nothing if any row is wrong | The file is the source of truth |
| D18 | The exposure source is required: if the history cannot be read, the result is INCOMPLETE | PRD §0 rule 4 |
| D19 | At most 10 R-EXP-01 and 10 R-HEU-05 findings per check, largest counterparties first | Keeps a busy address's result readable |
| D20 | A finding's priority is kept in its evidence (`"priority": "low"`), not in a new column | Existing audit records keep verifying |
| D21 | BSC transfer history comes from Envio HyperSync's free plan (`HYPERSYNC_API_TOKEN`), and the Etherscan code is removed | Free and complete (V12). The owner declined Etherscan's Lite plan, and no source without an account is complete (V13) |
| D22 | A BSC address that shows more than `max_transfers` in the lookback is read newest first, window by window from the head down, each window read whole | HyperSync reads oldest first, and the newest transfers matter most. The result then matches TRON's: "only the newest 5,000 were read" |
| D23 | First activity on BSC is the first transaction the address sent or received, or the first Transfer log of any token naming it. BNB paid to it by a contract call, which leaves no log, is not counted | The same definition as the Etherscan version had (transactions and token transfers), found in one HyperSync scan |
| D24 | A check's client is part of its record's hash only when it is set | Records written before the column existed keep verifying (Q13) |
| D25 | `batch` checks every row before screening any. It refuses a repeated address and ignores unknown columns | A typo cannot leave a half-screened batch, and a spreadsheet's extra columns do no harm |
| D26 | `batch`, `watch run` and the web page screen one address at a time, sharing one Eagle Virtual rate limiter | Every free plan's rate holds, and the TRON index is never refreshed twice at once |
| D27 | A CSV cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'` | A spreadsheet cannot run it as a formula (OWASP "CSV injection") |
| D28 | A watched address starts from its latest check in the audit log. A first verdict is not a change. `watch run` exits 6 when a verdict changed | Adding an address costs no API call, and the exit status lets a scheduler or script react (Q12) |
| D29 | Eagle Virtual's credit line is kept with each record, and exports print it. Older records get the line V4 recorded | Exports show Eagle Virtual data, and its licence requires the credit (V4) |
| D30 | An export of a log that fails verification is still written. It says so, and the command exits 1 | The operator needs the data to investigate, and a script must not miss the failure |
| D31 | The web page serves only the host names 127.0.0.1 and localhost, needs a start-up token on every POST, sends a strict Content-Security-Policy, and ships HTMX itself | Any page open in the operator's browser can send requests to 127.0.0.1 (DNS rebinding, cross-site requests) |
| D32 | The PDF is set in Helvetica, which covers Latin scripts only | Notes or client names in other scripts may not show in the PDF. CSV and JSON keep them exactly |
| D33 | The HyperSync client paces itself: it reads the budget from every answer and waits for the next window, for up to 65 seconds, rather than being refused. The budget is shared by the whole process. A longer wait is an error, and so INCOMPLETE | The free plan allows 30 queries a minute (V15). D8's 10-second cap would make every 2-hop walk fail |
| D34 | The 2-hop walk reads the 20 largest counterparties. One flagged itself is left to R-EXP-01. One with more than 1,000 transfers is a hub: listed, not read. One that cannot be read makes the result INCOMPLETE | A bounded cost, and 2 hops through an exchange reach almost everyone |
| D35 | R-EXP-03 fires when a counterparty received at least 1,000 USDT from one sanctioned or frozen wallet within the lookback, with flags from local data only (Q16) | The owner's answer to Q16, and no API quota spent on flags |
| D36 | The 2-hop walk runs for `investigate`, for the web form's 2-hop box, and for checks of at least 10,000 USDT (Q15) | Everyday checks stay fast, and large amounts get the deeper look |
| D37 | A vendor is a class named in `[vendor] adapter`, asked only for REVIEW results or amounts of at least `min_amount_usdt`. It adds attribution, is not required, and never changes the verdict (Q14, PRD Q3) | No vendor is paid for now. A paid one can be added without changing amlcheck |
| D38 | The graph is SVG drawn by amlcheck, in the status palette, with a mark for every state and a legend. Labels are 8 + 6 characters. The file version is light; the web version follows the page | No outside code, a strict CSP, colour never alone, and look-alike addresses told apart (V15) |
| D39 | `amlcheck api` serves `POST /v1/check` on 127.0.0.1 only, for a caller on the same machine, with a Bearer token of at least 32 characters from `.env` (Q18). It serves only the host names 127.0.0.1 and localhost, and runs checks one at a time | PRD §10.4. Nothing is opened to the network, and the sources' rate limits hold as in a batch |
| D40 | `EAGLE_VIRTUAL_API_KEY` can hold several keys, separated by commas. A key answering 429 rests for its Retry-After, and the next key is used. A key after the first is used only once `/v1/usage` says its plan is Business or Enterprise (Q19) | The Business plan counts each of its 5 keys separately. The Free plan counts per account, and Eagle Virtual's terms forbid getting around its limits |
| D41 | Idempotency follows the IETF draft (V16). Keys are 8 to 128 characters of letters, digits and `. _ : -`. They never expire, and a request is compared as understood | A corridor can retry after a timeout without a second check or audit record |
| D42 | A replay, and `GET /v1/checks/{id}`, are rebuilt from the audit record, and only after its hash still matches. The vendor's name is kept in its evidence, so a stored check keeps its labels | The audit log stays the one record, and a changed record is never served |
| D43 | Errors are `application/problem+json` (RFC 9457). Every verdict, INCOMPLETE included, is a 200 | A failed source is part of the result, not an HTTP failure |
| D44 | amlcheck installs with `uv tool install` or `pipx install` from the GitHub repo, pinned to a commit or tag, with systemd units for the API and the sync ([server.md](server.md)) (Q20) | Nothing to publish or host, and the server runs the version reviewed |
| D45 | The version is 0.5.0 from Phase 5 on. It had stayed 0.1.0 since Phase 0 | Every audit record keeps `tool_version`, which should tell the Phase 5 code apart |
| D46 | A source result has no `raw_ref`, the pointer to a cached raw response in PRD §7. The evidence itself is kept in the audit record instead: list entry IDs, transaction hashes, blocks, and the provider's own answer fields | Found in the review after Phase 5 and recorded late. The cache keeps answers for 15 minutes, so a pointer into it would soon lead nowhere. Keeping raw answers for good would build a copy of Eagle Virtual's data, which its licence allows only by written agreement (V4) |
| D47 | CI also runs on Windows, with Python 3.12 only | PRD §11 asks for Windows on a best-effort basis. One job shows whether it works, without doubling the matrix |
| D48 | API keys come only from the environment and `.env` files. The OS keyring, which PRD §0 and §8 offer as optional, is not used (Q22) | The owner's answer. It works the same on the Mac and on a headless server, which has no keyring |
| D49 | Every release has its own version, tagged `vX.Y.Z` on main's merge commit and published as a GitHub release. A fix raises the last number (0.5.1), new features the middle one | Every audit record keeps `tool_version`, so records made by different code must never share a version. A server pins the tag |

## Open questions

Per PRD §0 rule 7, these are listed rather than guessed. Answers are recorded below as they arrive.

| # | Question | Needed by | Status |
|---|---|---|---|
| Q1 | BEP20 USDT cannot be frozen (V6). On BSC, should the freeze source report `skipped` ("not applicable"), and can a BSC check then end in `NO_HITS`? | Phase 1 | Decided |
| Q2 | Eagle Virtual answers for a `0x` address across every EVM chain it covers (V4). If Tether froze the same `0x` address on Ethereum, should a BSC check say BLOCK (R-FRZ-01), REVIEW, or ignore it? And if an unrelated EVM chain is behind (`verdict: null`), is the BSC check INCOMPLETE, as §6 reads literally? | Phase 1 | Decided |
| Q3 | "Sanctions snapshot > 48 h old" (§11): is age measured from our last successful download, or from OFAC's publish date? OFAC does not publish daily (the current list is from 2026-09-23), so measuring from the publish date would make most checks INCOMPLETE. | Phase 1 | Decided |
| Q4 | Where should BSC transfer history come from: an Etherscan paid plan, NodeReal MegaNode's free tier, or PublicAML? | Phase 2 | Decided |
| Q5 | Should PublicAML be a source at all? It covers sanctions, issuer freezes, exposure and attribution on both chains, but publishes no terms or licence | Phase 2 | Decided |
| Q6 | The Chainalysis free API is closed to new users (V2). Drop it, or do you already hold a key? | Phase 1 | Decided |
| Q7 | R-HEU-03 and R-HEU-04 give no defaults for K, the window or what counts as a small amount. R-HEU-01 says "REVIEW (low)" and §10.2 prints the severity `low`: is `low` a severity of its own? | Phase 2 | Decided |
| Q8 | §10.1 says audit export is CSV and JSON, with "PDF in Phase 3", but §12 puts all export in Phase 3. Which is it? | Phase 1 | Decided |
| Q9 | PRD §15 Q1: should R-EXP-01 be BLOCK instead of REVIEW? | Phase 2 | Decided |
| Q10 | What happens with an address that has more transfers than a check can read quickly? | Phase 2 | Decided |
| Q11 | How does the "allowlist for own/known wallets" in `labels.csv` (§14) work? | Phase 2 | Decided |
| Q12 | How should `watch run` report a verdict that changed? | Phase 3 | Decided |
| Q13 | The PRD asks for exports "for a client" (U5), but checks record no client. How should a check name its client? | Phase 3 | Decided |
| Q14 | PRD §15 Q2 and Q3: which commercial attribution vendor, what budget, and above what amount must it be asked? | Phase 4 | Decided |
| Q15 | When should the 2-hop walk run: on request, for large amounts, or on every check? | Phase 4 | Decided |
| Q16 | What counts as 2-hop exposure (R-EXP-03's "threshold")? | Phase 4 | Decided |
| Q17 | PRD §15 Q4: will screening results be shown to corridor clients? This affects Eagle Virtual's licence | Phase 5 | Decided |
| Q18 | Where does the corridor system run, and so where must the API be reachable? | Phase 5 | Decided |
| Q19 | Build Eagle Virtual key rotation without a Business plan? | Phase 5 | Decided |
| Q20 | How is amlcheck packaged: pipx or uv tool, or a single binary? | Phase 5 | Decided |
| Q21 | PRD §15 Q5: how long must audit records be kept, for the corridor's jurisdiction? | Phase 3 | Decided |
| Q22 | Should API keys also be read from the OS keyring (PRD §0 rule 6, §8)? | Phase 5 | Decided |

### Answers

**Q1, decided 2026-09-28.** The BEP20 USDT contract check reports `skipped` with the reason
"BEP20 USDT has no freeze function (V6)". A skipped source is not a failure, so a BSC check can end in
`NO_HITS`. Every BSC result states that the token cannot be frozen. This covers only the token's own
freeze check. Eagle Virtual still runs for BSC addresses, because its answer covers the same address
on other EVM chains (Q2).

**Q2, decided 2026-09-28.** Apply R-FRZ-01 as written. If Eagle Virtual reports the
address FROZEN or SEIZED on any EVM chain, a BSC check is BLOCK, and the finding names the chain,
token and freezing transaction. An ordinary wallet has the same owner on every EVM chain, because one
private key controls that address everywhere. If Eagle Virtual cannot vouch for a chain
(`verdict: null`), the check is INCOMPLETE and names that chain, as PRD rule 4 requires. If BLOCK
proves too strict in practice, it can become a configurable REVIEW, like R-EXP-01.

**Q3, decided 2026-09-28.** The sanctions list's age is the time since its last successful download.
OFAC's publish date is still stored and shown with every result, but it does not make the list
stale. If OFAC cannot be reached for more than 48 hours, the list becomes stale and checks are
INCOMPLETE.

**Q6, decided 2026-09-28.** Chainalysis is dropped as a source. Sanctions screening uses the local
OFAC list.

**Q8, decided 2026-09-28.** `audit export` (CSV, JSON and PDF) is built in Phase 3, as §12 says.
Phase 1 builds only `audit list` and `audit verify`.

**Q4, decided 2026-09-29.** Envio HyperSync's free plan (V12, D21). NodeReal's free tier did not
hold up (V10). The agreed fallback, Etherscan's Lite plan at $49 a month, was built (V11), but the
owner chose not to pay for it. No free source without an account gives a complete history (V13), so
the owner created a free Envio account. Without `HYPERSYNC_API_TOKEN`, a BSC check ends INCOMPLETE.

**Q5, decided 2026-09-28.** PublicAML is not a source for now, because it publishes no terms or
licence. Revisit in Phase 4, when the PRD adds a commercial vendor. Re-checked on 2026-09-29 as a
free BSC history without an account: it misses transfers (V13), so it stays out.

**Q7, decided 2026-09-28.**

- **R-HEU-03 (fan-in):** more than 50 different senders, each sending under 100 USDT, within 24
  hours.
- **R-HEU-04 (fan-out):** more than 50 different recipients within 24 hours.
- **`low`** marks a REVIEW finding as low priority. It does not change the verdict.
- All of these can be changed under `[heuristics]` in `config.toml`.

**Q9, decided 2026-09-28.** R-EXP-01 is REVIEW by default, as §5.2 says. Setting
`[rules] severity = { "R-EXP-01" = "BLOCK" }` makes it BLOCK.

**Q10, decided 2026-09-28.** The exposure source reads at most 5,000 transfers in the lookback
(`[exposure] max_transfers`). If the lookback holds more, the source is stale and the result is
INCOMPLETE, never a clean result over part of the history.

**Q11, decided 2026-09-28.** Counterparties tagged `allowlist` in `labels.csv` are left out of
R-HEU-02 to R-HEU-04. They never cancel a sanctions or freeze finding.

**Q12, decided 2026-09-29.** A changed verdict is printed, kept in the audit log with the check
behind it, and signalled by exit status 6 and, on macOS, a notification (options a and b). No
messaging service or email is involved, so no new account or key is needed.

**Q13, decided 2026-09-29.** `check` and `batch` take `--client "name"` (a `client` column in a
batch file), and `watch add` keeps one too. `audit list`, `audit export` and the web page's history
filter by it, ignoring case (D24).

**Q14 to Q16, decided 2026-09-29.** The owner accepted the recommended answers:

- **Q14:** no vendor for now. Instead, a plug-in point swappable through config (D37). The amount
  above which the vendor is always asked is `[vendor] min_amount_usdt`, 0 by default.
- **Q15:** on request, and for checks of at least 10,000 USDT (D36).
- **Q16:** a counterparty received at least 1,000 USDT from one sanctioned or frozen wallet within
  the lookback (D35). A share of the money traced through the middleman was not chosen, since
  money cannot really be traced dollar by dollar through a middleman.

**Q17 to Q20, decided 2026-09-30.**

- **Q17:** no. Results are for internal use only, so no written agreement with Eagle Virtual is
  needed, and the API's reference says so.
- **Q18:** on another server. amlcheck is installed on that server, and the corridor calls its API
  on 127.0.0.1 (D39, [server.md](server.md)).
- **Q19:** build it anyway. It is tested with mocks only, since there is no Business plan (D40).
- **Q20:** `uv tool` or `pipx`, installed from the GitHub repo (D44).

**Q21 and Q22, decided 2026-09-30.**

- **Q21 (PRD Q5):** keep everything. amlcheck deletes no audit record and no idempotency key. That
  meets any rule to keep records for at least a number of years, such as FATF Recommendation 11's
  five years for transaction records. No purge command is built.
- **Q22:** no. Keys stay in the environment and `.env` (D48).

## Phase 0 exit criteria

| Criterion | Status |
|---|---|
| `amlcheck --help` runs | Done |
| CI green | Done: all 4 jobs (Ubuntu and macOS, Python 3.12 and 3.14) pass on PR #1 |
| Verification report complete | Done: V1–V8 checked, Q1–Q3, Q6 and Q8 answered. Q4, Q5 and Q7 remain open for Phase 2 |
