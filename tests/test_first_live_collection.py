"""
First live collection scenario: empty review store → first successful pipeline run.

Collector is mocked with fixed records for stability; ingestion pipeline and SQLite
writes are real.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DB_DIR = SRC / "db"
for path in (str(SRC), str(DB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from ingest_live_app import ingest_live_apps  # noqa: E402
from init_db import init_db  # noqa: E402
from load_sample import ensure_app, ensure_data_source, utc_now  # noqa: E402

APP_A = ("com.example.first.a", "First A")
APP_B = ("com.example.first.b", "First B")

# Fixed catalog returned by the mocked collector (per package).
FIXED_REVIEWS: dict[str, list[dict]] = {
    APP_A[0]: [
        {
            "reviewId": "first-a-1",
            "userName": "u1",
            "userImage": "",
            "content": "Great app",
            "score": 5,
            "thumbsUpCount": 1,
            "reviewCreatedVersion": "2.0.0",
            "at": datetime(2026, 3, 1, 9, 0, 0),
            "replyContent": "Thanks!",
            "repliedAt": datetime(2026, 3, 1, 10, 0, 0),
            "appVersion": "2.0.0",
        },
        {
            "reviewId": "first-a-2",
            "userName": "u2",
            "userImage": "",
            "content": "Needs work",
            "score": 2,
            "thumbsUpCount": 0,
            "reviewCreatedVersion": None,
            "at": datetime(2026, 3, 1, 9, 5, 0),
            "replyContent": None,
            "repliedAt": None,
            "appVersion": None,  # triggers missing_app_version
        },
        {
            "reviewId": "first-a-3",
            "userName": "u3",
            "userImage": "",
            "content": "   ",  # empty after clean → empty_review_text
            "score": 4,
            "thumbsUpCount": 0,
            "reviewCreatedVersion": "2.0.0",
            "at": datetime(2026, 3, 1, 9, 10, 0),
            "replyContent": None,
            "repliedAt": None,
            "appVersion": "2.0.0",
        },
    ],
    APP_B[0]: [
        {
            "reviewId": "first-b-1",
            "userName": "u4",
            "userImage": "",
            "content": "Solid",
            "score": 5,
            "thumbsUpCount": 2,
            "reviewCreatedVersion": "1.1.0",
            "at": datetime(2026, 3, 1, 11, 0, 0),
            "replyContent": None,
            "repliedAt": None,
            "appVersion": "1.1.0",
        },
        {
            "reviewId": "first-b-2",
            "userName": "u5",
            "userImage": "",
            "content": "Solid",  # duplicate text within app B
            "score": 4,
            "thumbsUpCount": 0,
            "reviewCreatedVersion": "1.1.0",
            "at": datetime(2026, 3, 1, 11, 5, 0),
            "replyContent": None,
            "repliedAt": None,
            "appVersion": "1.1.0",
        },
    ],
}

EXPECTED_RAW = sum(len(rows) for rows in FIXED_REVIEWS.values())  # 5


def _prepare_empty_review_db(db_path: Path) -> None:
    """Schema + registered apps only; no runs/reviews yet (first collection)."""
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        now = utc_now()
        source_id = ensure_data_source(conn, now)
        for package_id, app_name in (APP_A, APP_B):
            ensure_app(conn, source_id, package_id, app_name, now)
        conn.commit()

        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM reviews_raw").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM review_observations").fetchone()[0] == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM reviews_processed").fetchone()[0] == 0
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM review_quality_flags").fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def _mock_collector(app_name: str, package_id: str, n_reviews: int) -> list[dict]:
    rows = FIXED_REVIEWS[package_id]
    # Respect n_reviews but scenario uses full fixed catalogs.
    selected = rows[:n_reviews] if n_reviews < len(rows) else list(rows)
    out = []
    for row in selected:
        item = dict(row)
        item["app_name"] = app_name
        item["app_id"] = package_id
        out.append(item)
    return out


def test_first_live_collection_on_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "first_live_collection.db"
    _prepare_empty_review_db(db_path)

    summary = ingest_live_apps(
        db_path=db_path,
        apps=[
            {"package_id": APP_A[0]},
            {"package_id": APP_B[0]},
        ],
        n_reviews=10,
        collect_fn=_mock_collector,
    )

    assert summary["status"] == "completed"
    assert summary["apps_completed"] == 2
    assert summary["apps_failed"] == 0

    conn = sqlite3.connect(db_path)
    try:
        run_count = conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0]
        assert run_count == 1

        run = conn.execute(
            """
            SELECT run_id, status, total_fetched, total_inserted, skipped_duplicates,
                   completed_at
            FROM ingestion_runs
            """
        ).fetchone()
        run_id, run_status, total_fetched, total_inserted, skipped, completed_at = run
        assert run_id == summary["run_id"]
        assert run_status == "completed"
        assert completed_at is not None
        assert total_fetched == EXPECTED_RAW
        assert total_inserted == EXPECTED_RAW
        assert skipped == 0

        app_results = conn.execute(
            """
            SELECT a.source_app_identifier, r.status, r.fetched_count,
                   r.inserted_count, r.skipped_count, r.error_message
            FROM ingestion_run_apps r
            JOIN apps a ON a.app_id = r.app_id
            WHERE r.run_id = ?
            ORDER BY a.source_app_identifier
            """,
            (run_id,),
        ).fetchall()
        assert len(app_results) == 2
        by_pkg = {row[0]: row for row in app_results}

        assert by_pkg[APP_A[0]][1] == "completed"
        assert by_pkg[APP_A[0]][2] == 3  # fetched
        assert by_pkg[APP_A[0]][3] == 3  # inserted
        assert by_pkg[APP_A[0]][4] == 0  # skipped
        assert by_pkg[APP_A[0]][5] is None

        assert by_pkg[APP_B[0]][1] == "completed"
        assert by_pkg[APP_B[0]][2] == 2
        assert by_pkg[APP_B[0]][3] == 2
        assert by_pkg[APP_B[0]][4] == 0
        assert by_pkg[APP_B[0]][5] is None

        raw_count = conn.execute("SELECT COUNT(*) FROM reviews_raw").fetchone()[0]
        distinct_keys = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT app_id, source_review_id
                FROM reviews_raw
                GROUP BY app_id, source_review_id
            )
            """
        ).fetchone()[0]
        duplicate_groups = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT app_id, source_review_id
                FROM reviews_raw
                GROUP BY app_id, source_review_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        assert raw_count == EXPECTED_RAW
        assert distinct_keys == EXPECTED_RAW
        assert duplicate_groups == 0

        obs_count = conn.execute(
            "SELECT COUNT(*) FROM review_observations WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        assert obs_count == EXPECTED_RAW

        processed_count = conn.execute(
            "SELECT COUNT(*) FROM reviews_processed"
        ).fetchone()[0]
        assert processed_count == EXPECTED_RAW

        # has_developer_reply availability on processed rows
        with_reply = conn.execute(
            "SELECT COUNT(*) FROM reviews_processed WHERE has_developer_reply = 1"
        ).fetchone()[0]
        without_reply = conn.execute(
            "SELECT COUNT(*) FROM reviews_processed WHERE has_developer_reply = 0"
        ).fetchone()[0]
        assert with_reply == 1
        assert without_reply == 4

        flag_totals = {
            flag_type: count
            for flag_type, count in conn.execute(
                """
                SELECT flag_type, COUNT(*)
                FROM review_quality_flags
                GROUP BY flag_type
                ORDER BY flag_type
                """
            )
        }
        # Existing rules: missing version (a-2), empty text (a-3), duplicate text (b-1 & b-2)
        assert flag_totals.get("missing_app_version", 0) == 1
        assert flag_totals.get("empty_review_text", 0) == 1
        assert flag_totals.get("duplicate_text_within_app", 0) == 2
        assert flag_totals.get("invalid_rating", 0) == 0
        flags_total = sum(flag_totals.values())

        print("\nFirst live collection key counts")
        print(f"  ingestion_runs:              {run_count}")
        print(f"  run_status:                  {run_status}")
        print(f"  per_app_results:             {len(app_results)}")
        print(f"  total_fetched:               {total_fetched}")
        print(f"  total_inserted:              {total_inserted}")
        print(f"  skipped_duplicates:          {skipped}")
        print(f"  reviews_raw:                 {raw_count}")
        print(f"  review_observations:         {obs_count}")
        print(f"  reviews_processed:           {processed_count}")
        print(f"  has_developer_reply=1:       {with_reply}")
        print(f"  review_quality_flags:        {flags_total}")
        print(f"  flag_totals:                 {flag_totals}")
        print(f"  duplicate_raw_groups:        {duplicate_groups}")
        for package_id, row in by_pkg.items():
            print(
                f"  app {package_id}: status={row[1]} "
                f"fetched={row[2]} inserted={row[3]} skipped={row[4]}"
            )
    finally:
        conn.close()
