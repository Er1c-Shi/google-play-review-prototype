# Google Play Review Prototype

A prototype that evaluates Google Play Store reviews as a data source for downstream analytics, with a SQLite ingestion layer that supports both CSV loading and live Google Play collection.

The repository includes collection scripts, EDA, a Version 2 relational schema, a shared record adapter, CSV and live ingestion paths, observation-aware deduplication, per-app run results, pytest scenarios, and a database validation script.

---

## Objectives

- Evaluate whether Google Play reviews are suitable for a review analytics pipeline.
- Collect structured reviews from multiple applications.
- Assess completeness, uniqueness, duplication, and field reliability.
- Persist data in a relational model that separates identity (`reviews_raw`), processing (`reviews_processed`), and sightings (`review_observations`).
- Support CSV sample loads and live multi-app ingestion with clear run / per-app status.

---

## Repository Structure

```
google-play-review-prototype/
├── data/
│   ├── raw/                         # CSV exports from collect_reviews.py
│   ├── processed/
│   └── samples/                     # Integration sample CSV
├── docs/
│   └── database_integration_test.md
├── notebooks/
│   └── eda_google_play_reviews.ipynb
├── src/
│   ├── collect_reviews.py           # CSV collection (google-play-scraper)
│   ├── review_records.py            # Shared ReviewRecord adapters
│   └── db/
│       ├── schema.sql
│       ├── init_db.py
│       ├── load_sample.py           # CSV → SQLite
│       ├── ingest_live_app.py       # Live single-/multi-app → SQLite
│       ├── validate_db.py
│       ├── apply_migrations.py
│       └── migrations/
├── tests/                           # pytest suite (incl. live scenarios)
├── database_schema.md
├── database_erd.md
├── requirements.txt
└── README.md
```

---

## Current Database Architecture

SQLite Version 2 schema (`src/db/schema.sql`). Details: **[database_schema.md](database_schema.md)**, **[database_erd.md](database_erd.md)**.

| Table | Role |
| ----- | ---- |
| `data_sources` | Source registry (e.g. Google Play) |
| `apps` | One row per package (`source_app_identifier`) |
| `ingestion_runs` | One collection/load execution |
| `ingestion_run_apps` | Per-app result within a run (`fetched` / `inserted` / `skipped`, status, error) |
| `reviews_raw` | Canonical review identity; deduped by `UNIQUE (app_id, source_review_id)` |
| `review_observations` | Junction: review was seen in a run; `UNIQUE (run_id, review_raw_id)` |
| `reviews_processed` | Derived fields for analytics (cleaned text, score, `has_developer_reply`, …) |
| `review_quality_flags` | Quality issues on processed rows (version missing, empty text, etc.) |

Default DB path: `data/google_play_reviews.db` (override with `--db-path` on all DB scripts).

### Raw, processed, and observation

```
ingestion run
    │
    ├─ ingestion_run_apps (per app: counts + status)
    │
    └─ for each fetched review
           ├─ reviews_raw          ← insert only if (app_id, source_review_id) is new
           ├─ review_observations  ← always record (run_id, review_raw_id) when seen
           └─ reviews_processed + quality flags
                                   ← only for newly inserted raw rows
```

- **`reviews_raw`** stores the durable review identity and payload. Reloading the same Google Play `reviewId` for an app does **not** create another raw row.
- **`review_observations`** records that a run observed a review. The same raw review can appear in many runs; the same pair `(run_id, review_raw_id)` cannot.
- **`reviews_processed`** is created once per raw review (on first insert). Skip paths do not recreate processed rows.

### Developer reply: availability feature, not a quality flag

Most public reviews have no developer reply. That is expected, not a data defect.

- Reply text / timestamp stay on `reviews_raw` (`reply_content`, `repliedAt`).
- `reviews_processed.has_developer_reply` is a boolean (SQLite `0`/`1`) set at processing time from non-empty trimmed `reply_content`.
- `missing_developer_reply` is **not** written to `review_quality_flags`.

Current quality flag types: `missing_app_version`, `duplicate_text_within_app`, `empty_review_text`, `invalid_rating`.

---

## How to Run

### 1. Clone and install

```bash
git clone <repository-url>
cd google-play-review-prototype

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

**Python:** 3.10+ recommended.  
**Environment variables:** none required. Paths and targets are set via CLI flags or constants in `src/collect_reviews.py`.

### 2. Configure the database

```bash
python src/db/init_db.py
# optional:
python src/db/init_db.py --db-path data/google_play_reviews.db
```

Creates the SQLite file from `schema.sql` with foreign keys enabled. Safe to re-run (`CREATE TABLE IF NOT EXISTS`).

Existing DBs may need migrations under `src/db/migrations/` if they were created before later schema additions (`review_observations`, `has_developer_reply`, `ingestion_run_apps`). Fresh `init_db` already includes the full current schema.

```bash
# Preferred upgrade helper (bootstraps empty DBs via init_db; skips 002 if column exists)
python src/db/apply_migrations.py --db-path data/google_play_reviews.db
```

### 3. Run the CSV loader

Uses the controlled sample by default:

```bash
python src/db/load_sample.py

python src/db/load_sample.py \
  --db-path data/google_play_reviews.db \
  --sample-path data/samples/google_play_reviews_integration_sample.csv
```

What it does:

1. Ensures `data_sources` / `apps` from the CSV.
2. Creates an `ingestion_runs` row and per-app `ingestion_run_apps` results.
3. Inserts new `reviews_raw` rows; skips existing `(app_id, source_review_id)`.
4. Writes `review_observations` for every valid row in the load.
5. Creates `reviews_processed` + quality flags only for newly inserted raw rows.

Re-running the same sample: `total_inserted = 0`, `skipped_duplicates` equals fetched count, observations are added for the new run, raw count unchanged.

### 4. Choose apps

**CSV batch collection** (`src/collect_reviews.py`) — edit the `APPS` map and `REVIEWS_PER_APP`:

```python
APPS = {
    "Spotify": "com.spotify.music",
    "Duolingo": "com.duolingo",
    # ...
}
```

```bash
python src/collect_reviews.py
# → data/raw/google_play_reviews_sample.csv
```

**CSV loader** — apps are taken from columns in the sample CSV (`app_id` / package id and `app_name`). New packages are inserted into `apps` during load.

**Live ingestion** — apps must **already exist** in `apps` (for example after a CSV load). Select targets on the CLI:

| Mode | How to select |
| ---- | ------------- |
| Single app | `--package-id com.spotify.music` **or** `--app-id <apps.app_id>` |
| Multi-app | Repeat `--package-id` once per app |

Live ingest does not create missing apps; unknown package / app id raises `LookupError`.

### 5. Run the live ingestion pipeline

Requires network access (calls `google-play-scraper`). Reviews are adapted through the shared `ReviewRecord` path and written with the same dedup / observation / processing rules as the CSV loader.

```bash
# Single app
python src/db/ingest_live_app.py --package-id com.spotify.music --n-reviews 50

# Multi-app (one ingestion_runs row; each app committed independently)
python src/db/ingest_live_app.py \
  --package-id com.spotify.music \
  --package-id com.duolingo \
  --n-reviews 50

# Optional DB path / internal app id (single-app only)
python src/db/ingest_live_app.py --app-id 1 --n-reviews 50 --db-path data/google_play_reviews.db
```

Behavior:

- Creates one `ingestion_runs` row for the pipeline invocation.
- For each app: starts `ingestion_run_apps`, collects, writes reviews, finalizes counts/status.
- A later app failure does **not** roll back earlier apps’ committed data.
- Multi-app terminal run status is derived from per-app outcomes (see below).

### 6. Ingestion run and per-app status

**`ingestion_run_apps.status`**

| Status | Meaning |
| ------ | ------- |
| `running` | App result started; should not remain after a finished pipeline |
| `completed` | App finished with at least some inserts and/or skips (may still note per-review errors in `error_message`) |
| `failed` | App produced no inserts and no skips, with an `error_message` (e.g. collector exception) |

Counts on each app result:

- `fetched_count` — reviews accepted into the ingest loop for that app
- `inserted_count` — new `reviews_raw` rows
- `skipped_count` — already-known reviews (still observed)

For a normal completed app: `inserted_count + skipped_count = fetched_count`.

**`ingestion_runs.status` (multi-app)**

| Status | Meaning |
| ------ | ------- |
| `completed` | Every app result is `completed` |
| `partial` | Mix of `completed` and `failed` apps |
| `failed` | Every app failed (or no app results) |
| `running` | In progress; finished runs set `completed_at` |

CSV `load_sample` runs finalize as `completed` when the load finishes successfully.

### 7. Three main live test scenarios

These use a **mocked collector** and a real SQLite write path (no live Google Play dependency):

```bash
# 1) First collection on an empty review store
python -m pytest tests/test_first_live_collection.py -v -s

# 2) Immediate repeat of the same review set (all skips, new observations)
python -m pytest tests/test_immediate_repeat_collection.py -v -s

# 3) Partial overlap (A,B,C then B,C,D,E)
python -m pytest tests/test_partial_overlap_collection.py -v -s
```

Related multi-app error handling (partial / all-failed):

```bash
python -m pytest tests/test_ingest_live_apps.py -v -s
```

Full suite:

```bash
python -m pytest tests/ -q
```

CSV reload integration (needs the sample under `data/samples/`):

```bash
python -m pytest tests/test_load_sample_observations.py::test_integration_sample_two_loads_preserve_counts -v -s
```

### 8. Run the validation script

```bash
python src/db/validate_db.py
python src/db/validate_db.py --db-path data/google_play_reviews.db
```

Each check prints `[PASS]` or `[FAIL]` with sample offending rows on failure. Exit code `0` = all pass, `1` = any fail.

Checks include: raw / observation uniqueness, observation FK integrity, non-negative and consistent per-app counts, terminal-run timestamps, running-state sanity, failed apps having error messages, no `missing_developer_reply` flags, and `has_developer_reply` vs reply content.

Recorded CSV integration notes: **[docs/database_integration_test.md](docs/database_integration_test.md)**.

### 9. Optional: EDA notebook

```bash
jupyter notebook notebooks/eda_google_play_reviews.ipynb
```

---

## Common Errors

| Symptom | Cause / fix |
| ------- | ----------- |
| `LookupError: No app with package_id=...` | Live ingest requires an existing `apps` row. Load a CSV sample first, or insert the app, then re-run with that `--package-id`. |
| `Database not found` from `validate_db.py` | Run `init_db` / a loader first, or pass the correct `--db-path`. |
| Live ingest hangs / network errors | Live mode calls Google Play via `google-play-scraper`. Needs outbound network; CSV loader and mocked pytest scenarios do not. |
| `Multi-app mode accepts repeated --package-id only` | Do not combine multi `--package-id` with `--app-id` / `--run-id`. |
| Stuck `running` rows after a crash | Finished pipelines clear terminal status; a hard kill mid-app can leave `running`. Re-run or inspect `ingestion_runs` / `ingestion_run_apps`. |
| Migration errors on an old DB file | Prefer `python src/db/apply_migrations.py`. Raw `002` SQL fails if the column already exists or if `reviews_processed` is missing — use `init_db` for brand-new DBs. |

**Environment variables:** this prototype does not read API keys or DB URLs from the environment. Configure only via CLI flags and `src/collect_reviews.py` constants.

---

## Data Quality Notes

| Topic | Behavior in this codebase |
| ----- | ------------------------- |
| Dedup key | `(app_id, source_review_id)` on `reviews_raw` (`source_review_id` ← Google Play `reviewId`) |
| Text duplication | Flagged as `duplicate_text_within_app`; raw rows are not dropped |
| App version | Optional; missing values may yield `missing_app_version` |
| Developer replies | Availability via `has_developer_reply`; not a quality flag |
| Sort order | Live/CSV collection uses Newest sort → recency bias |

---

## Current Limitations

- Newest-sort collection reflects recent reviews, not full history.
- App version metadata is incomplete for some reviews.
- Scope is evaluation/prototype: script-based ingestion, no scheduler or production monitoring.
- Live ingest assumes apps were registered beforehand (e.g. via CSV load).
- Single store: Google Play only.

---

## Requirements

| Package | Purpose |
| ------- | ------- |
| `google-play-scraper` | Fetch reviews from Google Play |
| `pandas` | CSV export in `collect_reviews.py` |
| `matplotlib` | EDA notebook charts |
| `jupyter` / `notebook` | EDA notebook |
| `pytest` | Automated tests |

```bash
pip install -r requirements.txt
```
