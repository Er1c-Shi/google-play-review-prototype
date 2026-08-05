"""Load Google Play reviews into SQLite (raw, processed, quality flags)."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from init_db import DEFAULT_DB_PATH, init_db

# Shared record contract lives one level above src/db.
_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from review_records import (  # noqa: E402
    ReviewRecord,
    adapt_csv_rows_to_records,
    empty_to_none,
    partition_review_records,
    read_csv_rows,
)

DEFAULT_SAMPLE_PATH = (
    Path("data") / "samples" / "google_play_reviews_integration_sample.csv"
)

GOOGLE_PLAY_SOURCE_CODE = "google_play"
GOOGLE_PLAY_SOURCE_NAME = "Google Play"
PROCESSING_VERSION = "v1-basic"

FLAG_SEVERITY = {
    "missing_app_version": "info",
    "duplicate_text_within_app": "warning",
    "empty_review_text": "warning",
    "invalid_rating": "error",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# Re-export for existing call sites / tests that imported empty_to_none here.
__all__ = [
    "FLAG_SEVERITY",
    "load_review_records",
    "load_sample",
    "empty_to_none",
    "compute_has_developer_reply",
]


def ensure_data_source(conn: sqlite3.Connection, created_at: str) -> int:
    row = conn.execute(
        "SELECT source_id FROM data_sources WHERE source_code = ?",
        (GOOGLE_PLAY_SOURCE_CODE,),
    ).fetchone()
    if row:
        return int(row[0])

    cursor = conn.execute(
        """
        INSERT INTO data_sources (source_code, source_name, description, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            GOOGLE_PLAY_SOURCE_CODE,
            GOOGLE_PLAY_SOURCE_NAME,
            "Google Play Store public reviews",
            created_at,
        ),
    )
    return int(cursor.lastrowid)


def ensure_app(
    conn: sqlite3.Connection,
    source_id: int,
    source_app_identifier: str,
    app_name: str,
    created_at: str,
) -> int:
    row = conn.execute(
        """
        SELECT app_id FROM apps
        WHERE source_id = ? AND source_app_identifier = ?
        """,
        (source_id, source_app_identifier),
    ).fetchone()
    if row:
        return int(row[0])

    cursor = conn.execute(
        """
        INSERT INTO apps (source_id, source_app_identifier, app_name, metadata_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source_id, source_app_identifier, app_name, None, created_at),
    )
    return int(cursor.lastrowid)


def create_ingestion_run(
    conn: sqlite3.Connection,
    source_id: int,
    started_at: str,
    app_count: int,
    target_review_count: int,
    *,
    notes: str = "Controlled sample load for database integration testing",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO ingestion_runs (
            source_id,
            started_at,
            completed_at,
            status,
            sort_order,
            country,
            language,
            target_review_count,
            app_count,
            total_fetched,
            total_inserted,
            skipped_duplicates,
            notes
        )
        VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?)
        """,
        (
            source_id,
            started_at,
            "running",
            "newest",
            "us",
            "en",
            target_review_count,
            app_count,
            notes,
        ),
    )
    return int(cursor.lastrowid)


def start_ingestion_run_app(
    conn: sqlite3.Connection,
    run_id: int,
    app_id: int,
    started_at: str,
) -> int:
    """Create a per-app result row in running state for this ingestion run."""
    cursor = conn.execute(
        """
        INSERT INTO ingestion_run_apps (
            run_id,
            app_id,
            status,
            fetched_count,
            inserted_count,
            skipped_count,
            error_message,
            started_at,
            completed_at
        )
        VALUES (?, ?, 'running', 0, 0, 0, NULL, ?, NULL)
        """,
        (run_id, app_id, started_at),
    )
    return int(cursor.lastrowid)


def finish_ingestion_run_app(
    conn: sqlite3.Connection,
    run_id: int,
    app_id: int,
    *,
    fetched_count: int,
    inserted_count: int,
    skipped_count: int,
    error_message: str | None,
    completed_at: str,
) -> None:
    """
    Finalize per-app counts and status.

    Status rules:
    - failed: the app had work to do but nothing was inserted or skipped as a
      duplicate (every attempt errored, or fetched_count is 0 with an error)
    - completed: otherwise (including partial per-review errors, reported in
      error_message when present)
    """
    error_message = empty_to_none(error_message)
    if error_message is not None and inserted_count == 0 and skipped_count == 0:
        status = "failed"
    else:
        status = "completed"

    conn.execute(
        """
        UPDATE ingestion_run_apps
        SET
            status = ?,
            fetched_count = ?,
            inserted_count = ?,
            skipped_count = ?,
            error_message = ?,
            completed_at = ?
        WHERE run_id = ? AND app_id = ?
        """,
        (
            status,
            fetched_count,
            inserted_count,
            skipped_count,
            error_message,
            completed_at,
            run_id,
            app_id,
        ),
    )


def clean_review_text(content: str | None) -> tuple[str | None, int]:
    """Trim whitespace; return cleaned text and character length."""
    if content is None:
        return None, 0
    cleaned = content.strip()
    if not cleaned:
        return None, 0
    return cleaned, len(cleaned)


def compute_has_developer_reply(reply_content: str | None) -> bool:
    """
    Availability feature: True when reply_content has non-whitespace text.

    This is not a quality flag — missing replies are expected for most reviews.
    """
    if reply_content is None:
        return False
    return bool(reply_content.strip())


def get_review_raw_id(
    conn: sqlite3.Connection,
    app_id: int,
    source_review_id: str,
) -> int | None:
    """Look up an existing reviews_raw row by the deduplication key."""
    row = conn.execute(
        """
        SELECT review_raw_id
        FROM reviews_raw
        WHERE app_id = ? AND source_review_id = ?
        """,
        (app_id, source_review_id),
    ).fetchone()
    if row is None:
        return None
    return int(row[0])


def insert_review_raw(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    app_id: int,
    source_review_id: str,
    content: str | None,
    score: int | None,
    thumbs_up_count: int | None,
    review_created_at: str | None,
    app_version: str | None,
    reply_content: str | None,
    replied_at: str | None,
    collected_at: str,
    raw_payload_json: str,
) -> int | None:
    """
    Insert a reviews_raw row if new.

    Deduplication remains UNIQUE (app_id, source_review_id) via INSERT OR IGNORE.
    Returns the new review_raw_id when inserted, otherwise None.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO reviews_raw (
            ingestion_run_id,
            app_id,
            source_review_id,
            content,
            score,
            thumbs_up_count,
            review_created_at,
            app_version,
            reply_content,
            replied_at,
            collected_at,
            raw_payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            app_id,
            source_review_id,
            content,
            score,
            thumbs_up_count,
            review_created_at,
            app_version,
            reply_content,
            replied_at,
            collected_at,
            raw_payload_json,
        ),
    )
    if cursor.rowcount != 1:
        return None
    return int(cursor.lastrowid)


def record_review_observation(
    conn: sqlite3.Connection,
    run_id: int,
    review_raw_id: int,
    observed_at: str,
) -> bool:
    """
    Record that a review was observed in an ingestion run.

    Uses INSERT OR IGNORE so the same (run_id, review_raw_id) is never duplicated
    and in-run CSV duplicates do not abort the load. Returns True if a new row
    was created.
    """
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO review_observations (run_id, review_raw_id, observed_at)
        VALUES (?, ?, ?)
        """,
        (run_id, review_raw_id, observed_at),
    )
    return cursor.rowcount == 1


def ingest_review_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    app_id: int,
    source_review_id: str,
    content: str | None,
    score: int | None,
    thumbs_up_count: int | None,
    review_created_at: str | None,
    app_version: str | None,
    reply_content: str | None,
    replied_at: str | None,
    collected_at: str,
    raw_payload_json: str,
    observed_at: str,
) -> tuple[int, bool, bool]:
    """
    Upsert raw review identity and always attempt an observation for this run.

    Returns (review_raw_id, raw_inserted, observation_created).
    """
    review_raw_id = insert_review_raw(
        conn,
        run_id=run_id,
        app_id=app_id,
        source_review_id=source_review_id,
        content=content,
        score=score,
        thumbs_up_count=thumbs_up_count,
        review_created_at=review_created_at,
        app_version=app_version,
        reply_content=reply_content,
        replied_at=replied_at,
        collected_at=collected_at,
        raw_payload_json=raw_payload_json,
    )
    raw_inserted = review_raw_id is not None
    if review_raw_id is None:
        existing_id = get_review_raw_id(conn, app_id, source_review_id)
        if existing_id is None:
            raise RuntimeError(
                f"Failed to resolve reviews_raw for app_id={app_id}, "
                f"source_review_id={source_review_id!r}"
            )
        review_raw_id = existing_id

    observation_created = record_review_observation(
        conn, run_id, review_raw_id, observed_at
    )
    return review_raw_id, raw_inserted, observation_created


def create_processed_review(
    conn: sqlite3.Connection,
    review_raw_id: int,
    content: str | None,
    score: int | None,
    reply_content: str | None,
    processed_at: str,
) -> int | None:
    """Insert a processed row for a raw review. Returns review_processed_id if created."""
    cleaned_content, text_length = clean_review_text(content)
    has_reply = 1 if compute_has_developer_reply(reply_content) else 0
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO reviews_processed (
            review_raw_id,
            cleaned_content,
            normalized_score,
            text_length,
            language_code,
            has_developer_reply,
            processed_at,
            processing_version
        )
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?)
        """,
        (
            review_raw_id,
            cleaned_content,
            score,
            text_length,
            has_reply,
            processed_at,
            PROCESSING_VERSION,
        ),
    )
    if cursor.rowcount != 1:
        return None
    return int(cursor.lastrowid)


def insert_quality_flag(
    conn: sqlite3.Connection,
    review_processed_id: int,
    flag_type: str,
    detected_at: str,
    flag_value: str | None = None,
) -> bool:
    """Insert one quality flag. Returns True if a new flag row was created."""
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO review_quality_flags (
            review_processed_id,
            flag_type,
            flag_value,
            severity,
            detected_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            review_processed_id,
            flag_type,
            flag_value,
            FLAG_SEVERITY[flag_type],
            detected_at,
        ),
    )
    return cursor.rowcount == 1


def duplicate_text_keys(conn: sqlite3.Connection) -> set[tuple[int, str]]:
    """Return (app_id, content) pairs that appear more than once within an app."""
    rows = conn.execute(
        """
        SELECT app_id, content
        FROM reviews_raw
        WHERE content IS NOT NULL AND TRIM(content) != ''
        GROUP BY app_id, content
        HAVING COUNT(*) > 1
        """
    ).fetchall()
    return {(int(app_id), content) for app_id, content in rows}


def generate_quality_flags(
    conn: sqlite3.Connection,
    review_processed_ids: list[int],
    detected_at: str,
) -> Counter[str]:
    """Generate basic quality flags for the given processed reviews."""
    created: Counter[str] = Counter()
    if not review_processed_ids:
        return created

    duplicates = duplicate_text_keys(conn)
    placeholders = ",".join("?" * len(review_processed_ids))
    rows = conn.execute(
        f"""
        SELECT
            p.review_processed_id,
            p.cleaned_content,
            p.normalized_score,
            r.app_id,
            r.content,
            r.app_version
        FROM reviews_processed p
        JOIN reviews_raw r ON r.review_raw_id = p.review_raw_id
        WHERE p.review_processed_id IN ({placeholders})
        """,
        review_processed_ids,
    ).fetchall()

    for (
        review_processed_id,
        cleaned_content,
        normalized_score,
        app_id,
        content,
        app_version,
    ) in rows:
        pending: list[tuple[str, str | None]] = []

        if app_version is None:
            pending.append(("missing_app_version", None))
        if cleaned_content is None:
            pending.append(("empty_review_text", None))
        if normalized_score is None or not (1 <= int(normalized_score) <= 5):
            pending.append(
                (
                    "invalid_rating",
                    None if normalized_score is None else str(normalized_score),
                )
            )
        if content is not None and (int(app_id), content) in duplicates:
            pending.append(("duplicate_text_within_app", None))

        for flag_type, flag_value in pending:
            if insert_quality_flag(
                conn, int(review_processed_id), flag_type, detected_at, flag_value
            ):
                created[flag_type] += 1

    return created


def load_review_records(
    records: list[ReviewRecord] | list[Any],
    db_path: Path = DEFAULT_DB_PATH,
    *,
    source_path: str | None = None,
    prior_errors: list[dict] | None = None,
) -> dict:
    """
    Load standardized review records into SQLite.

    This is the shared bottom layer for CSV and (future) live collector paths.
    Deduplication, observations, processing, and quality flags are applied once
    here. Invalid input rows are skipped and reported in `record_errors` instead
    of failing the whole batch silently.
    """
    if not isinstance(records, list):
        raise TypeError(
            f"records must be a list of review mappings, got {type(records).__name__}"
        )

    record_errors: list[dict] = list(prior_errors or [])
    valid_records, validation_errors = partition_review_records(records)
    record_errors.extend(validation_errors)

    # `records` are already-adapted candidates; prior_errors are rows rejected
    # before reaching this function (e.g. CSV adapt failures).
    total_input = len(records) + len(prior_errors or [])

    if not valid_records:
        raise ValueError(
            "No valid review records to load"
            + (f" ({len(record_errors)} invalid)" if record_errors else "")
        )

    init_db(db_path)
    started_at = utc_now()
    collected_at = started_at

    apps_in_sample: list[tuple[str, str]] = []
    seen_app_ids: set[str] = set()
    for record in valid_records:
        package_id = record["package_id"]
        if package_id not in seen_app_ids:
            seen_app_ids.add(package_id)
            apps_in_sample.append((package_id, record["app_name"]))

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = None  # manual transaction control
        conn.execute("BEGIN")

        try:
            source_id = ensure_data_source(conn, started_at)

            app_id_by_package: dict[str, int] = {}
            for package_id, app_name in apps_in_sample:
                app_id_by_package[package_id] = ensure_app(
                    conn, source_id, package_id, app_name, started_at
                )

            run_id = create_ingestion_run(
                conn,
                source_id=source_id,
                started_at=started_at,
                app_count=len(apps_in_sample),
                target_review_count=len(valid_records),
            )

            for package_id, _app_name in apps_in_sample:
                start_ingestion_run_app(
                    conn,
                    run_id,
                    app_id_by_package[package_id],
                    started_at,
                )

            total_fetched = len(valid_records)
            total_inserted = 0
            skipped_duplicates = 0
            observations_created = 0
            processed_created = 0
            ingest_errors = 0
            newly_inserted_raw: list[
                tuple[int, str | None, int | None, str | None]
            ] = []
            per_app_fetched: Counter[int] = Counter()
            per_app_inserted: Counter[int] = Counter()
            per_app_skipped: Counter[int] = Counter()
            per_app_errors: dict[int, list[str]] = {
                app_id_by_package[package_id]: [] for package_id, _ in apps_in_sample
            }

            for record in valid_records:
                package_id = record["package_id"]
                app_id = app_id_by_package[package_id]
                content = record["content"]
                score = record["score"]
                reply_content = record["reply_content"]
                per_app_fetched[app_id] += 1

                conn.execute("SAVEPOINT ingest_one")
                try:
                    raw_payload_json = json.dumps(
                        record["raw_payload"], ensure_ascii=False
                    )
                    review_raw_id, raw_inserted, observation_created = (
                        ingest_review_for_run(
                            conn,
                            run_id=run_id,
                            app_id=app_id,
                            source_review_id=record["source_review_id"],
                            content=content,
                            score=score,
                            thumbs_up_count=record["thumbs_up_count"],
                            review_created_at=record["review_created_at"],
                            app_version=record["app_version"],
                            reply_content=reply_content,
                            replied_at=record["replied_at"],
                            collected_at=collected_at,
                            raw_payload_json=raw_payload_json,
                            observed_at=collected_at,
                        )
                    )
                    conn.execute("RELEASE SAVEPOINT ingest_one")
                except Exception as exc:  # noqa: BLE001 - keep batch alive
                    conn.execute("ROLLBACK TO SAVEPOINT ingest_one")
                    ingest_errors += 1
                    message = str(exc)
                    record_errors.append(
                        {
                            "index": None,
                            "stage": "ingest",
                            "source_review_id": record["source_review_id"],
                            "error": message,
                        }
                    )
                    per_app_errors[app_id].append(
                        f"{record['source_review_id']}: {message}"
                    )
                    continue

                if raw_inserted:
                    total_inserted += 1
                    per_app_inserted[app_id] += 1
                    newly_inserted_raw.append(
                        (review_raw_id, content, score, reply_content)
                    )
                else:
                    skipped_duplicates += 1
                    per_app_skipped[app_id] += 1

                if observation_created:
                    observations_created += 1

            processed_at = utc_now()
            newly_processed_ids: list[int] = []
            for review_raw_id, content, score, reply_content in newly_inserted_raw:
                processed_id = create_processed_review(
                    conn,
                    review_raw_id,
                    content,
                    score,
                    reply_content,
                    processed_at,
                )
                if processed_id is not None:
                    processed_created += 1
                    newly_processed_ids.append(processed_id)

            detected_at = utc_now()
            flags_created_by_type = generate_quality_flags(
                conn, newly_processed_ids, detected_at
            )
            flags_created = sum(flags_created_by_type.values())

            completed_at = utc_now()
            app_results: list[dict] = []
            for package_id, app_name in apps_in_sample:
                app_id = app_id_by_package[package_id]
                app_error_lines = per_app_errors[app_id]
                if not app_error_lines:
                    app_error_message = None
                elif len(app_error_lines) == 1:
                    app_error_message = app_error_lines[0][:500]
                else:
                    app_error_message = (
                        f"{len(app_error_lines)} review ingest errors; "
                        f"first: {app_error_lines[0][:400]}"
                    )
                finish_ingestion_run_app(
                    conn,
                    run_id,
                    app_id,
                    fetched_count=per_app_fetched[app_id],
                    inserted_count=per_app_inserted[app_id],
                    skipped_count=per_app_skipped[app_id],
                    error_message=app_error_message,
                    completed_at=completed_at,
                )
                row = conn.execute(
                    """
                    SELECT status, fetched_count, inserted_count, skipped_count,
                           error_message
                    FROM ingestion_run_apps
                    WHERE run_id = ? AND app_id = ?
                    """,
                    (run_id, app_id),
                ).fetchone()
                app_results.append(
                    {
                        "app_name": app_name,
                        "package_id": package_id,
                        "app_id": app_id,
                        "status": row[0],
                        "fetched_count": int(row[1]),
                        "inserted_count": int(row[2]),
                        "skipped_count": int(row[3]),
                        "error_message": row[4],
                    }
                )

            status = "completed" if not record_errors else "completed_with_errors"
            conn.execute(
                """
                UPDATE ingestion_runs
                SET
                    completed_at = ?,
                    status = ?,
                    total_fetched = ?,
                    total_inserted = ?,
                    skipped_duplicates = ?
                WHERE run_id = ?
                """,
                (
                    completed_at,
                    status,
                    total_fetched,
                    total_inserted,
                    skipped_duplicates,
                    run_id,
                ),
            )

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        duplicate_processed = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT review_raw_id
                FROM reviews_processed
                GROUP BY review_raw_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]

        flag_totals = {
            flag_type: count
            for flag_type, count in conn.execute(
                """
                SELECT flag_type, COUNT(*) AS n
                FROM review_quality_flags
                GROUP BY flag_type
                ORDER BY flag_type
                """
            ).fetchall()
        }
        duplicate_flag_groups = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT review_processed_id, flag_type
                FROM review_quality_flags
                GROUP BY review_processed_id, flag_type
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]

        observations_for_run = conn.execute(
            """
            SELECT COUNT(*) FROM review_observations WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()[0]

        summary = {
            "db_path": str(Path(db_path).resolve()),
            "source_id": source_id,
            "run_id": run_id,
            "app_count": len(apps_in_sample),
            "apps": [
                {
                    "app_name": app["app_name"],
                    "package_id": app["package_id"],
                    "app_id": app["app_id"],
                    "status": app["status"],
                    "fetched_count": app["fetched_count"],
                    "inserted": app["inserted_count"],
                    "inserted_count": app["inserted_count"],
                    "skipped_count": app["skipped_count"],
                    "error_message": app["error_message"],
                }
                for app in app_results
            ],
            "app_results": app_results,
            "total_input": total_input,
            "total_fetched": total_fetched,
            "total_inserted": total_inserted,
            "skipped_duplicates": skipped_duplicates,
            "invalid_records": len(record_errors),
            "ingest_errors": ingest_errors,
            "record_errors": record_errors,
            "observations_created": observations_created,
            "observations_for_run": int(observations_for_run),
            "processed_created": processed_created,
            "duplicate_processed_groups": int(duplicate_processed),
            "flags_created": flags_created,
            "flags_created_by_type": dict(flags_created_by_type),
            "flag_totals": flag_totals,
            "duplicate_flag_groups": int(duplicate_flag_groups),
            "status": status,
            "completed_at": completed_at,
        }
        if source_path is not None:
            summary["sample_path"] = source_path
        return summary
    finally:
        conn.close()


def load_sample(
    db_path: Path = DEFAULT_DB_PATH,
    sample_path: Path = DEFAULT_SAMPLE_PATH,
) -> dict:
    """CSV workflow: read + adapt rows, then call the shared record loader."""
    sample_path = Path(sample_path)
    rows = read_csv_rows(sample_path)
    records, adapt_errors = adapt_csv_rows_to_records(rows)
    return load_review_records(
        records,
        db_path=db_path,
        source_path=str(sample_path.resolve()),
        prior_errors=adapt_errors,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Load the controlled Google Play review sample into SQLite."
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database file (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--sample-path",
        type=Path,
        default=DEFAULT_SAMPLE_PATH,
        help=f"Path to the sample CSV (default: {DEFAULT_SAMPLE_PATH})",
    )
    args = parser.parse_args()

    summary = load_sample(db_path=args.db_path, sample_path=args.sample_path)

    print("Sample load completed.")
    print(f"  database:           {summary['db_path']}")
    print(f"  sample:             {summary['sample_path']}")
    print(f"  source_id:          {summary['source_id']}")
    print(f"  run_id:             {summary['run_id']}")
    print(f"  apps:               {summary['app_count']}")
    for app in summary["apps"]:
        print(
            f"    - {app['app_name']} ({app['package_id']}): "
            f"status={app['status']} fetched={app['fetched_count']} "
            f"inserted={app['inserted_count']} skipped={app['skipped_count']}"
        )
    print(f"  total_fetched:      {summary['total_fetched']}")
    print(f"  total_inserted:     {summary['total_inserted']}")
    print(f"  skipped_duplicates: {summary['skipped_duplicates']}")
    print(f"  invalid_records:    {summary['invalid_records']}")
    print(f"  observations_created: {summary['observations_created']}")
    print(f"  observations_for_run: {summary['observations_for_run']}")
    print(f"  processed_created:  {summary['processed_created']}")
    print(
        f"  duplicate_processed_groups: {summary['duplicate_processed_groups']}"
    )
    print(f"  flags_created:      {summary['flags_created']}")
    print("  flag totals:")
    for flag_type in FLAG_SEVERITY:
        count = summary["flag_totals"].get(flag_type, 0)
        print(f"    - {flag_type}: {count}")
    print(f"  duplicate_flag_groups: {summary['duplicate_flag_groups']}")
    print(f"  status:             {summary['status']}")
    print(f"  completed_at:       {summary['completed_at']}")
    if summary["record_errors"]:
        print(f"  record_errors ({len(summary['record_errors'])}):")
        for err in summary["record_errors"][:10]:
            print(
                f"    - [{err.get('stage')}] index={err.get('index')} "
                f"id={err.get('source_review_id')}: {err.get('error')}"
            )
        if len(summary["record_errors"]) > 10:
            print(f"    ... and {len(summary['record_errors']) - 10} more")


if __name__ == "__main__":
    main()
