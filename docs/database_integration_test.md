# Database Integration Test Results

Controlled SQLite integration test for the Version 2 schema, using a bounded Google Play review sample.

## Test setup

| Item | Value |
| ---- | ----- |
| Database | SQLite (`data/google_play_reviews.db`) |
| Sample file | `data/samples/google_play_reviews_integration_sample.csv` |
| Apps | Spotify (`com.spotify.music`), Duolingo (`com.duolingo`) |
| Sample size | 400 reviews (200 per app) |
| Schema | Version 2 (`src/db/schema.sql`) |

## First load

| Metric | Value |
| ------ | ----: |
| total_fetched | 400 |
| total_inserted | 400 |
| skipped_duplicates | 0 |
| processed_created | 400 |
| flags_created | 432 |

## Second load (same sample)

| Metric | Value |
| ------ | ----: |
| total_fetched | 400 |
| total_inserted | 0 |
| skipped_duplicates | 400 |
| processed_created | 0 |
| flags_created | 0 |

## Totals after both runs

| Table / entity | Count |
| -------------- | ----: |
| `reviews_raw` | 400 |
| `reviews_processed` | 400 |
| `review_quality_flags` | 432 |

## Quality flags

| Flag type | Severity | Count |
| --------- | -------- | ----: |
| `missing_developer_reply` | info | 379 |
| `missing_app_version` | info | 37 |
| `duplicate_text_within_app` | warning | 16 |
| `empty_review_text` | warning | 0 |
| `invalid_rating` | error | 0 |

## Relationship validation

Ran `python src/db/validate_db.py` after the two loads:

| Check | Count |
| ----- | ----: |
| orphan_raw_reviews | 0 |
| orphan_processed_reviews | 0 |
| orphan_quality_flags | 0 |
| duplicate_processed_reviews | 0 |
| duplicate_quality_flags | 0 |
| foreign_key_violations | 0 |

**Result: PASS**

## How to run the full test

From the repository root:

```bash
# Optional: start from a clean database
rm -f data/google_play_reviews.db

# Initialize schema (also invoked automatically by the loader)
python src/db/init_db.py

# First load
python src/db/load_sample.py

# Second load (deduplication check)
python src/db/load_sample.py

# Relationship integrity checks
python src/db/validate_db.py
```

Optional path overrides:

```bash
python src/db/load_sample.py \
  --db-path data/google_play_reviews.db \
  --sample-path data/samples/google_play_reviews_integration_sample.csv

python src/db/validate_db.py --db-path data/google_play_reviews.db
```
