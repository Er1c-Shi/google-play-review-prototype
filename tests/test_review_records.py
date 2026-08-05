"""Tests for the shared ReviewRecord adapters (collector ↔ loader)."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DB_DIR = SRC / "db"
for path in (str(SRC), str(DB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from load_sample import load_review_records  # noqa: E402
from review_records import (  # noqa: E402
    review_record_from_csv_row,
    review_record_from_live_review,
    review_records_from_live_reviews,
    read_review_records_from_csv,
)


def test_live_review_adapter_normalizes_datetimes_and_ints() -> None:
    live = {
        "reviewId": "live-1",
        "userName": "Ada",
        "userImage": "https://example.com/a.png",
        "content": "Great app",
        "score": 5,
        "thumbsUpCount": 2,
        "reviewCreatedVersion": "1.2.3",
        "at": datetime(2026, 1, 2, 3, 4, 5),
        "replyContent": "Thanks!",
        "repliedAt": datetime(2026, 1, 3, 4, 5, 6),
        "appVersion": "1.2.3",
        "app_name": "Example",
        "app_id": "com.example.app",
    }

    record = review_record_from_live_review(live)

    assert record["source_review_id"] == "live-1"
    assert record["package_id"] == "com.example.app"
    assert record["app_name"] == "Example"
    assert record["score"] == 5
    assert record["thumbs_up_count"] == 2
    assert record["review_created_at"] == "2026-01-02 03:04:05"
    assert record["replied_at"] == "2026-01-03 04:05:06"
    assert record["reply_content"] == "Thanks!"
    assert record["raw_payload"]["at"] == "2026-01-02 03:04:05"
    assert isinstance(record["raw_payload"]["score"], int)


def test_csv_and_live_adapters_agree_on_core_fields() -> None:
    csv_row = {
        "reviewId": "same-id",
        "userName": "Ada",
        "userImage": "",
        "content": "Nice",
        "score": "4",
        "thumbsUpCount": "1",
        "reviewCreatedVersion": "9.0",
        "at": "2026-06-30 16:17:40",
        "replyContent": "",
        "repliedAt": "",
        "appVersion": "9.0",
        "app_name": "Spotify",
        "app_id": "com.spotify.music",
    }
    live = {
        "reviewId": "same-id",
        "userName": "Ada",
        "userImage": None,
        "content": "Nice",
        "score": 4,
        "thumbsUpCount": 1,
        "reviewCreatedVersion": "9.0",
        "at": datetime(2026, 6, 30, 16, 17, 40),
        "replyContent": None,
        "repliedAt": None,
        "appVersion": "9.0",
        "app_name": "Spotify",
        "app_id": "com.spotify.music",
    }

    from_csv = review_record_from_csv_row(csv_row)
    from_live = review_record_from_live_review(live)

    for key in (
        "source_review_id",
        "package_id",
        "app_name",
        "content",
        "score",
        "thumbs_up_count",
        "review_created_at",
        "app_version",
        "reply_content",
        "replied_at",
    ):
        assert from_csv[key] == from_live[key]


def test_load_review_records_accepts_live_adapted_rows(tmp_path: Path) -> None:
    live_rows = [
        {
            "reviewId": "pipe-1",
            "userName": "A",
            "userImage": "",
            "content": "ok",
            "score": 5,
            "thumbsUpCount": 0,
            "reviewCreatedVersion": "1.0",
            "at": datetime(2026, 1, 1, 12, 0, 0),
            "replyContent": None,
            "repliedAt": None,
            "appVersion": "1.0",
            "app_name": "Example",
            "app_id": "com.example.app",
        }
    ]
    records = review_records_from_live_reviews(live_rows)
    summary = load_review_records(records, db_path=tmp_path / "direct.db")

    assert summary["total_inserted"] == 1
    assert summary["skipped_duplicates"] == 0
    assert "sample_path" not in summary


def test_csv_workflow_still_loads_via_shared_records(tmp_path: Path) -> None:
    sample = ROOT / "data" / "samples" / "google_play_reviews_integration_sample.csv"
    records = read_review_records_from_csv(sample)
    assert len(records) == 400
    assert records[0]["source_review_id"]
    assert records[0]["package_id"]

    from load_sample import load_sample

    summary = load_sample(db_path=tmp_path / "csv.db", sample_path=sample)
    assert summary["total_inserted"] == 400
    assert summary["sample_path"].endswith("google_play_reviews_integration_sample.csv")
