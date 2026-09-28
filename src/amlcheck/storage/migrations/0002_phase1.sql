-- Phase 1: what the sources and the audit log need beyond the key columns of PRD §9.

-- OFAC: who is listed, and how many addresses a snapshot holds (the PRD §14 sanity check).
ALTER TABLE sanctioned_addresses ADD COLUMN entity_name TEXT;
ALTER TABLE list_snapshots ADD COLUMN address_count INTEGER;

-- TRON USDT index: the balance a DestroyedBlackFunds event destroyed, and the time of the last
-- block read, which is what "index lag" measures. Syncing the same events twice adds nothing.
ALTER TABLE issuer_events ADD COLUMN amount TEXT;
ALTER TABLE index_state ADD COLUMN last_block_time TEXT;
CREATE UNIQUE INDEX issuer_events_unique
    ON issuer_events (chain, token_contract, tx_hash, event_type, address_norm);

-- Audit log: keep what the operator was shown with the record, so the hash covers it too.
ALTER TABLE check_sources ADD COLUMN required INTEGER;
ALTER TABLE check_sources ADD COLUMN summary TEXT;
ALTER TABLE check_findings ADD COLUMN summary TEXT;
