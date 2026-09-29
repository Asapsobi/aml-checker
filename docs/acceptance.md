# Acceptance tests (PRD §13) and Phase 1 exit criteria

Where each acceptance test is covered. The tests run without the network, against the recorded
responses in `tests/fixtures/`.

| ID | Expected | Tested in |
|---|---|---|
| AT-01 | An invalid address is an error, with no audit record and a non-zero exit | `test_cli.py::test_invalid_address_is_refused_and_not_recorded` |
| AT-02 | A clean TRON address gives `NO_HITS`, prints the disclaimer and writes an audit record | `test_cli.py::test_clean_address_is_no_hits_with_the_disclaimer`, `test_engine.py::test_clean_sources_give_no_hits_and_the_record_is_written` |
| AT-03 | An address on the OFAC list gives `BLOCK` with R-SAN-01 and the list entry ID | `test_ofac.py::test_listed_address_blocks_with_its_list_entry`, `test_cli.py::test_known_ofac_listed_address_blocks` |
| AT-04 | Eagle Virtual's `FROZEN` gives `BLOCK` with R-FRZ-01 and the transaction hash | `test_eagle_virtual.py::test_frozen_address_names_the_freezing_transaction` |
| AT-05 | Eagle Virtual's `UNFROZEN` gives `REVIEW` with R-FRZ-02 | `test_eagle_virtual.py::test_released_address_is_review` |
| AT-06 | Eagle Virtual's `null` for a lagging chain gives `INCOMPLETE` with R-SYS-01 and the reason | `test_eagle_virtual.py::test_a_chain_it_cannot_vouch_for_makes_it_stale_and_is_asked_again`, `test_verdict.py::test_stale_list_is_incomplete_unless_it_blocks` |
| AT-07 | An HTTP 429 is retried after its Retry-After; still failing, the check is `INCOMPLETE` | `test_net.py::test_retry_after_is_honoured_then_the_answer_returned`, `test_eagle_virtual.py::test_rate_limit_is_waited_out_when_short`, `test_eagle_virtual.py::test_rate_limit_with_a_long_wait_is_an_error` |
| AT-08 | A sanctions list more than 48 hours old gives `INCOMPLETE`, unless there is a BLOCK finding | `test_ofac.py::test_an_old_download_is_stale_but_still_matches`, `test_verdict.py::test_stale_list_is_incomplete_unless_it_blocks` |
| AT-09 | An OFAC hit while Eagle Virtual is down gives `BLOCK` | `test_verdict.py::test_sanctions_hit_blocks_although_another_source_is_down`, `test_engine.py::test_sanctions_hit_blocks_while_another_source_is_down` |
| AT-10 | A mixed-case EVM address with a bad checksum is an error | `test_address.py::test_mixed_case_with_a_bad_checksum_is_refused`, `test_cli.py::test_bad_checksum_is_refused` |
| AT-11 | After one `checks` row is tampered with, `audit verify` reports the break at that record | `test_audit.py::test_tampering_is_reported_at_that_record`, `test_cli.py::test_audit_list_filters_and_verify_finds_tampering` |

## Phase 1 exit criteria

| Criterion | Tested in | Live result, 2026-09-28 |
|---|---|---|
| A known OFAC-listed address gives `BLOCK` | `test_cli.py::test_known_ofac_listed_address_blocks` | `TA3941uFAvmVibSkQ6fMJXxmaSNovX86mz`, CHEIL CREDIT BANK (entry 22985): `BLOCK` in 2.3 seconds, with R-SAN-01 and two R-FRZ-01 (Circle froze its USDC; Tether blacklisted it on 2025-04-30) |
| A known frozen TRON address gives `BLOCK` | `test_cli.py::test_known_frozen_tron_address_blocks` | `TAQM43owNJLZz3vh3PXxBu2qTWf2McMQwJ`, blacklisted on 2026-09-27: `BLOCK` in 2.8 seconds. Eagle Virtual and the TRON index both name transaction `c743e8f4…` |
| A mocked source outage gives `INCOMPLETE` | `test_engine.py::test_a_source_that_breaks_makes_it_incomplete`, `test_cli.py::test_without_a_key_or_data_the_check_is_incomplete` | Not run live |
| `audit verify` detects a tampered row | `test_audit.py::test_tampering_is_reported_at_that_record` | Not run live |

Also run live on 2026-09-28:

- A never-used TRON address gave `NO_HITS`.
- The Lazarus Group address, checked on BSC, gave `BLOCK` with R-SAN-01 (OFAC lists it under ETH) and
  R-FRZ-01 (122 freezes across 25 chains).
- A never-used BSC address gave `NO_HITS`.
- `amlcheck sync` built the list (1,059 addresses) and the index (10,803 events) in 196 seconds, almost
  all of it the OFAC download.

## Phase 2 exit criteria

| Criterion | Tested in | Live result, 2026-09-28 |
|---|---|---|
| An address with a known frozen sender gives `REVIEW`, with the right transaction as evidence | `test_exposure.py::test_known_frozen_sender_gives_review_with_its_transaction`, `test_cli.py::test_known_frozen_sender_gives_review_end_to_end` | `TAjoXRsomrsDDCXsxD1ELFQu4wHfF9HZSv`: `REVIEW` in 4.4 seconds. R-EXP-01 names the 500,000 USDT from the Tether-frozen `TAQM43ow…` (transaction `f94a6ad33f18…`), with R-EXP-02 (16.7%), R-HEU-01 (first active 4 days ago) and R-HEU-02 (100% passed on). OFAC and Eagle Virtual had nothing on this address, so Phase 1 alone would have said `NO_HITS` |
| p95 under 60 seconds on a 1,000-transfer history | `test_exposure.py::test_a_thousand_transfers_are_screened_quickly` (under 5 seconds with the network mocked) | A Bybit hot wallet: the newest 5,000 transfers, the most a check reads, took 20.7 seconds for the whole check. With more in the lookback, the result was `INCOMPLETE`, as decided in Q10 |

BSC exposure is tested against Envio HyperSync answers recorded on 2026-09-29, in
`test_hypersync.py`. Run live on 2026-09-29 (V12):

- `0xd5efbbd79fcdc2834b7e2dcc7a0c6279e1281e36`: `REVIEW` in 6 seconds. R-EXP-01 names the 4,300 USDT
  it received from the OFAC-listed `0x6b0736fe…` (Behzad MESRI, entry 24003) in transaction
  `0x4ae30b33…0a64` on 2026-08-08, which SQD's raw chain data confirms. R-EXP-02 (16.3%) and R-HEU-02
  (100% passed on) came with it. OFAC and Eagle Virtual had nothing on this address.
- `0x4f47bc496083c727c5fbe3ce9cdf2b0f6496270c`, which OFAC lists as a BSC address: `BLOCK` in 8
  seconds, with R-SAN-01 and R-FRZ-01.
- A Binance hot wallet: `INCOMPLETE` in 21 seconds, with the newest 5,000 transfers read.
- A quiet exchange deposit address, 81 transfers: `NO_HITS` in 12 seconds.
- A never-used address: `REVIEW (low)` in 8 seconds, with R-HEU-01 ("no activity on chain yet").

## Phase 3 exit criteria

| Criterion | Tested in | Live result, 2026-09-29 |
|---|---|---|
| A batch of 100 addresses completes within rate limits | `test_batch.py::test_a_batch_of_100_keeps_to_the_eagle_virtual_rate`: 100 addresses on a virtual clock, with every Eagle Virtual call at least a second after the one before (the Free plan's rate) | A batch of 8 real TRON and BSC addresses in 86 seconds: 4 BLOCK, 3 REVIEW and 1 NO_HITS, each as expected |
| The export opens cleanly | `test_export.py::test_the_pdf_opens_and_reads_back` (40 checks, read back with pypdf), `test_cli.py::test_audit_export_in_every_format` | The PDF of those 8 checks: 3 pages, opened and rendered by macOS |
| The watchlist flags a changed verdict | `test_cli.py::test_watch_run_reports_a_changed_verdict`: after Tether freezes a watched address, `watch run` shows NO_HITS → BLOCK, exits 6 and raises one notification | Not tried live: a real verdict change cannot be arranged on demand |

The web page is tested in `test_web.py`:

- the host-name check, the form token and the security headers
- a check from the page, with HTMX and without JavaScript
- the history filters
- the explorer links, and escaping

Live, a BSC check through the page gave the same REVIEW as the command line, with its evidence linked
to BscScan. The first check took 35 seconds; later ones took 3.5 to 11 seconds, as on the command line.
