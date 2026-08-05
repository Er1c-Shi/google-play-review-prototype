# Database Integration Test Results

Controlled SQLite integration test for the Version 2 schema, using a bounded Google Play review sample. Includes verification that reloading the same sample grows `review_observations` without duplicating `reviews_raw`.

## Test setup

| Item | Value |
| ---- | ----- |
| Database | SQLite (`data/google_play_reviews.db` or a pytest temp DB) |
| Sample file | `data/samples/google_play_reviews_integration_sample.csv` |
| Apps | Spotify (`com.spotify.music`), Duolingo (`com.duolingo`) |
| Sample size | 400 reviews (200 per app) |
| Schema | Version 2 (`src/db/schema.sql`) |
| Automated test | `tests/test_load_sample_observations.py::test_integration_sample_two_loads_preserve_counts` |

## First load

| Metric | Value |
| ------ | ----: |
| total_fetched | 400 |
| total_inserted | 400 |
| skipped_duplicates | 0 |
| observations_created | 400 |
| `reviews_raw` | 400 |
| `review_observations` | 400 |
| processed_created | 400 |
| flags_created | 53 |

## Second load (same sample, new ingestion run)

| Metric | Value |
| ------ | ----: |
| total_fetched | 400 |
| total_inserted | 0 |
| skipped_duplicates | 400 |
| observations_created | 400 |
| `reviews_raw` | 400 (unchanged) |
| `review_observations` | 800 |
| processed_created | 0 |
| flags_created | 0 |

## Totals after both runs

| Table / entity | Count |
| -------------- | ----: |
| `reviews_raw` | 400 |
| `review_observations` | 800 |
| observations per run | 400 |
| reviews visible to both runs via observations | 400 |
| duplicate `(app_id, source_review_id)` groups | 0 |
| `reviews_processed` | 400 |
| `review_quality_flags` | 53 |

## Observation reload checks

| Check | Expected |
| ----- | -------- |
| `reviews_raw` does not grow on second load | PASS |
| No duplicate raw reviews on `(app_id, source_review_id)` | PASS |
| Second run still creates 400 observations | PASS |
| Each run can query 400 observed reviews | PASS |
| `inserted` / `skipped` counts match loader summary | PASS |

## Quality flags

| Flag type | Severity | Count |
| --------- | -------- | ----: |
| `missing_app_version` | info | 37 |
| `duplicate_text_within_app` | warning | 16 |
| `empty_review_text` | warning | 0 |
| `invalid_rating` | error | 0 |

Missing developer replies are common and are **not** recorded as quality flags. Reply availability is stored on `reviews_processed.has_developer_reply` (derived at processing time). `reply_content` / `replied_at` remain on `reviews_raw` when present.

## Relationship validation

Ran `python src/db/validate_db.py` after the two loads. All checks reported `[PASS]` (violation count 0), including live-ingestion checks:

| Check | Count |
| ----- | ----: |
| orphan_raw_reviews / orphan_processed_reviews / orphan_quality_flags | 0 |
| duplicate_processed_reviews / duplicate_quality_flags | 0 |
| foreign_key_violations | 0 |
| no_duplicate_app_source_review_id / no_duplicate_run_observation | 0 |
| observation_refs_valid | 0 |
| app_result_counts_non_negative / completed_app_count_consistency | 0 |
| terminal_run_has_completed_at / running_run_state / running_app_result_state | 0 |
| failed_app_has_error_message | 0 |
| no_missing_developer_reply_flags | 0 |
| has_developer_reply_matches_reply_content | 0 |

**Result: PASS**

## How to run the full test

From the repository root:

```bash
# Preferred: automated integration assertion (includes observation reload checks)
python -m pytest tests/test_load_sample_observations.py::test_integration_sample_two_loads_preserve_counts -v -s

# Manual script flow (optional)
rm -f data/google_play_reviews.db
python src/db/init_db.py
python src/db/load_sample.py
python src/db/load_sample.py
python src/db/validate_db.py
```

Optional path overrides:

```bash
python src/db/load_sample.py \
  --db-path data/google_play_reviews.db \
  --sample-path data/samples/google_play_reviews_integration_sample.csv

python src/db/validate_db.py --db-path data/google_play_reviews.db
```
