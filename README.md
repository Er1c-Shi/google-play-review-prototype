# Google Play Review Prototype

A prototype project to evaluate Google Play Store reviews as a potential data source for downstream product analytics and NLP applications.

---

## Project Overview

This repository explores whether Google Play reviews are suitable for building a review analytics pipeline. The work goes beyond data collection: it also assesses the quality, completeness, and usability of the collected review data before investing in a production-grade ingestion and analytics system.

The prototype includes a Python collection script, raw and processed review datasets, an exploratory data analysis (EDA) notebook, and data quality checks. Reviews were collected from multiple Google Play applications.

---



## Objectives

- **Evaluate data suitability** — Determine whether Google Play reviews meet the requirements of a downstream review analytics pipeline.
- **Collect structured review data** — Gather reviews from multiple applications in a consistent, machine-readable format.
- **Assess data quality** — Inspect completeness, uniqueness, duplication patterns, and field-level reliability.
- **Document limitations** — Identify gaps and constraints that should inform future pipeline design.
- **Inform next-stage architecture** — Use findings to guide database schema and ingestion design for multi-source, multi-app analytics.

---



## Repository Structure

```
google-play-review-prototype/
├── data/
│   ├── raw/
│   └── processed/
├── notebooks/
│   └── eda_google_play_reviews.ipynb
├── src/
│   └── collect_reviews.py
├── requirements.txt
└── README.md
```

---



## Data Source

Reviews are sourced from the Google Play Store via public-facing review data. The collection script uses the google-play-scraper Python library to retrieve review records for configured applications.

Each review record typically includes fields such as:


| Field                        | Description                      |
| ---------------------------- | -------------------------------- |
| `reviewId`                   | Unique identifier for the review |
| `content`                    | Review text                      |
| `score`                      | Star rating                      |
| `at`                         | Review timestamp                 |
| `app_id` / `app_name`        | Application identifiers          |
| `appVersion`                 | App version at time of review    |
| `replyContent` / `repliedAt` | Developer reply, if present      |


---



## Data Collection Workflow

1. **Configure target applications** — Define the set of Google Play applications to collect in `src/collect_reviews.py`.
2. **Fetch reviews in batches** — The script retrieves reviews using the Newest sort order, paginating until the configured limit is reached per application.
3. **Enrich records** — Each review is annotated with `app_name` and `app_id` for downstream multi-app analysis.
4. **Export to CSV** — Collected reviews are written to `data/raw/google_play_reviews_sample.csv`.
5. **Run quality checks** — Use the EDA notebook to inspect structure, completeness, and duplication patterns.

```bash
python src/collect_reviews.py
```

---



## Exploratory Data Analysis

The notebook `notebooks/eda_google_play_reviews.ipynb` performs an initial exploratory analysis of the collected dataset. The analysis covers:

- Review ID uniqueness
- Duplicate review IDs
- Duplicate review text within the same app
- Missing values
- Rating distribution
- Timestamp coverage
- App version availability
- Developer reply availability
- Basic review quality inspection

---



## Key Findings

- **Review IDs are stable** — Review IDs appear consistent and suitable for deduplication.
- **Core fields are reliably present** — Ratings, timestamps, app IDs, and review text are consistently available.
- **App version data is partially missing** — Version information is present for many records but not universally populated.
- **Developer replies are sparse** — Reply fields are largely empty, which is expected for public review data.
- **Identical review text can appear across users** — Repeated text within an app should be treated as a quality flag, not as evidence of duplicate records.
- **Newest-sort bias** — Because reviews are collected using the Newest sorting option, the dataset primarily reflects recent user feedback rather than a complete historical record.

---



## Data Quality Considerations

When using this dataset or extending the collection pipeline, keep the following in mind:


| Consideration      | Recommendation                                                                                                        |
| ------------------ | --------------------------------------------------------------------------------------------------------------------- |
| Deduplication key  | Use `reviewId` as the primary deduplication identifier.                                                               |
| Text duplication   | Flag identical `content` within the same app for manual or automated review; do not automatically drop as duplicates. |
| App version        | Treat `appVersion` as optional metadata; do not assume full coverage.                                                 |
| Developer replies  | Model reply fields as sparse optional attributes.                                                                     |
| Temporal coverage  | Account for recency bias introduced by Newest-sort collection.                                                        |
| Multi-app analysis | Always join or filter on `app_id` to avoid cross-app aggregation errors.                                              |


---



## Current Limitations

- **Recency bias** — The dataset represents recently posted reviews, not the full review history of each application.
- **Incomplete version metadata** — App version fields are not available for all records.
- **Prototype scope** — Collection, storage, and quality checks are designed for evaluation, not production-scale ingestion.
- **Single data source** — Only Google Play reviews are included; other app store sources are out of scope for this prototype.
- **No automated pipeline** — Ingestion is script-based with no scheduling, monitoring, or incremental update mechanism.

---



## Future Work

The next stage of this project is to design a scalable database schema and ingestion architecture that supports:

- **Multiple data sources** — Google Play, Apple App Store, and other review platforms
- **Multiple applications** — Unified storage and querying across apps
- **Recurring ingestion runs** — Scheduled, incremental data collection
- **Processed review storage** — Cleaned, normalized, and enriched review tables
- **Data quality flags** — Automated tagging of missing fields, text duplicates, and anomalies
- **Downstream analytics** — Sentiment analysis, topic modeling, and product analytics

---



## How to Run



### 1. Clone the repository

```bash
git clone <repository-url>
cd google-play-review-prototype
```



### 2. Create a virtual environment and install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```



### 3. Collect reviews

```bash
python src/collect_reviews.py
```

Output is saved to `data/raw/google_play_reviews_sample.csv`.

### 4. Run exploratory data analysis

```bash
jupyter notebook notebooks/eda_google_play_reviews.ipynb
```

Open the notebook and execute cells sequentially to reproduce the data quality analysis.

---



## Requirements


| Package                | Purpose                                  |
| ---------------------- | ---------------------------------------- |
| `google-play-scraper`  | Fetch reviews from the Google Play Store |
| `pandas`               | Data manipulation and CSV export         |
| `matplotlib`           | Visualization in the EDA notebook        |
| `jupyter` / `notebook` | Interactive notebook environment         |


Install all dependencies with:

```bash
pip install -r requirements.txt
```

**Python version:** 3.10 or later recommended.

---



## Project Structure


| Path                                      | Description                                                      |
| ----------------------------------------- | ---------------------------------------------------------------- |
| `src/collect_reviews.py`                  | Batch review collection script                                   |
| `data/raw/`                               | Raw CSV exports from collection runs                             |
| `data/processed/`                         | Cleaned or transformed review datasets                           |
| `notebooks/eda_google_play_reviews.ipynb` | Exploratory data analysis and quality checks                     |
| `requirements.txt`                        | Python package dependencies                                      |
| `.gitignore`                              | Excludes virtual environments, checkpoints, and local data files |


---

