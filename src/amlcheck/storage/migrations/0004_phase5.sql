-- Phase 5: the local API's idempotency keys (docs/api.md).

-- A corridor that sends a request again with the same Idempotency-Key gets the check that request
-- already made, not a second one. `fingerprint` is the sha256 of the request as understood, so the
-- same key cannot be reused for a different request. Keys never expire: like the audit log they
-- name, they are never deleted.
CREATE TABLE api_requests (
    idempotency_key  TEXT PRIMARY KEY,
    fingerprint      TEXT NOT NULL,
    check_id         TEXT NOT NULL UNIQUE REFERENCES checks (check_id),
    created_at       TEXT NOT NULL
);
