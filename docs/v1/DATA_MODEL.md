# amlcheck v1 — Data model

> New tables for v1, as migrations after `0004_phase5.sql`. Migrations stay append-only (never edit an
> earlier file). Timestamps are ISO-8601 UTC text, amounts are decimal text, as in 0.5.1. The SQL below
> is the intended shape: the agent may adjust types or indexes, recording each change as a decision.

| Migration | Phase | Tables |
|---|---|---|
| `0005_intel.sql` | 6 | `transfers`, `history_windows`, `intel_labels`, `entities`, `entity_members`, `counterparties` |
| `0006_classifier.sql` | 7 | `classifications`, `contracts` |
| `0007_trace.sql` | 8 | `traces` |
| `0008_score.sql` | 9 | `checks.score_json` |
| `0009_cases.sql` | 10 | `cases`, `decisions`, `inference_feedback` |
| `0010_monitor.sql` | 11 | `own_wallets`, `monitor_state` |

Unchanged tables keep their meaning. In particular **`labels` stays the `labels.csv` table** (D17):
`labels import` keeps replacing it whole, R-HEU-05 keeps reading it, and the intelligence store reads it
as provenance `labels.csv`, mapping tags to categories (PRD F4.7).

---

## 0005_intel.sql (Phase 6)

```sql
-- v1 Phase 6: persistent transfer cache, intelligence store, counterparty registry.

-- Every USDT transfer read for any address, stored once. A transfer appears in two addresses'
-- histories; it is one row. `idx` tells transfers in one transaction apart (log index on BSC,
-- event index on TRON; V18/V19 confirm the field).
CREATE TABLE transfers (
    chain       TEXT    NOT NULL,
    tx_hash     TEXT    NOT NULL,
    idx         INTEGER NOT NULL,
    block       INTEGER,
    time        TEXT    NOT NULL,
    sender      TEXT    NOT NULL,
    recipient   TEXT    NOT NULL,
    amount      TEXT    NOT NULL,
    PRIMARY KEY (chain, tx_hash, idx)
);
CREATE INDEX transfers_in  ON transfers (chain, recipient, time);
CREATE INDEX transfers_out ON transfers (chain, sender, time);

-- Which part of an address's history is in `transfers`, complete. A read asks only for the gaps.
-- `capped` = the window held more than the read's limit (D22): only the newest part is stored, and
-- `complete` is 0.
CREATE TABLE history_windows (
    chain           TEXT    NOT NULL,
    address_norm    TEXT    NOT NULL,
    since           TEXT    NOT NULL,
    until           TEXT    NOT NULL,
    complete        INTEGER NOT NULL,
    capped          INTEGER NOT NULL DEFAULT 0,
    transfer_count  INTEGER NOT NULL,
    zero_value      INTEGER NOT NULL DEFAULT 0,
    first_activity  TEXT,
    fetched_at      TEXT    NOT NULL,
    last_used_at    TEXT    NOT NULL,
    PRIMARY KEY (chain, address_norm, since)
);

-- Groups of addresses believed to share one owner.
CREATE TABLE entities (
    id          INTEGER PRIMARY KEY,
    chain       TEXT NOT NULL,
    name        TEXT NOT NULL,              -- "hub-TXyz1234" until the operator names it
    kind        TEXT NOT NULL,              -- a category from METHODOLOGY §5, or 'unknown'
    named_by    TEXT,                       -- operator name, NULL when auto-named
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE entity_members (
    entity_id      INTEGER NOT NULL REFERENCES entities (id),
    chain          TEXT    NOT NULL,
    address_norm   TEXT    NOT NULL,
    role           TEXT    NOT NULL,        -- 'hub' | 'deposit' | 'member'
    provenance     TEXT    NOT NULL,        -- 'inferred' | 'operator'
    evidence_json  TEXT    NOT NULL,
    linked_at      TEXT    NOT NULL,
    PRIMARY KEY (chain, address_norm)       -- an address belongs to at most one entity
);

-- Every label except labels.csv. Retracted, never deleted.
CREATE TABLE intel_labels (
    id                INTEGER PRIMARY KEY,
    chain             TEXT    NOT NULL,
    address_norm      TEXT    NOT NULL,
    category          TEXT    NOT NULL,
    provenance        TEXT    NOT NULL CHECK (provenance IN ('list', 'operator', 'import', 'inferred')),
    source_ref        TEXT    NOT NULL,     -- 'ofac:22985', 'pack:<name>', 'case:<id>', 'entity:<id>'
    confidence        TEXT    NOT NULL,     -- '1' for list/operator/import
    note              TEXT,
    licence           TEXT,                 -- required for provenance 'import'
    category_version  INTEGER NOT NULL,
    created_by        TEXT,
    created_at        TEXT    NOT NULL,
    retracted_at      TEXT,
    retracted_by      TEXT,
    retract_reason    TEXT
);
CREATE INDEX intel_labels_by_address ON intel_labels (chain, address_norm) WHERE retracted_at IS NULL;

-- One row per screened counterparty. Derived from the audit log (`cp rebuild` recreates it).
CREATE TABLE counterparties (
    chain               TEXT NOT NULL,
    address_norm        TEXT NOT NULL,
    lookalike_key       TEXT NOT NULL,      -- METHODOLOGY §6
    first_screened_at   TEXT NOT NULL,
    last_screened_at    TEXT NOT NULL,
    last_check_id       TEXT NOT NULL REFERENCES checks (check_id),
    last_verdict        TEXT NOT NULL,
    last_score          INTEGER,
    clients_json        TEXT NOT NULL DEFAULT '[]',   -- sorted, distinct, as typed first
    check_count         INTEGER NOT NULL,
    PRIMARY KEY (chain, address_norm)
);
CREATE INDEX counterparties_lookalike ON counterparties (chain, lookalike_key);
CREATE INDEX counterparties_by_time   ON counterparties (last_screened_at);
```

---

## 0006_classifier.sql (Phase 7)

```sql
-- v1 Phase 7: inferred types and contract lookups.

CREATE TABLE classifications (
    id                  INTEGER PRIMARY KEY,
    chain               TEXT    NOT NULL,
    address_norm        TEXT    NOT NULL,
    type                TEXT    NOT NULL,   -- METHODOLOGY §4
    is_primary          INTEGER NOT NULL,
    confidence          TEXT    NOT NULL,
    features_json       TEXT    NOT NULL,   -- the Profile and the conditions that held
    window_since        TEXT    NOT NULL,
    window_until        TEXT    NOT NULL,
    classifier_version  INTEGER NOT NULL,
    computed_at         TEXT    NOT NULL,
    expires_at          TEXT    NOT NULL
);
CREATE INDEX classifications_by_address ON classifications (chain, address_norm, computed_at);

-- Whether an address is a contract does not change: cached forever.
CREATE TABLE contracts (
    chain         TEXT    NOT NULL,
    address_norm  TEXT    NOT NULL,
    is_contract   INTEGER NOT NULL,
    checked_at    TEXT    NOT NULL,
    source        TEXT    NOT NULL,
    PRIMARY KEY (chain, address_norm)
);
```

---

## 0007_trace.sql (Phase 8)

```sql
-- v1 Phase 8: traces, as jobs and as results.

CREATE TABLE traces (
    trace_id        TEXT PRIMARY KEY,       -- UUID
    chain           TEXT NOT NULL,
    address_norm    TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    status          TEXT NOT NULL CHECK (status IN ('queued', 'running', 'done', 'failed')),
    requested_by    TEXT NOT NULL,          -- 'cli' | 'web' | 'api' | 'check' | 'monitor'
    check_id        TEXT REFERENCES checks (check_id),
    idempotency_key TEXT UNIQUE,
    settings_json   TEXT NOT NULL,          -- [trace] values used, trace_version
    as_of           TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    progress_json   TEXT,                   -- nodes read, queue length, queries, seconds
    result_json     TEXT,                   -- METHODOLOGY §2.8 when done
    partial_json    TEXT,                   -- when failed
    failure_reason  TEXT
);
CREATE INDEX traces_by_address ON traces (chain, address_norm, created_at);
CREATE INDEX traces_by_status  ON traces (status);
```

---

## 0008_score.sql (Phase 9)

```sql
-- v1 Phase 9: the score is part of the check record. Hashed only when set (D24), so every record
-- written before this column keeps verifying.
ALTER TABLE checks ADD COLUMN score_json TEXT;   -- {"score_version":1,"score":66,"band":"high",
                                                 --  "lower_bound":false,"components":{...}}
```

---

## 0009_cases.sql (Phase 10)

```sql
-- v1 Phase 10: cases and the operator's decisions.

CREATE TABLE cases (
    case_id       TEXT PRIMARY KEY,         -- UUID
    chain         TEXT NOT NULL,
    address_norm  TEXT NOT NULL,
    opened_from   TEXT NOT NULL REFERENCES checks (check_id),
    opened_by     TEXT NOT NULL,
    opened_at     TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    client        TEXT
);
CREATE INDEX cases_by_address ON cases (chain, address_norm);

-- Append-only, hash-chained like `checks`, in a chain of its own:
-- record_hash = sha256(prev_hash + canonical_json(decision)). `check_record_hash` ties a decision to
-- the exact check record it was made on.
CREATE TABLE decisions (
    seq                INTEGER PRIMARY KEY,
    decision_id        TEXT NOT NULL UNIQUE,
    case_id            TEXT NOT NULL REFERENCES cases (case_id),
    check_id           TEXT NOT NULL REFERENCES checks (check_id),
    check_record_hash  TEXT NOT NULL,
    decision           TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'escalated')),
    note               TEXT NOT NULL,
    operator           TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    tool_version       TEXT NOT NULL,
    prev_hash          TEXT NOT NULL,
    record_hash        TEXT NOT NULL UNIQUE
);

-- The operator's answer to an inference, made inside a case. A rejection suppresses that type for
-- that address until classifier_version changes (PRD F8.3).
CREATE TABLE inference_feedback (
    id                  INTEGER PRIMARY KEY,
    case_id             TEXT    NOT NULL REFERENCES cases (case_id),
    chain               TEXT    NOT NULL,
    address_norm        TEXT    NOT NULL,
    type                TEXT    NOT NULL,
    classifier_version  INTEGER NOT NULL,
    verdict             TEXT    NOT NULL CHECK (verdict IN ('confirmed', 'rejected')),
    label_id            INTEGER REFERENCES intel_labels (id),   -- set when confirmed
    operator            TEXT    NOT NULL,
    created_at          TEXT    NOT NULL
);
CREATE INDEX inference_feedback_by_address ON inference_feedback (chain, address_norm);
```

---

## 0010_monitor.sql (Phase 11)

```sql
-- v1 Phase 11: the business's own wallets, and where monitoring left off.

CREATE TABLE own_wallets (
    chain         TEXT NOT NULL,
    address_norm  TEXT NOT NULL,
    name          TEXT NOT NULL,
    added_at      TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (chain, address_norm)
);

CREATE TABLE monitor_state (
    chain           TEXT NOT NULL,
    address_norm    TEXT NOT NULL,
    last_seen_time  TEXT NOT NULL,          -- newest inbound transfer already handled
    last_run_at     TEXT NOT NULL,
    PRIMARY KEY (chain, address_norm)
);
```

---

## Retention

| Data | Kept |
|---|---|
| `checks`, `decisions`, `cases`, `api_requests`, `intel_labels`, `inference_feedback` | Forever (Q21) |
| `counterparties`, `entities`, `entity_members`, `own_wallets` | Forever (small) |
| `classifications` | Latest per address and type forever; older ones may be pruned |
| `traces` | Forever when linked to a check; others `[cache] history_keep_days` |
| `transfers`, `history_windows` | Cache: pruned for addresses outside the registry, entities, labels and own wallets after `history_keep_days` (30, Q29) |
| Eagle Virtual answers | Only in `http_cache`, 15 minutes (D46). Never in any table above |
