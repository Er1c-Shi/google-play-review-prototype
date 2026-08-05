"""
Partial-overlap collection scenario: second run shares some reviews with the first.

Run 1 returns A, B, C.
Run 2 returns B, C, D, E.

Collector is mocked; pipeline and SQLite writes are real.
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

PACKAGE_ID = "com.example.overlap.app"
APP_NAME = "Overlap App"

# Stable fixture catalog keyed by review letter id.
REVIEW_FIXTURES: dict[str, dict] = {
    "A": {
        "reviewId": "review-A",
        "userName": "user-A",
        "userImage": "",
        "content": "Review A content",
        "score": 5,
        "thumbsUpCount": 1,
        "reviewCreatedVersion": "1.0.0",
        "at": datetime(2026, 5, 1, 9, 0, 0),
        "replyContent": None,
        "repliedAt": None,
        "appVersion": "1.0.0",
    },
    "B": {
        "reviewId": "review-B",
        "userName": "user-B",
        "userImage": "",
        "content": "Review B content",
        "score": 4,
        "thumbsUpCount": 0,
        "reviewCreatedVersion": "1.0.0",
        "at": datetime(2026, 5, 1, 9, 5, 0),
        "replyContent": "Thanks B",
        "repliedAt": datetime(2026, 5, 1, 10, 0, 0),
        "appVersion": "1.0.0",
    },
    "C": {
        "reviewId": "review-C",
        "userName": "user-C",
        "userImage": "",
        "content": "Review C content",
        "score": 3,
        "thumbsUpCount": 2,
        "reviewCreatedVersion": "1.1.0",
        "at": datetime(2026, 5, 1, 9, 10, 0),
        "replyContent": None,
        "repliedAt": None,
        "appVersion": "1.1.0",
    },
    "D": {
        "reviewId": "review-D",
        "userName": "user-D",
        "userImage": "",
        "content": "Review D content",
        "score": 2,
        "thumbsUpCount": 0,
        "reviewCreatedVersion": "1.2.0",
        "at": datetime(2026, 5, 2, 11, 0, 0),
        "replyContent": None,
        "repliedAt": None,
        "appVersion": "1.2.0",
    },
    "E": {
        "reviewId": "review-E",
        "userName": "user-E",
        "userImage": "",
        "content": "Review E content",
        "score": 1,
        "thumbsUpCount": 0,
        "reviewCreatedVersion": "1.2.0",
        "at": datetime(2026, 5, 2, 11, 5, 0),
        "replyContent": None,
        "repliedAt": None,
        "appVersion": "1.2.0",
    },
}

RUN1_IDS = ("A", "B", "C")
RUN2_IDS = ("B", "C", "D", "E")


class _PartialOverlapCollector:
    """Deterministic collector that returns a fixed letter set per call."""

    def __init__(self) -> None:
        self._call = 0
        self._sequences = (RUN1_IDS, RUN2_IDS)

    def __call__(self, app_name: str, package_id: str, n_reviews: int) -> list[dict]:
        assert package_id == PACKAGE_ID
        letters = self._sequences[min(self._call, len(self._sequences) - 1)]
        self._call += 1
        rows: list[dict] = []
        for letter in letters[:n_reviews]:
            item = dict(REVIEW_FIXTURES[letter])
            item["app_name"] = app_name
            item["app_id"] = package_id
            rows.append(item)
        return rows


def _prepare_db(db_path: Path) -> int:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        now = utc_now()
        source_id = ensure_data_source(conn, now)
        app_id = ensure_app(conn, source_id, PACKAGE_ID, APP_NAME, now)
        conn.commit()
        return app_id
    finally:
        conn.close()


def _source_ids(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT source_review_id FROM reviews_raw ORDER BY source_review_id"
    ).fetchall()
    return {row[0] for row in rows}


def _observed_source_ids(conn: sqlite3.Connection, run_id: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT r.source_review_id
        FROM review_observations o
        JOIN reviews_raw r ON r.review_raw_id = o.review_raw_id
        WHERE o.run_id = ?
        ORDER BY r.source_review_id
        """,
        (run_id,),
    ).fetchall()
    return {row[0] for row in rows}


def test_partial_overlap_collection(tmp_path: Path) -> None:
    db_path = tmp_path / "partial_overlap.db"
    app_id = _prepare_db(db_path)
    collector = _PartialOverlapCollector()
    apps = [{"package_id": PACKAGE_ID}]

    first = ingest_live_apps(
        db_path=db_path,
        apps=apps,
        n_reviews=10,
        collect_fn=collector,
    )

    conn = sqlite3.connect(db_path)
    try:
        assert first["status"] == "completed"
        assert first["total_fetched"] == 3
        assert first["total_inserted"] == 3
        assert first["skipped_duplicates"] == 0
        assert _source_ids(conn) == {"review-A", "review-B", "review-C"}

        app1 = conn.execute(
            """
            SELECT status, fetched_count, inserted_count, skipped_count
            FROM ingestion_run_apps
            WHERE run_id = ? AND app_id = ?
            """,
            (first["run_id"], app_id),
        ).fetchone()
        assert app1 == ("completed", 3, 3, 0)
        assert _observed_source_ids(conn, first["run_id"]) == {
            "review-A",
            "review-B",
            "review-C",
        }
    finally:
        conn.close()

    second = ingest_live_apps(
        db_path=db_path,
        apps=apps,
        n_reviews=10,
        collect_fn=collector,
    )

    conn = sqlite3.connect(db_path)
    try:
        assert second["status"] == "completed"
        assert second["run_id"] != first["run_id"]
        assert second["total_fetched"] == 4
        assert second["total_inserted"] == 2  # D, E
        assert second["skipped_duplicates"] == 2  # B, C

        assert _source_ids(conn) == {
            "review-A",
            "review-B",
            "review-C",
            "review-D",
            "review-E",
        }

        # A retained; B/C not duplicated in raw.
        raw_count = conn.execute("SELECT COUNT(*) FROM reviews_raw").fetchone()[0]
        assert raw_count == 5
        b_c_counts = conn.execute(
            """
            SELECT source_review_id, COUNT(*)
            FROM reviews_raw
            WHERE source_review_id IN ('review-B', 'review-C')
            GROUP BY source_review_id
            """
        ).fetchall()
        assert dict(b_c_counts) == {"review-B": 1, "review-C": 1}
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM reviews_raw WHERE source_review_id = 'review-A'"
            ).fetchone()[0]
            == 1
        )

        # Run 2 observes B, C, D, E (overlap + new).
        assert _observed_source_ids(conn, second["run_id"]) == {
            "review-B",
            "review-C",
            "review-D",
            "review-E",
        }
        # A was not in run 2 payload → no observation for A on run 2.
        assert "review-A" not in _observed_source_ids(conn, second["run_id"])

        app2 = conn.execute(
            """
            SELECT status, fetched_count, inserted_count, skipped_count
            FROM ingestion_run_apps
            WHERE run_id = ? AND app_id = ?
            """,
            (second["run_id"], app_id),
        ).fetchone()
        assert app2 == ("completed", 4, 2, 2)

        # D and E correctly linked to app, source, and first-seen ingestion run.
        de_rows = conn.execute(
            """
            SELECT source_review_id, app_id, ingestion_run_id, content
            FROM reviews_raw
            WHERE source_review_id IN ('review-D', 'review-E')
            ORDER BY source_review_id
            """
        ).fetchall()
        assert len(de_rows) == 2
        for source_review_id, row_app_id, ingestion_run_id, content in de_rows:
            assert row_app_id == app_id
            assert ingestion_run_id == second["run_id"]
            assert content == REVIEW_FIXTURES[source_review_id[-1]]["content"]

        # B/C keep original first-seen run (run 1).
        bc_first_seen = conn.execute(
            """
            SELECT source_review_id, ingestion_run_id
            FROM reviews_raw
            WHERE source_review_id IN ('review-B', 'review-C')
            ORDER BY source_review_id
            """
        ).fetchall()
        assert dict(bc_first_seen) == {
            "review-B": first["run_id"],
            "review-C": first["run_id"],
        }

        # No within-run observation duplicates.
        junction_dupes = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT run_id, review_raw_id
                FROM review_observations
                GROUP BY run_id, review_raw_id
                HAVING COUNT(*) > 1
            )
            """
        ).fetchone()[0]
        assert junction_dupes == 0

        processed_count = conn.execute(
            "SELECT COUNT(*) FROM reviews_processed"
        ).fetchone()[0]
        assert processed_count == 5

        print("\nPartial-overlap collection — run comparison")
        print(
            f"  run1: id={first['run_id']} status={first['status']} "
            f"fetched={first['total_fetched']} inserted={first['total_inserted']} "
            f"skipped={first['skipped_duplicates']} "
            f"raw_ids={sorted(_observed_source_ids(conn, first['run_id']))}"
        )
        print(
            f"  run2: id={second['run_id']} status={second['status']} "
            f"fetched={second['total_fetched']} inserted={second['total_inserted']} "
            f"skipped={second['skipped_duplicates']} "
            f"obs_ids={sorted(_observed_source_ids(conn, second['run_id']))}"
        )
        print(f"  reviews_raw after run1:      {sorted({'review-A', 'review-B', 'review-C'})}")
        print(f"  reviews_raw after run2:      {sorted(_source_ids(conn))}")
        print(f"  per-app run1:                {app1}")
        print(f"  per-app run2:                {app2}")
        print(f"  reviews_processed:           {processed_count}")
        print(f"  D/E ingestion_run_id:        {second['run_id']}")
        print(f"  B/C ingestion_run_id:        {first['run_id']}")
    finally:
        conn.close()
