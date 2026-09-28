# Recorded API responses

Real responses captured on 2026-09-28 during the Phase 0 verification (see `docs/verification.md`).
Adapters are tested against these rather than invented payloads (PRD §0 rule 3). When a provider
changes its API, re-record the file and update the date here.

| File | Request |
|---|---|
| `tron/usdt_getcontract.json` | `POST https://api.trongrid.io/wallet/getcontract` for the USDT contract `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` (bytecode removed to keep it small) |
| `tron/events_AddedBlackList.json`, `events_RemovedBlackList.json`, `events_DestroyedBlackFunds.json` | `GET https://api.trongrid.io/v1/contracts/TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t/events?event_name=<name>&limit=2&order_by=block_timestamp,desc` |
| `tron/trc20_transfers.json` | `GET https://api.trongrid.io/v1/accounts/TNiq9AXBp9EjUqhDhrwrfvAA8U3GUQZH81/transactions/trc20?limit=2&contract_address=TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` (an OFAC-listed address) |
| `tron/isBlackListed_true.json` | `POST https://api.trongrid.io/wallet/triggerconstantcontract` calling `isBlackListed(address)` for `0x04c11ec1d749054d4ef6681bc98ca37663d16553`, blacklisted by Tether on 2026-09-27 |

Eagle Virtual's own API description (`https://eaglevirtual.com/v1/openapi.json`, version 1.2.0 on
2026-09-28) is not copied here, because it is their document. Fetch it when writing the adapter.
