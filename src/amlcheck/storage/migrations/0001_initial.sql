-- Data model from PRD §9. Timestamps are ISO-8601 UTC text.
-- Migrations are append-only: change the schema with a new numbered file, never by editing this one.

-- Sanctions lists: one row per downloaded snapshot, and the addresses parsed from it.
CREATE TABLE list_snapshots (
    id            INTEGER PRIMARY KEY,
    source        TEXT    NOT NULL,
    fetched_at    TEXT    NOT NULL,
    published_at  TEXT,
    sha256        TEXT    NOT NULL,
    entry_count   INTEGER NOT NULL
);

CREATE TABLE sanctioned_addresses (
    address_norm    TEXT    NOT NULL,
    chain_hint      TEXT,
    currency_label  TEXT    NOT NULL,
    list_entry_id   TEXT    NOT NULL,
    program         TEXT,
    snapshot_id     INTEGER NOT NULL REFERENCES list_snapshots (id)
);
CREATE INDEX sanctioned_addresses_by_address ON sanctioned_addresses (address_norm);

-- Local index of stablecoin issuer blacklist events (TRON USDT).
CREATE TABLE issuer_events (
    chain           TEXT    NOT NULL,
    token_contract  TEXT    NOT NULL,
    address_norm    TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    tx_hash         TEXT    NOT NULL,
    block           INTEGER NOT NULL,
    block_time      TEXT    NOT NULL
);
CREATE INDEX issuer_events_by_address ON issuer_events (chain, address_norm);

CREATE TABLE index_state (
    source      TEXT    PRIMARY KEY,
    last_block  INTEGER NOT NULL,
    updated_at  TEXT    NOT NULL
);

-- The user's own labels, loaded from labels.csv.
CREATE TABLE labels (
    address_norm  TEXT NOT NULL,
    chain         TEXT NOT NULL,
    tag           TEXT NOT NULL,
    note          TEXT,
    source        TEXT
);
CREATE INDEX labels_by_address ON labels (chain, address_norm);

CREATE TABLE http_cache (
    key            TEXT    PRIMARY KEY,
    source         TEXT    NOT NULL,
    response_json  TEXT    NOT NULL,
    fetched_at     TEXT    NOT NULL,
    ttl_s          INTEGER NOT NULL
);

-- Audit log: append-only and hash-chained. `seq` fixes the order of the chain:
-- record_hash = sha256(prev_hash + canonical_json(check + sources + findings)).
CREATE TABLE checks (
    seq            INTEGER PRIMARY KEY,
    check_id       TEXT    NOT NULL UNIQUE,
    created_at     TEXT    NOT NULL,
    address_norm   TEXT    NOT NULL,
    chain          TEXT    NOT NULL,
    verdict        TEXT    NOT NULL CHECK (verdict IN ('BLOCK', 'REVIEW', 'INCOMPLETE', 'NO_HITS')),
    amount_hint    TEXT,
    operator_note  TEXT,
    tool_version   TEXT    NOT NULL,
    config_hash    TEXT    NOT NULL,
    prev_hash      TEXT    NOT NULL,
    record_hash    TEXT    NOT NULL UNIQUE
);
CREATE INDEX checks_by_address ON checks (address_norm, chain);
CREATE INDEX checks_by_time ON checks (created_at);

CREATE TABLE check_sources (
    check_id            TEXT NOT NULL REFERENCES checks (check_id),
    source              TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('ok', 'error', 'stale', 'skipped')),
    as_of               TEXT,
    evidence_meta_json  TEXT NOT NULL
);
CREATE INDEX check_sources_by_check ON check_sources (check_id);

-- observed_at is required by PRD §5.2 ("Each finding records ... observed_at").
CREATE TABLE check_findings (
    check_id       TEXT NOT NULL REFERENCES checks (check_id),
    rule_id        TEXT NOT NULL,
    severity       TEXT NOT NULL,
    source         TEXT NOT NULL,
    evidence_json  TEXT NOT NULL,
    observed_at    TEXT NOT NULL
);
CREATE INDEX check_findings_by_check ON check_findings (check_id);

-- Addresses to re-screen on a schedule (Phase 3).
CREATE TABLE watchlist (
    address_norm     TEXT NOT NULL,
    chain            TEXT NOT NULL,
    added_at         TEXT NOT NULL,
    last_checked_at  TEXT,
    last_verdict     TEXT,
    PRIMARY KEY (address_norm, chain)
);
