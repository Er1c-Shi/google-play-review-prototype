"""
Live Google Play ingestion → SQLite (single-app and multi-app orchestration).

Transaction boundaries
----------------------
1. Run creation is committed immediately so the run survives later failures.
2. Each app starts an `ingestion_run_apps` row and commits before collection,
   so a crash cannot leave the app permanently "running" without a row.
3. Collection/normalization happens outside write transactions.
4. Each app's review writes + app-result finalization commit in one per-app
   transaction. A later app failure never rolls back earlier apps' commits.
5. The multi-app pipeline sets the run's terminal status
   (`completed` / `partial` / `failed`) and `completed_at` in a final update
   after all apps finish.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Sequence

from init_db import DEFAULT_DB_PATH, init_db
from load_sample import (
    create_ingestion_run,
    create_processed_review,
    ensure_data_source,
    finish_ingestion_run_app,
    generate_quality_flags,
    ingest_review_for_run,
    start_ingestion_run_app,
    utc_now,
)

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from review_records import (  # noqa: E402
    partition_review_records,
    review_record_from_live_review,
)

CollectFn = Callable[[str, str, int], list[dict[str, Any]]]
AppRef = dict[str, Any]  # {"app_id": int?} and/or {"package_id": str?}


def _default_collect_fn(app_name: str, package_id: str, n_reviews: int) -> list[dict[str, Any]]:
    if str(_SRC_DIR) not in sys.path:
        sys.path.insert(0, str(_SRC_DIR))
    from collect_reviews import collect_reviews_for_app

    return collect_reviews_for_app(app_name, package_id, n_reviews)


def resolve_existing_app(
    conn: sqlite3.Connection,
    *,
    app_id: int | None = None,
    package_id: str | None = None,
) -> tuple[int, str, str]:
    """
    Resolve an existing apps row.

    Returns (app_id, package_id, app_name). Raises LookupError if not found.
    """
    if app_id is None and not package_id:
        raise ValueError("Provide app_id or package_id for an existing app")
    if app_id is not None and package_id:
        row = conn.execute(
            """
            SELECT app_id, source_app_identifier, app_name
            FROM apps
            WHERE app_id = ? AND source_app_identifier = ?
            """,
            (app_id, package_id),
        ).fetchone()
        if row is None:
            raise LookupError(
                f"No app with app_id={app_id} and package_id={package_id!r}"
            )
        return int(row[0]), str(row[1]), str(row[2])

    if app_id is not None:
        row = conn.execute(
            """
            SELECT app_id, source_app_identifier, app_name
            FROM apps WHERE app_id = ?
            """,
            (app_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"No app with app_id={app_id}")
        return int(row[0]), str(row[1]), str(row[2])

    row = conn.execute(
        """
        SELECT app_id, source_app_identifier, app_name
        FROM apps WHERE source_app_identifier = ?
        """,
        (package_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"No app with package_id={package_id!r}")
    return int(row[0]), str(row[1]), str(row[2])


def _require_run(conn: sqlite3.Connection, run_id: int) -> None:
    row = conn.execute(
        "SELECT run_id FROM ingestion_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"No ingestion_runs row with run_id={run_id}")


def _append_run_error_summary(
    conn: sqlite3.Connection,
    run_id: int,
    message: str,
) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET error_summary = CASE
            WHEN error_summary IS NULL OR TRIM(error_summary) = '' THEN ?
            ELSE error_summary || '; ' || ?
        END
        WHERE run_id = ?
        """,
        (message[:500], message[:500], run_id),
    )


def _add_run_totals(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    fetched_count: int,
    inserted_count: int,
    skipped_count: int,
) -> None:
    conn.execute(
        """
        UPDATE ingestion_runs
        SET
            total_fetched = COALESCE(total_fetched, 0) + ?,
            total_inserted = COALESCE(total_inserted, 0) + ?,
            skipped_duplicates = COALESCE(skipped_duplicates, 0) + ?
        WHERE run_id = ?
        """,
        (fetched_count, inserted_count, skipped_count, run_id),
    )


def finalize_ingestion_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    completed_at: str | None = None,
) -> None:
    """Set terminal run status and completion timestamp."""
    if status not in {"completed", "partial", "failed"}:
        raise ValueError(f"Unsupported run status: {status!r}")
    completed_at = completed_at or utc_now()
    conn.execute(
        """
        UPDATE ingestion_runs
        SET completed_at = ?, status = ?
        WHERE run_id = ?
        """,
        (completed_at, status, run_id),
    )


def compute_run_status(app_statuses: Sequence[str]) -> str:
    """Derive run status from per-app result statuses."""
    if not app_statuses:
        return "failed"
    failed = sum(1 for status in app_statuses if status == "failed")
    succeeded = sum(1 for status in app_statuses if status == "completed")
    if failed == 0 and succeeded == len(app_statuses):
        return "completed"
    if succeeded == 0:
        return "failed"
    return "partial"


def _read_app_result(
    conn: sqlite3.Connection, run_id: int, app_id: int
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT status, fetched_count, inserted_count, skipped_count,
               error_message, started_at, completed_at
        FROM ingestion_run_apps
        WHERE run_id = ? AND app_id = ?
        """,
        (run_id, app_id),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"Missing ingestion_run_apps row for run_id={run_id}, app_id={app_id}"
        )
    return {
        "status": row["status"],
        "fetched_count": int(row["fetched_count"]),
        "inserted_count": int(row["inserted_count"]),
        "skipped_count": int(row["skipped_count"]),
        "error_message": row["error_message"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def _mark_app_failed_only(
    conn: sqlite3.Connection,
    *,
    run_id: int,
    app_id: int,
    fetched_count: int,
    inserted_count: int,
    skipped_count: int,
    error_message: str,
    completed_at: str,
    update_run_totals: bool,
) -> None:
    """Finalize app as failed; optionally bump run totals; never sets run terminal status."""
    conn.execute("BEGIN")
    try:
        finish_ingestion_run_app(
            conn,
            run_id,
            app_id,
            fetched_count=fetched_count,
            inserted_count=inserted_count,
            skipped_count=skipped_count,
            error_message=error_message[:1000],
            completed_at=completed_at,
        )
        if update_run_totals:
            _add_run_totals(
                conn,
                run_id=run_id,
                fetched_count=fetched_count,
                inserted_count=inserted_count,
                skipped_count=skipped_count,
            )
        _append_run_error_summary(
            conn, run_id, f"app_id={app_id}: {error_message[:400]}"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def ingest_one_app_in_run(
    db_path: Path,
    *,
    run_id: int,
    app_id: int | None = None,
    package_id: str | None = None,
    n_reviews: int = 50,
    collect_fn: CollectFn | None = None,
    update_run_totals: bool = True,
    reraise: bool = True,
) -> dict[str, Any]:
    """
    Ingest one existing app into an existing ingestion run.

    Commits app-level writes independently. Does not set the run's terminal
    status (`completed`/`partial`/`failed`); callers own that.
    """
    if n_reviews < 1:
        raise ValueError("n_reviews must be >= 1")

    collect = collect_fn or _default_collect_fn
    db_path = Path(db_path)
    init_db(db_path)

    started_at = utc_now()
    fetched_count = 0
    inserted_count = 0
    skipped_count = 0
    newly_processed_ids: list[int] = []
    flags_created_by_type: dict[str, int] = {}
    ingest_error_messages: list[str] = []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        conn.execute("BEGIN")
        try:
            _require_run(conn, run_id)
            resolved_app_id, resolved_package_id, resolved_app_name = (
                resolve_existing_app(conn, app_id=app_id, package_id=package_id)
            )
            start_ingestion_run_app(conn, run_id, resolved_app_id, started_at)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        # Collect outside write TX
        try:
            live_rows = collect(resolved_app_name, resolved_package_id, n_reviews)
            adapted: list[dict[str, Any]] = []
            adapt_errors: list[str] = []
            for row in live_rows:
                try:
                    adapted.append(
                        review_record_from_live_review(
                            row,
                            app_name=resolved_app_name,
                            package_id=resolved_package_id,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    adapt_errors.append(str(exc))
            valid_records, validation_errors = partition_review_records(adapted)
            if not valid_records:
                detail = "; ".join(
                    adapt_errors[:3] + [e["error"] for e in validation_errors[:3]]
                ) or "collector returned no usable reviews"
                raise RuntimeError(f"No valid live reviews to ingest: {detail}")
        except Exception as exc:
            completed_at = utc_now()
            _mark_app_failed_only(
                conn,
                run_id=run_id,
                app_id=resolved_app_id,
                fetched_count=0,
                inserted_count=0,
                skipped_count=0,
                error_message=str(exc),
                completed_at=completed_at,
                update_run_totals=update_run_totals,
            )
            if reraise:
                raise
            result = _read_app_result(conn, run_id, resolved_app_id)
            return {
                "db_path": str(db_path.resolve()),
                "run_id": run_id,
                "app_id": resolved_app_id,
                "package_id": resolved_package_id,
                "app_name": resolved_app_name,
                **result,
                "processed_created": 0,
                "flags_created": 0,
                "ingest_error_count": 0,
            }

        collected_at = utc_now()
        newly_inserted_raw: list[tuple[int, str | None, int | None, str | None]] = []
        app_failed_after_write = False
        failure_message: str | None = None

        conn.execute("BEGIN")
        try:
            for record in valid_records:
                if record["package_id"] != resolved_package_id:
                    ingest_error_messages.append(
                        f"{record['source_review_id']}: package mismatch"
                    )
                    continue
                fetched_count += 1
                conn.execute("SAVEPOINT live_ingest_one")
                try:
                    raw_payload_json = json.dumps(
                        record["raw_payload"], ensure_ascii=False
                    )
                    review_raw_id, raw_inserted, _observation_created = (
                        ingest_review_for_run(
                            conn,
                            run_id=run_id,
                            app_id=resolved_app_id,
                            source_review_id=record["source_review_id"],
                            content=record["content"],
                            score=record["score"],
                            thumbs_up_count=record["thumbs_up_count"],
                            review_created_at=record["review_created_at"],
                            app_version=record["app_version"],
                            reply_content=record["reply_content"],
                            replied_at=record["replied_at"],
                            collected_at=collected_at,
                            raw_payload_json=raw_payload_json,
                            observed_at=collected_at,
                        )
                    )
                    conn.execute("RELEASE SAVEPOINT live_ingest_one")
                except Exception as exc:  # noqa: BLE001
                    conn.execute("ROLLBACK TO SAVEPOINT live_ingest_one")
                    ingest_error_messages.append(
                        f"{record['source_review_id']}: {exc}"
                    )
                    continue

                if raw_inserted:
                    inserted_count += 1
                    newly_inserted_raw.append(
                        (
                            review_raw_id,
                            record["content"],
                            record["score"],
                            record["reply_content"],
                        )
                    )
                else:
                    skipped_count += 1

            processed_at = utc_now()
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
                    newly_processed_ids.append(processed_id)

            flags_created_by_type = dict(
                generate_quality_flags(conn, newly_processed_ids, utc_now())
            )

            completed_at = utc_now()
            error_message = None
            if ingest_error_messages:
                if len(ingest_error_messages) == 1:
                    error_message = ingest_error_messages[0][:500]
                else:
                    error_message = (
                        f"{len(ingest_error_messages)} review errors; "
                        f"first: {ingest_error_messages[0][:400]}"
                    )

            finish_ingestion_run_app(
                conn,
                run_id,
                resolved_app_id,
                fetched_count=fetched_count,
                inserted_count=inserted_count,
                skipped_count=skipped_count,
                error_message=error_message,
                completed_at=completed_at,
            )
            if update_run_totals:
                _add_run_totals(
                    conn,
                    run_id=run_id,
                    fetched_count=fetched_count,
                    inserted_count=inserted_count,
                    skipped_count=skipped_count,
                )

            app_status = conn.execute(
                """
                SELECT status FROM ingestion_run_apps
                WHERE run_id = ? AND app_id = ?
                """,
                (run_id, resolved_app_id),
            ).fetchone()[0]
            if app_status == "failed":
                _append_run_error_summary(
                    conn,
                    run_id,
                    f"app_id={resolved_app_id}: {error_message or 'app ingestion failed'}",
                )
                app_failed_after_write = True
                failure_message = (
                    error_message or "Single-app live ingestion failed with no inserts"
                )

            conn.commit()
        except Exception as exc:
            conn.rollback()
            completed_at = utc_now()
            _mark_app_failed_only(
                conn,
                run_id=run_id,
                app_id=resolved_app_id,
                fetched_count=fetched_count,
                inserted_count=inserted_count,
                skipped_count=skipped_count,
                error_message=str(exc),
                completed_at=completed_at,
                update_run_totals=update_run_totals,
            )
            if reraise:
                raise
            result = _read_app_result(conn, run_id, resolved_app_id)
            return {
                "db_path": str(db_path.resolve()),
                "run_id": run_id,
                "app_id": resolved_app_id,
                "package_id": resolved_package_id,
                "app_name": resolved_app_name,
                **result,
                "processed_created": len(newly_processed_ids),
                "flags_created": sum(flags_created_by_type.values()),
                "ingest_error_count": len(ingest_error_messages),
            }

        if app_failed_after_write and reraise:
            raise RuntimeError(failure_message)

        result = _read_app_result(conn, run_id, resolved_app_id)
        return {
            "db_path": str(db_path.resolve()),
            "run_id": run_id,
            "app_id": resolved_app_id,
            "package_id": resolved_package_id,
            "app_name": resolved_app_name,
            **result,
            "processed_created": len(newly_processed_ids),
            "flags_created": sum(flags_created_by_type.values()),
            "ingest_error_count": len(ingest_error_messages),
        }
    finally:
        conn.close()


def ingest_live_app(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    app_id: int | None = None,
    package_id: str | None = None,
    run_id: int | None = None,
    n_reviews: int = 50,
    collect_fn: CollectFn | None = None,
) -> dict[str, Any]:
    """
    Run single-app live ingestion into SQLite.

    Creates a new ingestion run unless `run_id` is provided, then finalizes the
    run as `completed` or `failed`.
    """
    if n_reviews < 1:
        raise ValueError("n_reviews must be >= 1")

    db_path = Path(db_path)
    init_db(db_path)
    started_at = utc_now()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        try:
            source_id = ensure_data_source(conn, started_at)
            # Validate app exists before creating a run.
            resolve_existing_app(conn, app_id=app_id, package_id=package_id)
            if run_id is None:
                resolved_run_id = create_ingestion_run(
                    conn,
                    source_id=source_id,
                    started_at=started_at,
                    app_count=1,
                    target_review_count=n_reviews,
                    notes="Single-app live Google Play ingestion",
                )
            else:
                _require_run(conn, run_id)
                resolved_run_id = run_id
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    try:
        summary = ingest_one_app_in_run(
            db_path,
            run_id=resolved_run_id,
            app_id=app_id,
            package_id=package_id,
            n_reviews=n_reviews,
            collect_fn=collect_fn,
            update_run_totals=True,
            reraise=True,
        )
    except Exception:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN")
            try:
                finalize_ingestion_run(conn, resolved_run_id, status="failed")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()
        raise

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        try:
            finalize_ingestion_run(
                conn,
                resolved_run_id,
                status="completed",
                completed_at=summary.get("completed_at") or utc_now(),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    return summary


def ingest_live_apps(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    apps: Sequence[AppRef],
    n_reviews: int = 50,
    collect_fn: CollectFn | None = None,
) -> dict[str, Any]:
    """
    Orchestrate one ingestion run across multiple existing apps.

    Each app is processed independently via `ingest_one_app_in_run`. App failures
    do not stop later apps and do not roll back earlier apps' committed data.
    """
    if not apps:
        raise ValueError("apps must be a non-empty sequence")
    if n_reviews < 1:
        raise ValueError("n_reviews must be >= 1")

    db_path = Path(db_path)
    init_db(db_path)
    started_at = utc_now()

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN")
        try:
            source_id = ensure_data_source(conn, started_at)
            # Fail fast if any configured app is missing.
            for ref in apps:
                resolve_existing_app(
                    conn,
                    app_id=ref.get("app_id"),
                    package_id=ref.get("package_id"),
                )
            run_id = create_ingestion_run(
                conn,
                source_id=source_id,
                started_at=started_at,
                app_count=len(apps),
                target_review_count=n_reviews * len(apps),
                notes="Multi-app live Google Play ingestion",
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()

    app_results: list[dict[str, Any]] = []
    try:
        for ref in apps:
            try:
                result = ingest_one_app_in_run(
                    db_path,
                    run_id=run_id,
                    app_id=ref.get("app_id"),
                    package_id=ref.get("package_id"),
                    n_reviews=n_reviews,
                    collect_fn=collect_fn,
                    update_run_totals=True,
                    reraise=False,
                )
            except Exception as exc:  # noqa: BLE001 - continue other apps
                # Defensive: ingest_one_app_in_run should not raise when reraise=False,
                # but keep the pipeline moving if it does.
                completed_at = utc_now()
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                try:
                    conn.execute("PRAGMA foreign_keys = ON")
                    resolved_app_id, package, name = resolve_existing_app(
                        conn,
                        app_id=ref.get("app_id"),
                        package_id=ref.get("package_id"),
                    )
                    existing = conn.execute(
                        """
                        SELECT id FROM ingestion_run_apps
                        WHERE run_id = ? AND app_id = ?
                        """,
                        (run_id, resolved_app_id),
                    ).fetchone()
                    if existing is None:
                        start_ingestion_run_app(
                            conn, run_id, resolved_app_id, started_at
                        )
                        conn.commit()
                    _mark_app_failed_only(
                        conn,
                        run_id=run_id,
                        app_id=resolved_app_id,
                        fetched_count=0,
                        inserted_count=0,
                        skipped_count=0,
                        error_message=str(exc),
                        completed_at=completed_at,
                        update_run_totals=True,
                    )
                    result = {
                        "db_path": str(db_path.resolve()),
                        "run_id": run_id,
                        "app_id": resolved_app_id,
                        "package_id": package,
                        "app_name": name,
                        **_read_app_result(conn, run_id, resolved_app_id),
                        "processed_created": 0,
                        "flags_created": 0,
                        "ingest_error_count": 0,
                    }
                finally:
                    conn.close()
            app_results.append(result)
    finally:
        # Always set terminal run status + completed_at.
        run_status = compute_run_status([r["status"] for r in app_results])
        completed_at = utc_now()
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("BEGIN")
            try:
                finalize_ingestion_run(
                    conn, run_id, status=run_status, completed_at=completed_at
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        finally:
            conn.close()

    run_row_conn = sqlite3.connect(db_path)
    try:
        run_row = run_row_conn.execute(
            """
            SELECT status, total_fetched, total_inserted, skipped_duplicates,
                   completed_at, error_summary
            FROM ingestion_runs WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
    finally:
        run_row_conn.close()

    return {
        "db_path": str(db_path.resolve()),
        "run_id": run_id,
        "status": run_row[0],
        "total_fetched": int(run_row[1] or 0),
        "total_inserted": int(run_row[2] or 0),
        "skipped_duplicates": int(run_row[3] or 0),
        "completed_at": run_row[4],
        "error_summary": run_row[5],
        "app_results": app_results,
        "apps_completed": sum(1 for r in app_results if r["status"] == "completed"),
        "apps_failed": sum(1 for r in app_results if r["status"] == "failed"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Live-ingest Google Play reviews (single or multi app)."
    )
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--app-id", type=int, default=None, help="Existing apps.app_id")
    parser.add_argument(
        "--package-id",
        action="append",
        default=[],
        help="Existing package id; pass multiple times for multi-app pipeline",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="Reuse an existing run (single-app mode only)",
    )
    parser.add_argument("--n-reviews", type=int, default=50)
    args = parser.parse_args()

    package_ids: list[str] = list(args.package_id)

    if len(package_ids) > 1:
        if args.app_id is not None or args.run_id is not None:
            raise SystemExit(
                "Multi-app mode accepts repeated --package-id only "
                "(do not combine with --app-id / --run-id)."
            )
        summary = ingest_live_apps(
            db_path=args.db_path,
            apps=[{"package_id": pid} for pid in package_ids],
            n_reviews=args.n_reviews,
        )
        print("Multi-app live ingestion finished.")
        print(f"  run_id: {summary['run_id']}")
        print(f"  status: {summary['status']}")
        print(f"  total_fetched: {summary['total_fetched']}")
        print(f"  total_inserted: {summary['total_inserted']}")
        print(f"  skipped_duplicates: {summary['skipped_duplicates']}")
        for app in summary["app_results"]:
            print(
                f"  - {app['app_name']} ({app['package_id']}): "
                f"{app['status']} fetched={app['fetched_count']} "
                f"inserted={app['inserted_count']} skipped={app['skipped_count']}"
            )
        return

    package_id = package_ids[0] if package_ids else None
    summary = ingest_live_app(
        db_path=args.db_path,
        app_id=args.app_id,
        package_id=package_id,
        run_id=args.run_id,
        n_reviews=args.n_reviews,
    )
    print("Live single-app ingestion completed.")
    for key in (
        "db_path",
        "run_id",
        "app_id",
        "package_id",
        "app_name",
        "status",
        "fetched_count",
        "inserted_count",
        "skipped_count",
        "error_message",
        "processed_created",
        "flags_created",
    ):
        print(f"  {key}: {summary[key]}")


if __name__ == "__main__":
    main()
