-- Migration 003: add ingestion_run_apps (per-app execution results)
-- Apply to an existing Version 2 SQLite database:
--   sqlite3 data/google_play_reviews.db < src/db/migrations/003_add_ingestion_run_apps.sql
-- Fresh databases created via init_db.py already include this table from schema.sql.

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS ingestion_run_apps (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    app_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'failed')),
    fetched_count INTEGER NOT NULL DEFAULT 0,
    inserted_count INTEGER NOT NULL DEFAULT 0,
    skipped_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    FOREIGN KEY (run_id) REFERENCES ingestion_runs (run_id),
    FOREIGN KEY (app_id) REFERENCES apps (app_id),
    UNIQUE (run_id, app_id)
);

CREATE INDEX IF NOT EXISTS ix_ingestion_run_apps_app_id
ON ingestion_run_apps (app_id);

CREATE INDEX IF NOT EXISTS ix_ingestion_run_apps_status
ON ingestion_run_apps (status);
