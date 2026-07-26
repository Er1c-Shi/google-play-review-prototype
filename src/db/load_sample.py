"""Load the controlled review sample into SQLite (raw, processed, quality flags)."""

from __future__ import annotations

import argparse
import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from init_db import DEFAULT_DB_PATH, init_db

DEFAULT_SAMPLE_PATH = (
    Path("data") / "samples" / "google_play_reviews_integration_sample.csv"
)

GOOGLE_PLAY_SOURCE_CODE = "google_play"
GOOGLE_PLAY_SOURCE_NAME = "Google Play"
PROCESSING_VERSION = "v1-basic"

FLAG_SEVERITY = {
    "missing_developer_reply": "info",
    "missing_app_version": "info",
    "duplicate_text_within_app": "warning",
    "empty_review_text": "warning",
    "invalid_rating": "error",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value if value else None


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
            "Controlled sample load for database integration testing",
        ),
    )
    return int(cursor.lastrowid)


def clean_review_text(content: str | None) -> tuple[str | None, int]:
    """Trim whitespace; return cleaned text and character length."""
    if content is None:
        return None, 0
    cleaned = content.strip()
    if not cleaned:
        return None, 0
    return cleaned, len(cleaned)


def create_processed_review(
    conn: sqlite3.Connection,
    review_raw_id: int,
    content: str | None,
    score: int | None,
    processed_at: str,
) -> int | None:
    """Insert a processed row for a raw review. Returns review_processed_id if created."""
    cleaned_content, text_length = clean_review_text(content)
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO reviews_processed (
            review_raw_id,
            cleaned_content,
            normalized_score,
            text_length,
            language_code,
            processed_at,
            processing_version
        )
        VALUES (?, ?, ?, ?, NULL, ?, ?)
        """,
        (
            review_raw_id,
            cleaned_content,
            score,
            text_length,
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
            r.app_version,
            r.reply_content
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
        reply_content,
    ) in rows:
        pending: list[tuple[str, str | None]] = []

        if app_version is None:
            pending.append(("missing_app_version", None))
        if reply_content is None:
            pending.append(("missing_developer_reply", None))
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


def load_sample(
    db_path: Path = DEFAULT_DB_PATH,
    sample_path: Path = DEFAULT_SAMPLE_PATH,
) -> dict:
    """Load sample CSV into reviews_raw, processed rows, and basic quality flags."""
    init_db(db_path)

    sample_path = Path(sample_path)
    if not sample_path.is_file():
        raise FileNotFoundError(f"Sample file not found: {sample_path}")

    with sample_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    if not rows:
        raise ValueError(f"Sample file is empty: {sample_path}")

    started_at = utc_now()
    collected_at = started_at

    # Discover apps from the sample (stable order by first appearance).
    apps_in_sample: list[tuple[str, str]] = []
    seen_app_ids: set[str] = set()
    for row in rows:
        package_id = row["app_id"]
        if package_id not in seen_app_ids:
            seen_app_ids.add(package_id)
            apps_in_sample.append((package_id, row["app_name"]))

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
                target_review_count=len(rows),
            )

            total_fetched = len(rows)
            total_inserted = 0
            skipped_duplicates = 0
            processed_created = 0
            newly_inserted_raw: list[tuple[int, str | None, int | None]] = []

            insert_sql = """
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
            """

            for row in rows:
                package_id = row["app_id"]
                app_id = app_id_by_package[package_id]
                source_review_id = row["reviewId"]

                score_raw = empty_to_none(row.get("score"))
                thumbs_raw = empty_to_none(row.get("thumbsUpCount"))
                app_version = empty_to_none(row.get("appVersion")) or empty_to_none(
                    row.get("reviewCreatedVersion")
                )
                content = empty_to_none(row.get("content"))
                score = int(score_raw) if score_raw is not None else None

                payload = dict(row)
                raw_payload_json = json.dumps(payload, ensure_ascii=False)

                cursor = conn.execute(
                    insert_sql,
                    (
                        run_id,
                        app_id,
                        source_review_id,
                        content,
                        score,
                        int(thumbs_raw) if thumbs_raw is not None else None,
                        empty_to_none(row.get("at")),
                        app_version,
                        empty_to_none(row.get("replyContent")),
                        empty_to_none(row.get("repliedAt")),
                        collected_at,
                        raw_payload_json,
                    ),
                )

                if cursor.rowcount == 1:
                    total_inserted += 1
                    newly_inserted_raw.append(
                        (int(cursor.lastrowid), content, score)
                    )
                else:
                    skipped_duplicates += 1

            processed_at = utc_now()
            newly_processed_ids: list[int] = []
            for review_raw_id, content, score in newly_inserted_raw:
                processed_id = create_processed_review(
                    conn, review_raw_id, content, score, processed_at
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
                    "completed",
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

        inserted_by_app = {
            row[0]: row[1]
            for row in conn.execute(
                """
                SELECT app_id, COUNT(*) AS n
                FROM reviews_raw
                WHERE ingestion_run_id = ?
                GROUP BY app_id
                """,
                (run_id,),
            ).fetchall()
        }

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

        return {
            "db_path": str(Path(db_path).resolve()),
            "sample_path": str(sample_path.resolve()),
            "source_id": source_id,
            "run_id": run_id,
            "app_count": len(apps_in_sample),
            "apps": [
                {
                    "app_name": app_name,
                    "package_id": package_id,
                    "inserted": inserted_by_app.get(app_id_by_package[package_id], 0),
                }
                for package_id, app_name in apps_in_sample
            ],
            "total_fetched": total_fetched,
            "total_inserted": total_inserted,
            "skipped_duplicates": skipped_duplicates,
            "processed_created": processed_created,
            "duplicate_processed_groups": int(duplicate_processed),
            "flags_created": flags_created,
            "flags_created_by_type": dict(flags_created_by_type),
            "flag_totals": flag_totals,
            "duplicate_flag_groups": int(duplicate_flag_groups),
            "status": "completed",
            "completed_at": completed_at,
        }
    finally:
        conn.close()


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
            f"{app['inserted']} inserted"
        )
    print(f"  total_fetched:      {summary['total_fetched']}")
    print(f"  total_inserted:     {summary['total_inserted']}")
    print(f"  skipped_duplicates: {summary['skipped_duplicates']}")
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


if __name__ == "__main__":
    main()
