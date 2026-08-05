-- Migration 001: add review_observations junction table
-- Apply to an existing Version 2 SQLite database:
--   sqlite3 data/google_play_reviews.db < src/db/migrations/001_add_review_observations.sql
-- Fresh databases created via init_db.py already include this table from schema.sql.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS review_observations (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    review_raw_id INTEGER NOT NULL,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES ingestion_runs (run_id),
    FOREIGN KEY (review_raw_id) REFERENCES reviews_raw (review_raw_id),
    UNIQUE (run_id, review_raw_id)
);

CREATE INDEX IF NOT EXISTS ix_review_observations_review_raw_id
ON review_observations (review_raw_id);

CREATE INDEX IF NOT EXISTS ix_review_observations_observed_at
ON review_observations (observed_at);
