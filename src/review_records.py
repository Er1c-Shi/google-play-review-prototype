"""
Shared review record interface between live collector and database loader.

Purpose
-------
Normalize Google Play review payloads (live scraper dicts or CSV rows) into a
single in-memory shape that the database loader can ingest without requiring a
CSV round-trip.

This module does not run collection or open the database. It only defines the
record contract and adapters.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, TypedDict


class ReviewRecord(TypedDict):
    """
    Unified review record for database ingestion.

    Field names are loader-oriented (not raw scraper names). Values are already
    lightly cleaned so `load_review_records` can insert without CSV-specific
    parsing. `raw_payload` keeps a JSON-serializable copy of the source row for
    `reviews_raw.raw_payload_json`.
    """

    source_review_id: str
    package_id: str
    app_name: str
    content: str | None
    score: int | None
    thumbs_up_count: int | None
    review_created_at: str | None
    app_version: str | None
    reply_content: str | None
    replied_at: str | None
    raw_payload: dict[str, Any]


def empty_to_none(value: Any) -> str | None:
    """Trim strings; map None/blank to None. Non-strings become str first."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        return value if value else None
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    text = str(value).strip()
    return text if text else None


def _as_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = empty_to_none(value)
    if text is None:
        return None
    return int(text)


def _jsonable_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.replace(microsecond=0).isoformat(sep=" ")
    return value


def jsonable_payload(source: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a source mapping into a JSON-serializable dict."""
    return {key: _jsonable_value(value) for key, value in source.items()}


REQUIRED_REVIEW_RECORD_FIELDS: tuple[str, ...] = (
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
    "raw_payload",
)


class ReviewRecordValidationError(ValueError):
    """Raised when a review record fails structural validation."""


def validate_review_record(record: Any) -> ReviewRecord:
    """
    Validate and return a ReviewRecord.

    Raises ReviewRecordValidationError when required identity fields are missing
    or types are incompatible with database insertion.
    """
    if not isinstance(record, Mapping):
        raise ReviewRecordValidationError(
            f"review record must be a mapping, got {type(record).__name__}"
        )

    missing = [field for field in REQUIRED_REVIEW_RECORD_FIELDS if field not in record]
    if missing:
        raise ReviewRecordValidationError(
            f"missing required field(s): {', '.join(missing)}"
        )

    source_review_id = empty_to_none(record.get("source_review_id"))
    package_id = empty_to_none(record.get("package_id"))
    app_name = empty_to_none(record.get("app_name"))
    if source_review_id is None:
        raise ReviewRecordValidationError("source_review_id is required")
    if package_id is None:
        raise ReviewRecordValidationError("package_id is required")
    if app_name is None:
        raise ReviewRecordValidationError("app_name is required")

    score = record.get("score")
    if score is not None and not isinstance(score, int):
        raise ReviewRecordValidationError(
            f"score must be int or None, got {type(score).__name__}"
        )
    thumbs = record.get("thumbs_up_count")
    if thumbs is not None and not isinstance(thumbs, int):
        raise ReviewRecordValidationError(
            f"thumbs_up_count must be int or None, got {type(thumbs).__name__}"
        )

    raw_payload = record.get("raw_payload")
    if not isinstance(raw_payload, dict):
        raise ReviewRecordValidationError("raw_payload must be a dict")

    for text_field in (
        "content",
        "review_created_at",
        "app_version",
        "reply_content",
        "replied_at",
    ):
        value = record.get(text_field)
        if value is not None and not isinstance(value, str):
            raise ReviewRecordValidationError(
                f"{text_field} must be str or None, got {type(value).__name__}"
            )

    return ReviewRecord(
        source_review_id=source_review_id,
        package_id=package_id,
        app_name=app_name,
        content=record.get("content"),
        score=score,
        thumbs_up_count=thumbs,
        review_created_at=record.get("review_created_at"),
        app_version=record.get("app_version"),
        reply_content=record.get("reply_content"),
        replied_at=record.get("replied_at"),
        raw_payload=raw_payload,
    )


def partition_review_records(
    records: Iterable[Any],
) -> tuple[list[ReviewRecord], list[dict[str, Any]]]:
    """
    Split input into validated ReviewRecords and per-item error descriptors.

    Invalid rows are skipped (not raised) so a batch can continue with valid data.
    """
    valid: list[ReviewRecord] = []
    errors: list[dict[str, Any]] = []
    for index, record in enumerate(records):
        try:
            valid.append(validate_review_record(record))
        except ReviewRecordValidationError as exc:
            identity = None
            if isinstance(record, Mapping):
                identity = record.get("source_review_id") or record.get("reviewId")
            errors.append(
                {
                    "index": index,
                    "stage": "validation",
                    "source_review_id": identity,
                    "error": str(exc),
                }
            )
    return valid, errors


def review_record_from_csv_row(row: Mapping[str, Any]) -> ReviewRecord:
    """
    Adapt a CSV DictReader row (all values typically str) to ReviewRecord.

    Reuses the same field mapping historically embedded in `load_sample`.
    """
    score = _as_optional_int(row.get("score"))
    thumbs = _as_optional_int(row.get("thumbsUpCount"))
    app_version = empty_to_none(row.get("appVersion")) or empty_to_none(
        row.get("reviewCreatedVersion")
    )
    return ReviewRecord(
        source_review_id=str(row["reviewId"]),
        package_id=str(row["app_id"]),
        app_name=str(row["app_name"]),
        content=empty_to_none(row.get("content")),
        score=score,
        thumbs_up_count=thumbs,
        review_created_at=empty_to_none(row.get("at")),
        app_version=app_version,
        reply_content=empty_to_none(row.get("replyContent")),
        replied_at=empty_to_none(row.get("repliedAt")),
        raw_payload=jsonable_payload(dict(row)),
    )


def review_record_from_live_review(
    review: Mapping[str, Any],
    *,
    app_name: str | None = None,
    package_id: str | None = None,
) -> ReviewRecord:
    """
    Adapt a google-play-scraper review dict (plus optional app annotations).

    Live collector currently mutates each review with `app_name` / `app_id`.
    Callers may also pass those explicitly.
    """
    resolved_app_name = app_name if app_name is not None else review.get("app_name")
    resolved_package_id = package_id if package_id is not None else review.get("app_id")
    if not resolved_app_name or not resolved_package_id:
        raise ValueError(
            "live review requires app_name and app_id (package id) on the dict "
            "or as explicit arguments"
        )

    app_version = empty_to_none(review.get("appVersion")) or empty_to_none(
        review.get("reviewCreatedVersion")
    )
    payload = jsonable_payload(dict(review))
    payload["app_name"] = str(resolved_app_name)
    payload["app_id"] = str(resolved_package_id)

    return ReviewRecord(
        source_review_id=str(review["reviewId"]),
        package_id=str(resolved_package_id),
        app_name=str(resolved_app_name),
        content=empty_to_none(review.get("content")),
        score=_as_optional_int(review.get("score")),
        thumbs_up_count=_as_optional_int(review.get("thumbsUpCount")),
        review_created_at=empty_to_none(review.get("at")),
        app_version=app_version,
        reply_content=empty_to_none(review.get("replyContent")),
        replied_at=empty_to_none(review.get("repliedAt")),
        raw_payload=payload,
    )


def review_records_from_live_reviews(
    reviews: Iterable[Mapping[str, Any]],
) -> list[ReviewRecord]:
    """Convert a list of live collector review dicts to ReviewRecord list."""
    return [review_record_from_live_review(review) for review in reviews]


def read_csv_rows(sample_path: Path | str) -> list[dict[str, Any]]:
    """Read CSV rows as dicts without adapting to ReviewRecord."""
    path = Path(sample_path)
    if not path.is_file():
        raise FileNotFoundError(f"Sample file not found: {path}")

    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    if not rows:
        raise ValueError(f"Sample file is empty: {path}")

    return rows


def adapt_csv_rows_to_records(
    rows: Iterable[Mapping[str, Any]],
) -> tuple[list[ReviewRecord], list[dict[str, Any]]]:
    """
    Adapt CSV dict rows to ReviewRecords, collecting per-row adaptation errors.
    """
    records: list[ReviewRecord] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        try:
            records.append(review_record_from_csv_row(row))
        except Exception as exc:  # noqa: BLE001 - surface row-level CSV issues
            identity = None
            if isinstance(row, Mapping):
                identity = row.get("reviewId")
            errors.append(
                {
                    "index": index,
                    "stage": "csv_adapt",
                    "source_review_id": identity,
                    "error": str(exc),
                }
            )
    return records, errors


def read_review_records_from_csv(sample_path: Path | str) -> list[ReviewRecord]:
    """Read CSV into ReviewRecords (returns successfully adapted rows only)."""
    rows = read_csv_rows(sample_path)
    records, errors = adapt_csv_rows_to_records(rows)
    if errors and not records:
        raise ValueError(
            f"Failed to adapt any CSV rows from {sample_path}: {errors[0]['error']}"
        )
    return records
