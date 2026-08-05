"""Integration tests for multi-app live ingestion orchestration."""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
DB_DIR = SRC / "db"
for path in (str(SRC), str(DB_DIR)):
    if path not in sys.path:
        sys.path.insert(0, path)

from ingest_live_app import (  # noqa: E402
    compute_run_status,
    ingest_live_apps,
)
from init_db import init_db  # noqa: E402
from load_sample import ensure_app, ensure_data_source, utc_now  # noqa: E402


def _seed_apps(db_path: Path, packages: list[tuple[str, str]]) -> dict[str, int]:
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        now = utc_now()
        source_id = ensure_data_source(conn, now)
        mapping: dict[str, int] = {}
        for package_id, app_name in packages:
            mapping[package_id] = ensure_app(conn, source_id, package_id, app_name, now)
        conn.commit()
        return mapping
    finally:
        conn.close()


def _fake_collector_factory(fail_packages: set[str] | None = None):
    fail_packages = fail_packages or set()

    def _collect(app_name: str, package_id: str, n_reviews: int) -> list[dict]:
        if package_id in fail_packages:
            raise RuntimeError(f"collector failed for {package_id}")
        rows = []
        for i in range(n_reviews):
            rows.append(
                {
                    "reviewId": f"{package_id}-rev-{i}",
                    "userName": f"user-{i}",
                    "userImage": "",
                    "content": f"{app_name} review {i}",
                    "score": 5,
                    "thumbsUpCount": 0,
                    "reviewCreatedVersion": "1.0",
                    "at": datetime(2026, 2, 1, 10, 0, i % 60),
                    "replyContent": None,
                    "repliedAt": None,
                    "appVersion": "1.0",
                    "app_name": app_name,
                    "app_id": package_id,
                }
            )
        return rows

    return _collect


def test_compute_run_status_matrix() -> None:
    assert compute_run_status(["completed", "completed"]) == "completed"
    assert compute_run_status(["completed", "failed"]) == "partial"
    assert compute_run_status(["failed", "failed"]) == "failed"
    assert compute_run_status([]) == "failed"


def test_multi_app_all_success(tmp_path: Path) -> None:
    db_path = tmp_path / "multi_ok.db"
    _seed_apps(
        db_path,
        [
            ("com.example.one", "One"),
            ("com.example.two", "Two"),
        ],
    )

    summary = ingest_live_apps(
        db_path=db_path,
        apps=[
            {"package_id": "com.example.one"},
            {"package_id": "com.example.two"},
        ],
        n_reviews=3,
        collect_fn=_fake_collector_factory(),
    )

    assert summary["status"] == "completed"
    assert summary["apps_completed"] == 2
    assert summary["apps_failed"] == 0
    assert summary["total_fetched"] == 6
    assert summary["total_inserted"] == 6
    assert summary["completed_at"] is not None
    assert len(summary["app_results"]) == 2
    assert {r["status"] for r in summary["app_results"]} == {"completed"}

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM reviews_raw").fetchone()[0] == 6
        assert (
            conn.execute("SELECT COUNT(*) FROM review_observations").fetchone()[0] == 6
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM ingestion_run_apps").fetchone()[0] == 2
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ingestion_run_apps WHERE status = 'running'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT status FROM ingestion_runs WHERE run_id = ?",
                (summary["run_id"],),
            ).fetchone()[0]
            == "completed"
        )
    finally:
        conn.close()


def test_multi_app_partial_failure_keeps_successful_data(tmp_path: Path) -> None:
    """Scenario 1: A ok, B fail, C ok → run partial; A/C data kept; B failed with error."""
    db_path = tmp_path / "multi_partial.db"
    pkg_a, pkg_b, pkg_c = (
        "com.example.app.a",
        "com.example.app.b",
        "com.example.app.c",
    )
    _seed_apps(
        db_path,
        [
            (pkg_a, "App A"),
            (pkg_b, "App B"),
            (pkg_c, "App C"),
        ],
    )

    summary = ingest_live_apps(
        db_path=db_path,
        apps=[
            {"package_id": pkg_a},
            {"package_id": pkg_b},
            {"package_id": pkg_c},
        ],
        n_reviews=2,
        collect_fn=_fake_collector_factory(fail_packages={pkg_b}),
    )

    assert summary["status"] == "partial"
    assert summary["apps_completed"] == 2
    assert summary["apps_failed"] == 1
    assert summary["total_inserted"] == 4
    assert summary["completed_at"] is not None

    by_package = {r["package_id"]: r for r in summary["app_results"]}
    assert by_package[pkg_a]["status"] == "completed"
    assert by_package[pkg_a]["inserted_count"] == 2
    assert by_package[pkg_a]["fetched_count"] == 2
    assert by_package[pkg_c]["status"] == "completed"
    assert by_package[pkg_c]["inserted_count"] == 2
    assert by_package[pkg_c]["fetched_count"] == 2
    assert by_package[pkg_b]["status"] == "failed"
    assert by_package[pkg_b]["inserted_count"] == 0
    assert by_package[pkg_b]["fetched_count"] == 0
    err = by_package[pkg_b]["error_message"] or ""
    assert err
    assert "collector failed" in err
    assert pkg_b in err

    conn = sqlite3.connect(db_path)
    try:
        # A and C data committed; B failure did not roll them back.
        packages = {
            row[0]
            for row in conn.execute(
                """
                SELECT a.source_app_identifier
                FROM reviews_raw r
                JOIN apps a ON a.app_id = r.app_id
                """
            )
        }
        assert packages == {pkg_a, pkg_c}
        assert conn.execute("SELECT COUNT(*) FROM reviews_raw").fetchone()[0] == 4
        assert (
            conn.execute("SELECT COUNT(*) FROM review_observations").fetchone()[0] == 4
        )
        assert (
            conn.execute("SELECT COUNT(*) FROM reviews_processed").fetchone()[0] == 4
        )
        assert (
            conn.execute(
                """
                SELECT COUNT(*) FROM reviews_raw r
                JOIN apps a ON a.app_id = r.app_id
                WHERE a.source_app_identifier = ?
                """,
                (pkg_b,),
            ).fetchone()[0]
            == 0
        )

        run_status, completed_at, error_summary = conn.execute(
            """
            SELECT status, completed_at, error_summary
            FROM ingestion_runs WHERE run_id = ?
            """,
            (summary["run_id"],),
        ).fetchone()
        assert run_status == "partial"
        assert completed_at is not None
        assert error_summary

        app_rows = {
            row[0]: row
            for row in conn.execute(
                """
                SELECT a.source_app_identifier, r.status, r.fetched_count,
                       r.inserted_count, r.skipped_count, r.error_message,
                       r.completed_at
                FROM ingestion_run_apps r
                JOIN apps a ON a.app_id = r.app_id
                WHERE r.run_id = ?
                """,
                (summary["run_id"],),
            )
        }
        assert set(app_rows) == {pkg_a, pkg_b, pkg_c}
        assert app_rows[pkg_a][1] == "completed"
        assert app_rows[pkg_c][1] == "completed"
        assert app_rows[pkg_b][1] == "failed"
        assert app_rows[pkg_b][5]
        assert "collector failed" in app_rows[pkg_b][5]
        assert all(row[6] is not None for row in app_rows.values())
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ingestion_run_apps WHERE status = 'running'"
            ).fetchone()[0]
            == 0
        )

        print("\nPartial failure scenario (A ok, B fail, C ok)")
        print(f"  run_id={summary['run_id']} status={run_status}")
        print(f"  apps_completed={summary['apps_completed']} apps_failed={summary['apps_failed']}")
        print(f"  reviews_raw={conn.execute('SELECT COUNT(*) FROM reviews_raw').fetchone()[0]}")
        for pkg, row in sorted(app_rows.items()):
            print(
                f"  {pkg}: status={row[1]} fetched={row[2]} inserted={row[3]} "
                f"skipped={row[4]} error={row[5]!r}"
            )
    finally:
        conn.close()


def test_multi_app_all_failed(tmp_path: Path) -> None:
    """Scenario 2: all apps fail → run failed; run retained; no stuck running rows."""
    db_path = tmp_path / "multi_fail.db"
    packages = [
        ("com.example.fail.a", "Fail A"),
        ("com.example.fail.b", "Fail B"),
        ("com.example.fail.c", "Fail C"),
    ]
    _seed_apps(db_path, packages)
    package_ids = [p[0] for p in packages]

    summary = ingest_live_apps(
        db_path=db_path,
        apps=[{"package_id": pid} for pid in package_ids],
        n_reviews=2,
        collect_fn=_fake_collector_factory(fail_packages=set(package_ids)),
    )

    assert summary["status"] == "failed"
    assert summary["apps_completed"] == 0
    assert summary["apps_failed"] == 3
    assert summary["total_inserted"] == 0
    assert summary["total_fetched"] == 0
    assert summary["completed_at"] is not None
    assert len(summary["app_results"]) == 3
    assert {r["status"] for r in summary["app_results"]} == {"failed"}
    for result in summary["app_results"]:
        assert result["error_message"]
        assert "collector failed" in result["error_message"]

    conn = sqlite3.connect(db_path)
    try:
        # Ingestion run is retained with terminal failed status + completed_at.
        run_rows = conn.execute(
            """
            SELECT run_id, status, completed_at, error_summary,
                   total_fetched, total_inserted
            FROM ingestion_runs
            """
        ).fetchall()
        assert len(run_rows) == 1
        run_id, run_status, completed_at, error_summary, fetched, inserted = run_rows[0]
        assert run_id == summary["run_id"]
        assert run_status == "failed"
        assert completed_at is not None
        assert completed_at == summary["completed_at"]
        assert error_summary
        assert fetched == 0
        assert inserted == 0

        app_rows = conn.execute(
            """
            SELECT a.source_app_identifier, r.status, r.error_message,
                   r.completed_at, r.fetched_count, r.inserted_count
            FROM ingestion_run_apps r
            JOIN apps a ON a.app_id = r.app_id
            WHERE r.run_id = ?
            ORDER BY a.source_app_identifier
            """,
            (run_id,),
        ).fetchall()
        assert len(app_rows) == 3
        assert {row[0] for row in app_rows} == set(package_ids)
        assert all(row[1] == "failed" for row in app_rows)
        assert all(row[2] and "collector failed" in row[2] for row in app_rows)
        assert all(row[3] is not None for row in app_rows)
        assert all(row[4] == 0 and row[5] == 0 for row in app_rows)

        # Pipeline must not leave any running status behind.
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ingestion_runs WHERE status = 'running'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM ingestion_run_apps WHERE status = 'running'"
            ).fetchone()[0]
            == 0
        )
        assert conn.execute("SELECT COUNT(*) FROM reviews_raw").fetchone()[0] == 0
        assert (
            conn.execute("SELECT COUNT(*) FROM review_observations").fetchone()[0] == 0
        )

        print("\nAll-failed scenario")
        print(f"  run_id={run_id} status={run_status} completed_at={completed_at}")
        print(f"  apps_failed={summary['apps_failed']} reviews_raw=0")
        for row in app_rows:
            print(f"  {row[0]}: status={row[1]} error={row[2]!r}")
    finally:
        conn.close()


def test_multi_app_one_run_for_all_apps(tmp_path: Path) -> None:
    db_path = tmp_path / "multi_one_run.db"
    _seed_apps(
        db_path,
        [("com.example.x", "X"), ("com.example.y", "Y")],
    )
    summary = ingest_live_apps(
        db_path=db_path,
        apps=[{"package_id": "com.example.x"}, {"package_id": "com.example.y"}],
        n_reviews=1,
        collect_fn=_fake_collector_factory(),
    )
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM ingestion_runs").fetchone()[0] == 1
        run_ids = {
            row[0]
            for row in conn.execute("SELECT DISTINCT run_id FROM ingestion_run_apps")
        }
        assert run_ids == {summary["run_id"]}
    finally:
        conn.close()
