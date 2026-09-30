# Local HTTP API

`amlcheck api` lets another program on the same computer screen an address: a corridor system, for
example, before it settles a transfer (PRD §10.4, U7). `POST /v1/check` answers with the same JSON
as `amlcheck check --json` (PRD §10.3).

The API is for internal use (Q17). Its answers are not meant to be shown to the corridor's clients:
Eagle Virtual's licence needs a written agreement for that.

## Running it

The API listens on `127.0.0.1` only, so its caller must run on the same machine. On a server,
install amlcheck next to the corridor system ([server.md](server.md)).

1. Make a token and put it in `~/.amlcheck/.env` (or the `.env` of the folder you run amlcheck
   from). Give the same value to the calling system, and keep it secret:

   ```bash
   python3 -c 'import secrets; print("AMLCHECK_API_TOKEN=" + secrets.token_urlsafe(32))' >> ~/.amlcheck/.env
   chmod 600 ~/.amlcheck/.env
   ```

2. Start it. The default port is 8766; `--port` changes it.

   ```bash
   amlcheck api
   ```

It refuses to start without a token of at least 32 characters, or with a `[vendor] adapter` that
does not load. Keep `amlcheck sync sanctions` running daily as well: a sanctions list older than
48 hours makes every check `INCOMPLETE`.

## Requests

Every request needs the header `Authorization: Bearer <AMLCHECK_API_TOKEN>`, and the host name must
be `127.0.0.1` or `localhost`. Anything else is refused before it is read (401 or 400).

### `POST /v1/check`

Screens one address, writes the check to the audit log, and answers with the result.

```bash
curl -sS http://127.0.0.1:8766/v1/check \
  -H "Authorization: Bearer $AMLCHECK_API_TOKEN" \
  -H "Content-Type: application/json" \
  -H 'Idempotency-Key: "8e03978e-40d5-43e8-bc93-6894a57f9324"' \
  -d '{"address": "TJwwz9NR37hjXdAV5gowj7src4avMuZZNW", "amount": "25000", "client": "ACME Ltd"}'
```

The body is a JSON object (`Content-Type: application/json`, at most 16 KiB):

| Field | Type | Meaning |
|---|---|---|
| `address` | string, required | A TRON (`T…`) or BSC (`0x…`) address |
| `chain` | `"tron"` or `"bsc"` | Sets the chain instead of detecting it from the address |
| `amount` | string or number | The planned amount in USDT, such as `"25000"` or `"25,000.50"`. It is kept in the audit log. An amount of at least `[two_hop] auto_amount_usdt` (10,000 by default) adds the 2-hop walk |
| `note` | string, up to 1,000 characters | Kept in the audit log, such as the settlement's ID |
| `client` | string, up to 200 characters | The client the check is for, as with `--client` |
| `two_hop` | boolean | `true` adds the 2-hop walk at any amount, as with `amlcheck investigate` |

Other fields are refused. A check takes up to a minute, or about two with the 2-hop walk, and checks
run one at a time. Give the call a timeout of at least 5 minutes.

The answer is `200` for every verdict, with the result below. `Content-Location` names the check,
such as `/v1/checks/7f3c…`.

### `GET /v1/checks/{check_id}`

A check made earlier, read from the audit log. It is the same JSON the check was answered with,
except that each object's keys may come in another order. `404` when no check has that ID.

### `GET /v1/health`

`{"status": "ok", "tool_version": "0.5.0"}` while the API is up. It does not ask any source. Use
`amlcheck status` for the sources' health.

## The result

This is the stable contract of PRD §10.3. Fields may be added; none is removed or changes meaning.

| Field | Type | Meaning |
|---|---|---|
| `check_id` | string | A UUID naming the check in the audit log |
| `created_at` | string | ISO-8601 UTC time of the check |
| `address` | string | The address as screened: TRON Base58, or BSC lower-case hex |
| `chain` | `"tron"` or `"bsc"` | |
| `verdict` | `"BLOCK"`, `"REVIEW"`, `"INCOMPLETE"` or `"NO_HITS"` | See below |
| `sources` | array | One object per source: `source`, `label`, `required`, `status` (`ok`, `error`, `stale` or `skipped`), `as_of`, `summary` and `meta` |
| `findings` | array | One object per finding: `rule_id` (such as `R-SAN-01`), `severity`, `priority`, `source`, `summary`, `evidence` and `observed_at` |
| `amount_hint`, `operator_note`, `client` | string or null | What the request gave |
| `tool_version` | string | The amlcheck that made the check |
| `config_hash` | string | sha256 of the settings the check ran with |
| `record_hash` | string | The check's hash in the audit log (`amlcheck audit verify`) |
| `attribution` | array of strings | Credit lines the data calls for, such as Eagle Virtual's on its Free plan |

What a verdict means for a transfer:

| Verdict | Meaning |
|---|---|
| `BLOCK` | Do not transact. Escalate |
| `INCOMPLETE` | A required source failed or is out of date, so the result cannot be trusted. Retry, or treat it as `REVIEW`. Never treat it as clean |
| `REVIEW` | Review manually before transacting |
| `NO_HITS` | Nothing was found in the sources checked, as of the times shown. **Not a clearance** |

`scripts/corridor_mock.py` is a complete caller, written with the Python standard library only.
It checks the contract and lets a transfer go ahead only on `NO_HITS`.

## Idempotency

A corridor that loses the answer, for example to a timeout, can send the same request again
without making a second check. amlcheck follows the IETF draft
[The Idempotency-Key HTTP Header Field](https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/)
(revision 07):

- Send `Idempotency-Key` with a new value for each screening, such as a UUID. The draft makes the
  value a quoted string (`"…"`); a bare value is taken too. A key is 8 to 128 letters, digits and
  `.` `_` `:` `-`.
- The same key with the same request gets the first check back, with the header
  `Idempotent-Replayed: true`. No new check is made or logged, so the answer keeps its first
  `created_at`. Look at that time when you retry much later.
- A request is the same when it means the same: the address as screened, the chain, the amount's
  value, the note without surrounding spaces, the client and `two_hop`.
- The same key with a different request is refused with `422`. Use a new key for a new request.
- While the first request with a key is still running, the same key gets `409`. Retry a few
  seconds later to get its result.
- Keys never expire. Like the audit log, they are never deleted.
- To screen an address again, for example after `INCOMPLETE`, use a new key: the old one would
  give back the old result.

A request without the header is simply a new check each time.

## Errors

Errors come as `application/problem+json` (RFC 9457), with `title`, `status` and `detail`:

| Status | When |
|---|---|
| 400 | The body is not valid JSON, has an unknown field or a wrong type, or the address, amount, client or Idempotency-Key is invalid. Also a host name other than 127.0.0.1 or localhost |
| 401 | The token is missing or wrong |
| 404 | No check has that ID, or no such path |
| 405 | A method the path does not take |
| 409 | The first request with this Idempotency-Key is still running |
| 413 | The body is larger than 16 KiB |
| 415 | The body is not `application/json` |
| 422 | The Idempotency-Key was used for a different request |
| 500 | The check could not be made (see `~/.amlcheck/logs/amlcheck.log`), or a stored check no longer matches its hash (run `amlcheck audit verify`) |

A source that fails is not an HTTP error: the check still answers `200`, with the source's status
and the verdict `INCOMPLETE`.

## Security

- **Reachable from this machine only.** It is bound to 127.0.0.1, and only the host names
  127.0.0.1 and localhost are served, which stops DNS rebinding.
- **Every request needs the token.** It is compared in constant time and never logged. Keep it only
  in `.env` files with mode 600, or in the calling system's secret store.
- **No browser access.** The API sends no CORS headers, so a web page cannot read its answers.
- **Answers are not cached.** Each one carries `Cache-Control: no-store`.
- **Replays come from the audit log.** They are rebuilt from the audit record only after its hash
  still matches.
