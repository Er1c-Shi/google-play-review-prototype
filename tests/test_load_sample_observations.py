"""Loader tests: review_observations written during CSV sample ingestion."""

from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DB_DIR = ROOT / "src" / "db"
if str(DB_DIR) not in sys.path:
    sys.path.insert(0, str(DB_DIR))

from load_sample import load_sample  # noqa: E402

CSV_HEADER = [
    "reviewId",
    "userName",
    "userImage",
    "content",
    "score",
    "thumbsUpCount",
    "reviewCreatedVersion",
    "at",
    "replyContent",
    "repliedAt",
    "appVersion",
    "app_name",
    "app_id",
]


def _write_sample(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def _review_row(
    review_id: str,
    *,
    content: str = "Nice app",
    package_id: str = "com.example.app",
    app_name: str = "Example",
) -> dict[str, str]:
    return {
        "reviewId": review_id,
        "userName": "tester",
        "userImage": "",
        "content": content,
        "score": "5",
        "thumbsUpCount": "0",
        "reviewCreatedVersion": "1.0.0",
        "at": "2026-01-01 12:00:00",
        "replyContent": "",
        "repliedAt": "",
        "appVersion": "1.0.0",
        "app_name": app_name,
        "app_id": package_id,
    }


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def test_new_review_inserts_raw_and_observation(tmp_path: Path) -> None:
    sample = tmp_path / "sample.csv"
    db_path = tmp_path / "loader.db"
    _write_sample(sample, [_review_row("rev-1")])

    summary = load_sample(db_path=db_path, sample_path=sample)

    assert summary["total_inserted"] == 1
    assert summary["skipped_duplicates"] == 0
    assert summary["observations_created"] == 1

    conn = sqlite3.connect(db_path)
    try:
        assert _count(conn, "SELECT COUNT(*) FROM reviews_raw") == 1
        assert _count(conn, "SELECT COUNT(*) FROM review_observations") == 1
        assert (
            _count(
                conn,
                """
                SELECT COUNT(*) FROM review_observations
                WHERE run_id = ? AND review_raw_id = 1
                """,
                (summary["run_id"],),
            )
            == 1
        )
    finally:
        conn.close()


def test_missing_developer_reply_is_not_flagged(tmp_path: Path) -> None:
    """Absent reply fields are stored, but not treated as a quality issue."""
    sample = tmp_path / "sample.csv"
    db_path = tmp_path / "loader.db"
    row = _review_row("rev-no-reply")
    row["replyContent"] = ""
    row["repliedAt"] = ""
    _write_sample(sample, [row])

    summary = load_sample(db_path=db_path, sample_path=sample)

    assert "missing_developer_reply" not in summary["flag_totals"]
    assert summary["flags_created_by_type"].get("missing_developer_reply", 0) == 0

    conn = sqlite3.connect(db_path)
    try:
        reply_content, replied_at = conn.execute(
            "SELECT reply_content, replied_at FROM reviews_raw WHERE source_review_id = ?",
            ("rev-no-reply",),
        ).fetchone()
        assert reply_content is None
        assert replied_at is None
        assert (
            _count(
                conn,
                """
                SELECT COUNT(*) FROM review_quality_flags
                WHERE flag_type = 'missing_developer_reply'
                """,
            )
            == 0
        )
        has_reply = conn.execute(
            """
            SELECT p.has_developer_reply
            FROM reviews_processed p
            JOIN reviews_raw r ON r.review_raw_id = p.review_raw_id
            WHERE r.source_review_id = ?
            """,
            ("rev-no-reply",),
        ).fetchone()[0]
        assert has_reply == 0
    finally:
        conn.close()


def test_has_developer_reply_true_when_reply_present(tmp_path: Path) -> None:
    sample = tmp_path / "sample.csv"
    db_path = tmp_path / "loader.db"
    row = _review_row("rev-with-reply")
    row["replyContent"] = "Thanks for the feedback!"
    row["repliedAt"] = "2026-01-02 09:00:00"
    _write_sample(sample, [row])

    load_sample(db_path=db_path, sample_path=sample)

    conn = sqlite3.connect(db_path)
    try:
        has_reply, reply_content = conn.execute(
            """
            SELECT p.has_developer_reply, r.reply_content
            FROM reviews_processed p
            JOIN reviews_raw r ON r.review_raw_id = p.review_raw_id
            WHERE r.source_review_id = ?
            """,
            ("rev-with-reply",),
        ).fetchone()
        assert reply_content == "Thanks for the feedback!"
        assert has_reply == 1
        assert (
            _count(
                conn,
                """
                SELECT COUNT(*) FROM review_quality_flags
                WHERE flag_type = 'missing_developer_reply'
                """,
            )
            == 0
        )
    finally:
        conn.close()


def test_has_developer_reply_false_for_whitespace_only_reply(tmp_path: Path) -> None:
    sample = tmp_path / "sample.csv"
    db_path = tmp_path / "loader.db"
    row = _review_row("rev-whitespace-reply")
    row["replyContent"] = "   \t  "
    _write_sample(sample, [row])

    load_sample(db_path=db_path, sample_path=sample)

    conn = sqlite3.connect(db_path)
    try:
        # empty_to_none strips blanks to NULL on ingest; availability stays false.
        has_reply = conn.execute(
            """
            SELECT p.has_developer_reply
            FROM reviews_processed p
            JOIN reviews_raw r ON r.review_raw_id = p.review_raw_id
            WHERE r.source_review_id = ?
            """,
            ("rev-whitespace-reply",),
        ).fetchone()[0]
        assert has_reply == 0
    finally:
        conn.close()


def test_compute_has_developer_reply_rule() -> None:
    from load_sample import compute_has_developer_reply

    assert compute_has_developer_reply(None) is False
    assert compute_has_developer_reply("") is False
    assert compute_has_developer_reply("   ") is False
    assert compute_has_developer_reply("Thanks!") is True


def test_existing_review_skips_raw_but_records_observation(tmp_path: Path) -> None:
    sample = tmp_path / "sample.csv"
    db_path = tmp_path / "loader.db"
    _write_sample(sample, [_review_row("rev-1"), _review_row("rev-2")])

    first = load_sample(db_path=db_path, sample_path=sample)
    second = load_sample(db_path=db_path, sample_path=sample)

    assert first["total_inserted"] == 2
    assert first["skipped_duplicates"] == 0
    assert first["observations_created"] == 2

    assert second["total_inserted"] == 0
    assert second["skipped_duplicates"] == 2
    assert second["observations_created"] == 2
    assert second["run_id"] != first["run_id"]

    conn = sqlite3.connect(db_path)
    try:
        assert _count(conn, "SELECT COUNT(*) FROM reviews_raw") == 2
        assert _count(conn, "SELECT COUNT(*) FROM review_observations") == 4
        assert (
            _count(
                conn,
                "SELECT COUNT(*) FROM review_observations WHERE run_id = ?",
                (first["run_id"],),
            )
            == 2
        )
        assert (
            _count(
                conn,
                "SELECT COUNT(*) FROM review_observations WHERE run_id = ?",
                (second["run_id"],),
            )
            == 2
        )
        # Same review observed in both runs
        assert (
            _count(
                conn,
                """
                SELECT COUNT(DISTINCT run_id)
                FROM review_observations
                WHERE review_raw_id = 1
                """,
            )
            == 2
        )
    finally:
        conn.close()


def test_duplicate_row_in_same_run_does_not_duplicate_observation(
    tmp_path: Path,
) -> None:
    sample = tmp_path / "sample.csv"
    db_path = tmp_path / "loader.db"
    _write_sample(
        sample,
        [
            _review_row("rev-dup", content="first sighting"),
            _review_row("rev-dup", content="same review again"),
        ],
    )

    summary = load_sample(db_path=db_path, sample_path=sample)

    assert summary["total_fetched"] == 2
    assert summary["total_inserted"] == 1
    assert summary["skipped_duplicates"] == 1
    assert summary["observations_created"] == 1

    conn = sqlite3.connect(db_path)
    try:
        assert _count(conn, "SELECT COUNT(*) FROM reviews_raw") == 1
        assert _count(conn, "SELECT COUNT(*) FROM review_observations") == 1
    finally:
        conn.close()


def test_integration_sample_two_loads_preserve_counts(tmp_path: Path) -> None:
    """
    Documented database integration flow: load the fixed sample twice.

    Verifies raw deduplication stays stable while review_observations grow
    per ingestion run.
    """
    sample = ROOT / "data" / "samples" / "google_play_reviews_integration_sample.csv"
    if not sample.is_file():
        pytest.skip("integration sample CSV not present")

    db_path = tmp_path / "integration.db"
    expected_sample_size = 400

    # --- First load ---
    first = load_sample(db_path=db_path, sample_path=sample)
    conn = sqlite3.connect(db_path)
    try:
        raw_after_first = _count(conn, "SELECT COUNT(*) FROM reviews_raw")
        observations_after_first = _count(
            conn, "SELECT COUNT(*) FROM review_observations"
        )
        observations_run_1 = _count(
            conn,
            "SELECT COUNT(*) FROM review_observations WHERE run_id = ?",
            (first["run_id"],),
        )
        distinct_raw_keys_after_first = _count(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT app_id, source_review_id
                FROM reviews_raw
                GROUP BY app_id, source_review_id
            )
            """,
        )
    finally:
        conn.close()

    assert first["total_fetched"] == expected_sample_size
    assert first["total_inserted"] == expected_sample_size
    assert first["skipped_duplicates"] == 0
    assert first["observations_created"] == expected_sample_size
    assert first["observations_for_run"] == expected_sample_size
    assert raw_after_first == expected_sample_size
    assert observations_after_first == expected_sample_size
    assert observations_run_1 == expected_sample_size
    assert distinct_raw_keys_after_first == expected_sample_size

    # --- Second load (new ingestion run, same sample) ---
    second = load_sample(db_path=db_path, sample_path=sample)
    assert second["run_id"] != first["run_id"]

    conn = sqlite3.connect(db_path)
    try:
        raw_after_second = _count(conn, "SELECT COUNT(*) FROM reviews_raw")
        observations_after_second = _count(
            conn, "SELECT COUNT(*) FROM review_observations"
        )
        duplicate_raw_groups = _count(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT app_id, source_review_id
                FROM reviews_raw
                GROUP BY app_id, source_review_id
                HAVING COUNT(*) > 1
            )
            """,
        )
        observations_run_2 = _count(
            conn,
            "SELECT COUNT(*) FROM review_observations WHERE run_id = ?",
            (second["run_id"],),
        )
        reviews_seen_by_run_1 = _count(
            conn,
            """
            SELECT COUNT(DISTINCT o.review_raw_id)
            FROM review_observations o
            JOIN reviews_raw r ON r.review_raw_id = o.review_raw_id
            WHERE o.run_id = ?
            """,
            (first["run_id"],),
        )
        reviews_seen_by_run_2 = _count(
            conn,
            """
            SELECT COUNT(DISTINCT o.review_raw_id)
            FROM review_observations o
            JOIN reviews_raw r ON r.review_raw_id = o.review_raw_id
            WHERE o.run_id = ?
            """,
            (second["run_id"],),
        )
        reviews_seen_by_both_runs = _count(
            conn,
            """
            SELECT COUNT(*) FROM (
                SELECT review_raw_id
                FROM review_observations
                WHERE run_id IN (?, ?)
                GROUP BY review_raw_id
                HAVING COUNT(DISTINCT run_id) = 2
            )
            """,
            (first["run_id"], second["run_id"]),
        )
    finally:
        conn.close()

    assert second["total_fetched"] == expected_sample_size
    assert second["total_inserted"] == 0
    assert second["skipped_duplicates"] == expected_sample_size
    assert second["observations_created"] == expected_sample_size
    assert second["observations_for_run"] == expected_sample_size

    # reviews_raw unchanged; no duplicate raw rows
    assert raw_after_second == raw_after_first == expected_sample_size
    assert duplicate_raw_groups == 0

    # second run still recorded observations; both runs can query their reviews
    assert observations_after_second == observations_after_first + expected_sample_size
    assert observations_run_2 == expected_sample_size
    assert reviews_seen_by_run_1 == expected_sample_size
    assert reviews_seen_by_run_2 == expected_sample_size
    assert reviews_seen_by_both_runs == expected_sample_size

    # Surface key counts in pytest output (-s) / CI logs.
    print("\nIntegration reload key counts")
    print(
        f"  first  load: run_id={first['run_id']} "
        f"inserted={first['total_inserted']} skipped={first['skipped_duplicates']} "
        f"raw={raw_after_first} observations={observations_after_first} "
        f"obs_for_run={observations_run_1}"
    )
    print(
        f"  second load: run_id={second['run_id']} "
        f"inserted={second['total_inserted']} skipped={second['skipped_duplicates']} "
        f"raw={raw_after_second} observations={observations_after_second} "
        f"obs_for_run={observations_run_2}"
    )