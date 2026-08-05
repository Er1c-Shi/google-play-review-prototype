-- Migration 002: add has_developer_reply availability feature to reviews_processed
-- Prerequisites: an existing Version 2 database that already has reviews_processed.
-- Do not apply this file alone to a completely empty database — use init_db.py
-- (full schema) or: python src/db/apply_migrations.py
--
-- Apply manually:
--   sqlite3 data/google_play_reviews.db < src/db/migrations/002_add_has_developer_reply.sql
-- Fresh databases from init_db.py already include this column.
-- Re-running this file on a DB that already has the column fails (SQLite has no
-- ADD COLUMN IF NOT EXISTS); apply_migrations.py skips 002 in that case.
--
-- Backfill rule (same as processing): true when reply_content is non-null and not
-- blank/whitespace-only; otherwise false. Default 0 covers rows with no reply.

PRAGMA foreign_keys = ON;

ALTER TABLE reviews_processed
ADD COLUMN has_developer_reply INTEGER NOT NULL DEFAULT 0;

UPDATE reviews_processed
SET has_developer_reply = 1
WHERE review_raw_id IN (
    SELECT review_raw_id
    FROM reviews_raw
    WHERE reply_content IS NOT NULL
      AND TRIM(reply_content) != ''
);
