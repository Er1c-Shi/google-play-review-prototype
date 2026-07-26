# Database Schema Design (Version 2)

This document proposes a relational database schema for the Google Play review prototype. The current implementation ingests reviews from Google Play only. The design prioritizes traceability and clarity for the prototype, while remaining extensible so that additional review sources could be incorporated later without major redesign.

---

## 1. Design Goals

The schema is intended to support the following requirements:


| Goal                             | How the schema addresses it                                                                                                                                  |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **Google Play ingestion**        | Tables model the current collection workflow: apps, ingestion runs, raw reviews, processed reviews, and quality flags.                                       |
| **Traceability**                 | Every review links to an `app`, a `data_source`, and an `ingestion_run`, making it possible to determine where a record came from and when it was collected. |
| **Raw vs. processed separation** | `reviews_raw` stores fields as collected from Google Play; `reviews_processed` stores cleaned and normalized fields derived from raw records.                |
| **Deduplication**                | Stable Google Play review identifiers are enforced through unique constraints on `(app_id, source_review_id)`.                                               |
| **Data quality visibility**      | `review_quality_flags` records anomalies and quality signals without conflating them with core review content.                                               |
| **Extensibility**                | A lightweight `data_sources` table and source-scoped foreign keys provide a path to add new platforms later without restructuring core review tables.        |


---



## 2. Overall Data Flow

```text
┌──────────────┐     ┌──────────────┐     ┌─────────────────┐
│ data_sources │────▶│     apps     │────▶│ ingestion_runs  │
└──────────────┘     └──────────────┘     └────────┬────────┘
                                                   │
                                                   ▼
                                           ┌─────────────────┐
                                           │   reviews_raw   │
                                           └────────┬────────┘
                                                    │
                                                    ▼
                                        ┌───────────────────────┐
                                        │  reviews_processed    │
                                        └───────────┬───────────┘
                                                    │
                                                    ▼
                                        ┌───────────────────────┐
                                        │ review_quality_flags  │
                                        └───────────────────────┘
```

**Typical workflow:**

1. Register Google Play as the active `data_source`.
2. Register tracked applications in `apps` (using Google Play package identifiers).
3. Execute a collection job and record an `ingestion_run`.
4. Insert newly collected records into `reviews_raw`.
5. Apply deduplication rules during or after ingestion.
6. Transform raw records into `reviews_processed`.
7. Attach `review_quality_flags` based on validation and EDA-informed rules.
8. Expose processed records to analytics and NLP workflows.

---



## 3. Entity Relationship Diagram

The full ER diagram is maintained in a separate file for readability:

**[database_erd.md](database_erd.md)**

The schema consists of six tables:

1. `data_sources` — review platform reference (Google Play in the current prototype)
2. `apps` — tracked Google Play applications
3. `ingestion_runs` — collection run audit trail
4. `reviews_raw` — immutable collected review records
5. `reviews_processed` — cleaned, analysis-ready fields
6. `review_quality_flags` — data quality signals

---



## 4. Table Definitions



### 4.1 `data_sources`

**Purpose:** Identify the review platform for each app, ingestion run, and review record. In the current prototype, this table contains a single row for Google Play. It exists primarily to keep source references explicit and to avoid a schema redesign if additional platforms are added later.


| Field         | Type     | Notes                                           |
| ------------- | -------- | ----------------------------------------------- |
| `source_id`   | Integer  | **Primary key.** Internal surrogate identifier. |
| `source_code` | String   | Platform code (e.g., `google_play`).            |
| `source_name` | String   | Human-readable platform name.                   |
| `description` | String   | Optional notes about the source.                |
| `created_at`  | Datetime | Record creation timestamp.                      |


**Primary key:** `source_id`

**Unique constraints:** `source_code`

**Foreign keys:** None

---



### 4.2 `apps`

**Purpose:** Store metadata for each Google Play application tracked by the prototype.


| Field                   | Type          | Notes                                                     |
| ----------------------- | ------------- | --------------------------------------------------------- |
| `app_id`                | Integer       | **Primary key.** Internal surrogate identifier.           |
| `source_id`             | Integer       | **Foreign key** → `data_sources.source_id`                |
| `source_app_identifier` | String        | Google Play package name (e.g., `com.example.app`).       |
| `app_name`              | String        | Display name of the application.                          |
| `metadata_json`         | String / JSON | Optional flexible metadata for additional app attributes. |
| `created_at`            | Datetime      | Record creation timestamp.                                |


**Primary key:** `app_id`

**Foreign keys:** `source_id` → `data_sources.source_id`

**Unique constraints:** `UNIQUE (source_id, source_app_identifier)`

**Design note:** `source_app_identifier` is not globally unique. The same identifier string could theoretically exist on different platforms; uniqueness is enforced only within a data source. This keeps the schema ready for additional review sources beyond Google Play.

---



### 4.3 `ingestion_runs`

**Purpose:** Record each review collection execution for auditability and traceability.


| Field                 | Type     | Notes                                                        |
| --------------------- | -------- | ------------------------------------------------------------ |
| `run_id`              | Integer  | **Primary key.** Internal surrogate identifier.              |
| `source_id`           | Integer  | **Foreign key** → `data_sources.source_id`                   |
| `started_at`          | Datetime | Run start time.                                              |
| `completed_at`        | Datetime | Run completion time.                                         |
| `status`              | String   | Run state (e.g., `running`, `completed`, `failed`).          |
| `sort_order`          | String   | Collection sort option used (e.g., `newest`).                |
| `country`             | String   | Optional. Country/locale filter for the run (e.g., `US`).    |
| `language`            | String   | Optional. Language filter for the run (e.g., `en`).          |
| `target_review_count` | Integer  | Optional. Planned max reviews to fetch.                      |
| `app_count`           | Integer  | Optional. Number of apps targeted; default `0`.              |
| `total_fetched`       | Integer  | Optional. Reviews retrieved from source; default `0`.        |
| `total_inserted`      | Integer  | Optional. New rows inserted into `reviews_raw`; default `0`. |
| `skipped_duplicates`  | Integer  | Optional. Reviews skipped by deduplication; default `0`.     |
| `error_summary`       | Text     | Optional. Summary of errors during the run.                  |
| `notes`               | String   | Optional run-level context.                                  |


**Primary key:** `run_id`

**Foreign keys:** `source_id` → `data_sources.source_id`

**Unique constraints:** None

**Design note:** A single ingestion run may collect reviews for multiple apps. App-level coverage is inferred from `reviews_raw` rather than stored in a separate junction table, which keeps the prototype lean.

---



### 4.4 `reviews_raw`

**Purpose:** Store immutable Google Play review records exactly as collected.


| Field               | Type        | Notes                                                                                               |
| ------------------- | ----------- | --------------------------------------------------------------------------------------------------- |
| `review_raw_id`     | Integer     | **Primary key.** Internal surrogate identifier.                                                     |
| `ingestion_run_id`  | Integer     | **Foreign key** → `ingestion_runs.run_id`                                                           |
| `app_id`            | Integer     | **Foreign key** → `apps.app_id`                                                                     |
| `source_review_id`  | String      | Google Play `reviewId` used for deduplication.                                                      |
| `content`           | Text        | Original review text.                                                                               |
| `score`             | Integer     | Star rating as returned by Google Play.                                                             |
| `thumbs_up_count`   | Integer     | Helpfulness or thumbs-up count, if available.                                                       |
| `review_created_at` | Datetime    | Timestamp when the user posted the review.                                                          |
| `app_version`       | String      | App version associated with the review; may be null.                                                |
| `reply_content`     | Text        | Developer reply text; expected to be sparse.                                                        |
| `replied_at`        | Datetime    | Developer reply timestamp; may be null.                                                             |
| `collected_at`      | Datetime    | Timestamp when the record was ingested.                                                             |
| `raw_payload_json`  | JSON / Text | Required. Full source review object; use `JSONB` in PostgreSQL or serialized JSON `TEXT` in SQLite. |


**Primary key:** `review_raw_id`

**Foreign keys:**

- `ingestion_run_id` → `ingestion_runs.run_id`
- `app_id` → `apps.app_id`

**Unique constraints:** `UNIQUE (app_id, source_review_id)`

**Design note:** `source_review_id` is unique within an app, not globally.

---



### 4.5 `reviews_processed`

**Purpose:** Store cleaned, normalized, and analysis-ready review fields derived from raw records.


| Field                 | Type     | Notes                                                          |
| --------------------- | -------- | -------------------------------------------------------------- |
| `review_processed_id` | Integer  | **Primary key.** Internal surrogate identifier.                |
| `review_raw_id`       | Integer  | **Foreign key** → `reviews_raw.review_raw_id`                  |
| `cleaned_content`     | Text     | Normalized review text (trimmed, standardized encoding, etc.). |
| `normalized_score`    | Integer  | Validated rating on a consistent scale.                        |
| `text_length`         | Integer  | Character or token length for basic quality inspection.        |
| `language_code`       | String   | Detected or assigned language; optional at prototype stage.    |
| `processed_at`        | Datetime | Timestamp when processing occurred.                            |
| `processing_version`  | String   | Version label for the cleaning logic applied.                  |


**Primary key:** `review_processed_id`

**Foreign keys:** `review_raw_id` → `reviews_raw.review_raw_id`

**Unique constraints:** `review_raw_id`

---



### 4.6 `review_quality_flags`

**Purpose:** Capture data quality signals and anomalies without modifying core review content.


| Field                 | Type     | Notes                                                                     |
| --------------------- | -------- | ------------------------------------------------------------------------- |
| `flag_id`             | Integer  | **Primary key.** Internal surrogate identifier.                           |
| `review_processed_id` | Integer  | **Foreign key** → `reviews_processed.review_processed_id`                 |
| `flag_type`           | String   | Flag category (e.g., `missing_app_version`, `duplicate_text_within_app`). |
| `flag_value`          | String   | Optional detail or supporting value for the flag.                         |
| `severity`            | String   | One of `info`, `warning`, or `error`.                                     |
| `detected_at`         | Datetime | Timestamp when the flag was generated.                                    |


**Primary key:** `flag_id`

**Foreign keys:** `review_processed_id` → `reviews_processed.review_processed_id`

---



## 5. Table Relationships


| Parent Table        | Child Table            | Relationship                                                  | Join Key              | Cardinality |
| ------------------- | ---------------------- | ------------------------------------------------------------- | --------------------- | ----------- |
| `data_sources`      | `apps`                 | Each app belongs to one source                                | `source_id`           | 1:N         |
| `data_sources`      | `ingestion_runs`       | Each run belongs to one source (Google Play in the prototype) | `source_id`           | 1:N         |
| `apps`              | `reviews_raw`          | Each raw review belongs to one app                            | `app_id`              | 1:N         |
| `ingestion_runs`    | `reviews_raw`          | Each raw review is produced by one run                        | `ingestion_run_id`    | 1:N         |
| `reviews_raw`       | `reviews_processed`    | Each raw review may have zero or one current processed record | `review_raw_id`       | 0..1:1      |
| `reviews_processed` | `review_quality_flags` | Each flag belongs to one processed review                     | `review_processed_id` | 1:N         |


**Traceability chain:**

```text
review_quality_flags
  → reviews_processed
    → reviews_raw
      → ingestion_runs
      → apps
        → data_sources
```

This chain makes it possible to determine, for any flagged or analyzed review:

- which data source it came from (Google Play in the current prototype),
- which app it belongs to,
- which ingestion run collected it,
- when it was posted and when it was collected,
- how it was processed,
- what quality issues were detected.

---



## 6. Deduplication Logic

Deduplication is based on EDA findings that Google Play review IDs are stable and duplicate review IDs are very rare.

### Primary deduplication rule

A review is considered a duplicate if the same `source_review_id` already exists for the same `app_id` in `reviews_raw`.

**Enforced by:** `UNIQUE (app_id, source_review_id)`

`source_review_id` does not need to be globally unique—only unique within the same app.

---



## 7. Data Quality Handling

Quality checks should be applied after raw ingestion and during or after processing. Known prototype findings inform the following initial flag types:


| Flag Type                   | Trigger                                                              | Severity  | Rationale                                                                                       |
| --------------------------- | -------------------------------------------------------------------- | --------- | ----------------------------------------------------------------------------------------------- |
| `missing_developer_reply`   | `reply_content` is null                                              | `info`    | Developer replies are commonly absent and should not mark a review as problematic.              |
| `missing_app_version`       | `app_version` is null in raw data                                    | `info`    | App version is partially missing in the source data.                                            |
| `duplicate_text_within_app` | Identical `content` appears for multiple reviews within the same app | `warning` | Indicates possible spam, templated feedback, or copy-paste behavior; not a deduplication event. |
| `empty_review_text`         | `content` is null or blank after cleaning                            | `warning` | Review text is required for NLP workflows.                                                      |
| `invalid_rating`            | `score` outside the expected Google Play range (1–5)                 | `error`   | Protects downstream analytics from malformed values.                                            |


Possible future flag types (documentation only): `low_signal_text`, `possible_non_english`, `repeated_short_content`.

---

