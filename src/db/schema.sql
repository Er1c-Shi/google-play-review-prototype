-- Version 2 schema for Google Play review prototype (SQLite)
-- Foreign keys must be enabled per connection: PRAGMA foreign_keys = ON;

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS data_sources (
    source_id INTEGER PRIMARY KEY,
    source_code TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    description TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS apps (
    app_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    source_app_identifier TEXT NOT NULL,
    app_name TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES data_sources (source_id),
    UNIQUE (source_id, source_app_identifier)
);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    run_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    sort_order TEXT,
    country TEXT,
    language TEXT,
    target_review_count INTEGER,
    app_count INTEGER DEFAULT 0,
    total_fetched INTEGER DEFAULT 0,
    total_inserted INTEGER DEFAULT 0,
    skipped_duplicates INTEGER DEFAULT 0,
    error_summary TEXT,
    notes TEXT,
    FOREIGN KEY (source_id) REFERENCES data_sources (source_id)
);

CREATE TABLE IF NOT EXISTS reviews_raw (
    review_raw_id INTEGER PRIMARY KEY,
    ingestion_run_id INTEGER NOT NULL,
    app_id INTEGER NOT NULL,
    source_review_id TEXT NOT NULL,
    content TEXT,
    score INTEGER,
    thumbs_up_count INTEGER,
    review_created_at TEXT,
    app_version TEXT,
    reply_content TEXT,
    replied_at TEXT,
    collected_at TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    FOREIGN KEY (ingestion_run_id) REFERENCES ingestion_runs (run_id),
    FOREIGN KEY (app_id) REFERENCES apps (app_id),
    UNIQUE (app_id, source_review_id)
);

CREATE TABLE IF NOT EXISTS reviews_processed (
    review_processed_id INTEGER PRIMARY KEY,
    review_raw_id INTEGER NOT NULL UNIQUE,
    cleaned_content TEXT,
    normalized_score INTEGER,
    text_length INTEGER,
    language_code TEXT,
    processed_at TEXT NOT NULL,
    processing_version TEXT NOT NULL,
    FOREIGN KEY (review_raw_id) REFERENCES reviews_raw (review_raw_id)
);

CREATE TABLE IF NOT EXISTS review_quality_flags (
    flag_id INTEGER PRIMARY KEY,
    review_processed_id INTEGER NOT NULL,
    flag_type TEXT NOT NULL,
    flag_value TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
    detected_at TEXT NOT NULL,
    FOREIGN KEY (review_processed_id) REFERENCES reviews_processed (review_processed_id)
);

-- Enforce at most one flag of each type per processed review (idempotent generation).
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_quality_flags_processed_flag_type
ON review_quality_flags (review_processed_id, flag_type);
