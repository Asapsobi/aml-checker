-- Phase 3: the client a check was made for (Q13), and what the watchlist needs.

-- A check can name the client it was made for, so an export can be filtered by it. A client is
-- hashed only when set, so every record written before this column keeps verifying.
ALTER TABLE checks ADD COLUMN client TEXT;
CREATE INDEX checks_by_client ON checks (client COLLATE NOCASE);

-- Watchlist: who the address belongs to, a note, and the check behind its last verdict.
ALTER TABLE watchlist ADD COLUMN client TEXT;
ALTER TABLE watchlist ADD COLUMN note TEXT;
ALTER TABLE watchlist ADD COLUMN last_check_id TEXT;
