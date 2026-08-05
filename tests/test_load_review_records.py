"""Unit tests for in-memory review record loading and validation."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DB_DIR = SRC / "db"
for path in (str(SRC), str(DB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from load_sample import load_review_records, load_sample  # noqa: E402
from review_records import (  # noqa: E402
    ReviewRecordValidationError,
    adapt_csv_rows_to_records,
    partition_review_records,
    validate_review_record,
)


def _valid_record(
    review_id: str = "ok-1",
    *,
    package_id: str = "com.example.app",
    app_name: str = "Example",
    score: int | None = 5,
) -> dict:
    return {
        "source_review_id": review_id,
        "package_id": package_id,
        "app_name": app_name,
        "content": "Looks good",
        "score": score,
        "thumbs_up_count": 0,
        "review_created_at": "2026-01-01 12:00:00",
        "app_version": "1.0.0",
        "reply_content": None,
        "replied_at": None,
        "raw_payload": {"reviewId": review_id, "app_id": package_id},
    }


def test_validate_review_record_rejects_missing_identity() -> None:
    bad = _valid_record()
    del bad["source_review_id"]
    with pytest.raises(ReviewRecordValidationError, match="missing required"):
        validate_review_record(bad)


def test_validate_review_record_rejects_wrong_score_type() -> None:
    bad = _valid_record()
    bad["score"] = "5"  # must already be int on ReviewRecord
    with pytest.raises(ReviewRecordValidationError, match="score must be int"):
        validate_review_record(bad)


def test_partition_review_records_keeps_valid_and_reports_invalid() -> None:
    valid, errors = partition_review_records(
        [
            _valid_record("a"),
            {"package_id": "com.x", "app_name": "X"},  # missing many fields
            _valid_record("b"),
            "not-a-mapping",
        ]
    )
    assert [row["source_review_id"] for row in valid] == ["a", "b"]
    assert len(errors) == 2
    assert errors[0]["stage"] == "validation"
    assert errors[1]["stage"] == "validation"


def test_load_review_records_rejects_non_list(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="records must be a list"):
        load_review_records({"nope": True}, db_path=tmp_path / "x.db")  # type: ignore[arg-type]


def test_load_review_records_skips_invalid_and_loads_valid(tmp_path: Path) -> None:
    summary = load_review_records(
        [
            _valid_record("good-1"),
            {"source_review_id": "", "package_id": "com.x", "app_name": "X"},
            _valid_record("good-2"),
        ],
        db_path=tmp_path / "partial.db",
    )

    assert summary["total_input"] == 3
    assert summary["total_fetched"] == 2
    assert summary["total_inserted"] == 2
    assert summary["invalid_records"] == 1
    assert summary["status"] == "completed_with_errors"
    assert summary["record_errors"][0]["stage"] == "validation"

    conn = sqlite3.connect(tmp_path / "partial.db")
    try:
        ids = {
            row[0]
            for row in conn.execute(
                "SELECT source_review_id FROM reviews_raw ORDER BY source_review_id"
            )
        }
        assert ids == {"good-1", "good-2"}
    finally:
        conn.close()


def test_load_review_records_all_invalid_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="No valid review records"):
        load_review_records(
            [{"source_review_id": None, "package_id": None, "app_name": None}],
            db_path=tmp_path / "empty.db",
        )


def test_adapt_csv_rows_reports_bad_row_without_dropping_good_ones() -> None:
    rows = [
        {
            "reviewId": "csv-1",
            "userName": "A",
            "userImage": "",
            "content": "ok",
            "score": "5",
            "thumbsUpCount": "0",
            "reviewCreatedVersion": "1",
            "at": "2026-01-01 00:00:00",
            "replyContent": "",
            "repliedAt": "",
            "appVersion": "1",
            "app_name": "Example",
            "app_id": "com.example.app",
        },
        {
            # missing reviewId / app fields → adaptation error
            "content": "broken",
            "score": "1",
        },
    ]
    records, errors = adapt_csv_rows_to_records(rows)
    assert len(records) == 1
    assert records[0]["source_review_id"] == "csv-1"
    assert len(errors) == 1
    assert errors[0]["stage"] == "csv_adapt"


def test_csv_integration_sample_no_regression(tmp_path: Path) -> None:
    sample = ROOT / "data" / "samples" / "google_play_reviews_integration_sample.csv"
    first = load_sample(db_path=tmp_path / "integration.db", sample_path=sample)
    second = load_sample(db_path=tmp_path / "integration.db", sample_path=sample)

    assert first["total_fetched"] == 400
    assert first["total_inserted"] == 400
    assert first["skipped_duplicates"] == 0
    assert first["invalid_records"] == 0
    assert first["observations_created"] == 400
    assert first["status"] == "completed"

    assert second["total_fetched"] == 400
    assert second["total_inserted"] == 0
    assert second["skipped_duplicates"] == 400
    assert second["observations_created"] == 400
    assert second["invalid_records"] == 0
