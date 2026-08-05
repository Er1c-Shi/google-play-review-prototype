"""Database tests for the review_observations junction table."""

from __future__ import annotations

import sqlite3

import pytest

NOW = "2026-01-01T00:00:00+00:00"


def _insert_observation(
    conn: sqlite3.Connection,
    run_id: int,
    review_raw_id: int,
    observed_at: str = NOW,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO review_observations (run_id, review_raw_id, observed_at)
        VALUES (?, ?, ?)
        """,
        (run_id, review_raw_id, observed_at),
    )
    return int(cursor.lastrowid)


def test_same_review_can_be_observed_in_different_runs(
    conn: sqlite3.Connection, seeded: dict[str, int]
) -> None:
    review_raw_id = seeded["review_raw_id"]
    obs_1 = _insert_observation(conn, seeded["run_id_1"], review_raw_id, "2026-01-01T01:00:00+00:00")
    obs_2 = _insert_observation(conn, seeded["run_id_2"], review_raw_id, "2026-01-02T01:00:00+00:00")
    conn.commit()

    rows = conn.execute(
        """
        SELECT id, run_id, review_raw_id, observed_at
        FROM review_observations
        WHERE review_raw_id = ?
        ORDER BY run_id
        """,
        (review_raw_id,),
    ).fetchall()

    assert len(rows) == 2
    assert rows[0]["id"] == obs_1
    assert rows[0]["run_id"] == seeded["run_id_1"]
    assert rows[1]["id"] == obs_2
    assert rows[1]["run_id"] == seeded["run_id_2"]
    assert {row["review_raw_id"] for row in rows} == {review_raw_id}


def test_duplicate_observation_in_same_run_is_rejected(
    conn: sqlite3.Connection, seeded: dict[str, int]
) -> None:
    run_id = seeded["run_id_1"]
    review_raw_id = seeded["review_raw_id"]
    _insert_observation(conn, run_id, review_raw_id)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError) as exc_info:
        _insert_observation(conn, run_id, review_raw_id, "2026-01-01T02:00:00+00:00")

    assert "UNIQUE" in str(exc_info.value).upper() or "unique" in str(exc_info.value).lower()

    count = conn.execute(
        """
        SELECT COUNT(*) FROM review_observations
        WHERE run_id = ? AND review_raw_id = ?
        """,
        (run_id, review_raw_id),
    ).fetchone()[0]
    assert count == 1


def test_observation_joins_to_ingestion_run_and_reviews_raw(
    conn: sqlite3.Connection, seeded: dict[str, int]
) -> None:
    run_id = seeded["run_id_1"]
    review_raw_id = seeded["review_raw_id"]
    observation_id = _insert_observation(conn, run_id, review_raw_id)
    conn.commit()

    row = conn.execute(
        """
        SELECT
            o.id AS observation_id,
            o.observed_at,
            ir.run_id,
            ir.status AS run_status,
            r.review_raw_id,
            r.source_review_id,
            r.app_id
        FROM review_observations o
        JOIN ingestion_runs ir ON ir.run_id = o.run_id
        JOIN reviews_raw r ON r.review_raw_id = o.review_raw_id
        WHERE o.id = ?
        """,
        (observation_id,),
    ).fetchone()

    assert row is not None
    assert row["observation_id"] == observation_id
    assert row["run_id"] == run_id
    assert row["run_status"] == "completed"
    assert row["review_raw_id"] == review_raw_id
    assert row["source_review_id"] == "src-review-001"
    assert row["app_id"] == seeded["app_id"]


def test_insert_with_missing_run_is_rejected(
    conn: sqlite3.Connection, seeded: dict[str, int]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert_observation(conn, run_id=999, review_raw_id=seeded["review_raw_id"])


def test_insert_with_missing_review_is_rejected(
    conn: sqlite3.Connection, seeded: dict[str, int]
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        _insert_observation(conn, run_id=seeded["run_id_1"], review_raw_id=999)


def test_delete_ingestion_run_with_observation_is_rejected(
    conn: sqlite3.Connection, seeded: dict[str, int]
) -> None:
    """Schema uses default NO ACTION FKs; parent delete fails while children exist."""
    _insert_observation(conn, seeded["run_id_1"], seeded["review_raw_id"])
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "DELETE FROM ingestion_runs WHERE run_id = ?",
            (seeded["run_id_1"],),
        )

    still_there = conn.execute(
        "SELECT COUNT(*) FROM review_observations WHERE run_id = ?",
        (seeded["run_id_1"],),
    ).fetchone()[0]
    assert still_there == 1


def test_delete_review_raw_with_observation_is_rejected(
    conn: sqlite3.Connection, seeded: dict[str, int]
) -> None:
    """Schema uses default NO ACTION FKs; parent delete fails while children exist."""
    _insert_observation(conn, seeded["run_id_1"], seeded["review_raw_id"])
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "DELETE FROM reviews_raw WHERE review_raw_id = ?",
            (seeded["review_raw_id"],),
        )

    still_there = conn.execute(
        "SELECT COUNT(*) FROM review_observations WHERE review_raw_id = ?",
        (seeded["review_raw_id"],),
    ).fetchone()[0]
    assert still_there == 1
