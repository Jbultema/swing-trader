from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pandas as pd
from dotenv import load_dotenv


class RefreshJobError(RuntimeError):
    """Raised when a refresh job cannot be queued or reconciled safely."""


@dataclass(frozen=True)
class RefreshJob:
    job_id: str
    job_type: str
    label: str
    status: str
    created_at_utc: str
    started_at_utc: str
    completed_at_utc: str
    process_id: int
    return_code: int | None
    log_path: str
    error_message: str
    reused: bool = False


JOB_LABELS = {
    "stock_daily": "Stock daily refresh",
    "stock_evaluate": "Forward evidence evaluation",
    "etf_daily": "Historical ETF research refresh",
}
ACTIVE_STATUSES = ("queued", "running")
TERMINAL_STATUSES = ("completed", "failed")
MAXIMUM_ACTIVE_AGE = timedelta(minutes=45)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_jobs (
    job_id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    label TEXT NOT NULL,
    command_json TEXT NOT NULL,
    root_path TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed')),
    created_at_utc TEXT NOT NULL,
    started_at_utc TEXT NOT NULL DEFAULT '',
    completed_at_utc TEXT NOT NULL DEFAULT '',
    process_id INTEGER NOT NULL DEFAULT 0,
    return_code INTEGER,
    log_path TEXT NOT NULL,
    error_message TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_refresh_jobs_created
ON refresh_jobs(created_at_utc DESC);

CREATE INDEX IF NOT EXISTS ix_refresh_jobs_status
ON refresh_jobs(status, created_at_utc DESC);

CREATE UNIQUE INDEX IF NOT EXISTS ux_refresh_jobs_single_active
ON refresh_jobs((1))
WHERE status IN ('queued', 'running');
"""


class RefreshJobStore:
    """Persistent local job queue for whitelisted dashboard refresh operations."""

    def __init__(self, path: Path | str, *, log_dir: Path | str, root: Path | str) -> None:
        self.path = Path(path).resolve()
        self.log_dir = Path(log_dir).resolve()
        self.root = Path(root).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def queue(self, job_type: str) -> RefreshJob:
        clean_type = job_type.strip().lower()
        if clean_type not in JOB_LABELS:
            raise RefreshJobError(f"Unsupported refresh job: {job_type}")
        self.reconcile_active_jobs()
        active = self.active_job()
        if active is not None:
            if active.job_type == clean_type:
                return RefreshJob(**{**active.__dict__, "reused": True})
            raise RefreshJobError(
                f"{active.label} is already {active.status}; wait for it to finish before "
                f"starting {JOB_LABELS[clean_type]}."
            )

        job_id = f"refresh-{uuid4().hex[:20]}"
        label = JOB_LABELS[clean_type]
        command = _job_command(clean_type)
        created_at = _utc_now()
        log_path = self.log_dir / f"{job_id}.log"
        log_path.touch(exist_ok=False)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO refresh_jobs(
                        job_id, job_type, label, command_json, root_path, status,
                        created_at_utc, log_path
                    ) VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)
                    """,
                    (
                        job_id,
                        clean_type,
                        label,
                        json.dumps(command),
                        str(self.root),
                        created_at,
                        str(log_path),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            log_path.unlink(missing_ok=True)
            active = self.active_job()
            if active is not None and active.job_type == clean_type:
                return RefreshJob(**{**active.__dict__, "reused": True})
            if active is not None:
                raise RefreshJobError(
                    f"{active.label} is already {active.status}; wait for it to finish."
                ) from exc
            raise RefreshJobError("A refresh job could not acquire the single-writer lock.") from exc

        worker_command = [
            sys.executable,
            "-m",
            "swing_trader.refresh_jobs",
            "--worker",
            "--db",
            str(self.path),
            "--log-dir",
            str(self.log_dir),
            "--root",
            str(self.root),
            "--job-id",
            job_id,
        ]
        try:
            with log_path.open("a", encoding="utf-8") as log_handle:
                process = subprocess.Popen(  # noqa: S603 - fixed module worker and validated paths
                    worker_command,
                    cwd=self.root,
                    stdin=subprocess.DEVNULL,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
        except OSError as exc:
            self.mark_failed(job_id, return_code=None, error_message=f"Could not start: {exc}")
            raise RefreshJobError(f"Could not start {label}: {exc}") from exc
        with self._connect() as connection:
            connection.execute(
                "UPDATE refresh_jobs SET process_id = ? WHERE job_id = ?",
                (int(process.pid), job_id),
            )
        return self.get(job_id)

    def get(self, job_id: str) -> RefreshJob:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM refresh_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise RefreshJobError(f"Unknown refresh job: {job_id}")
        return _row_to_job(row)

    def list_jobs(self, *, limit: int = 8) -> pd.DataFrame:
        self.reconcile_active_jobs()
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT job_id, job_type, label, status, created_at_utc,
                       started_at_utc, completed_at_utc, process_id,
                       return_code, log_path, error_message
                FROM refresh_jobs
                ORDER BY created_at_utc DESC
                LIMIT ?
                """,
                connection,
                params=[max(1, int(limit))],
            )

    def active_job(self) -> RefreshJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM refresh_jobs
                WHERE status IN ('queued', 'running')
                ORDER BY created_at_utc DESC
                LIMIT 1
                """
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def mark_running(self, job_id: str, *, process_id: int | None = None) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE refresh_jobs
                SET status = 'running', started_at_utc = ?, process_id = ?
                WHERE job_id = ? AND status = 'queued'
                """,
                (_utc_now(), int(process_id or os.getpid()), job_id),
            )
        if cursor.rowcount != 1:
            current = self.get(job_id)
            if current.status != "running":
                raise RefreshJobError(
                    f"Cannot mark {job_id} running from status {current.status}."
                )

    def mark_completed(self, job_id: str, *, return_code: int = 0) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE refresh_jobs
                SET status = 'completed', completed_at_utc = ?, return_code = ?,
                    error_message = ''
                WHERE job_id = ? AND status IN ('queued', 'running')
                """,
                (_utc_now(), int(return_code), job_id),
            )
        if cursor.rowcount != 1 and self.get(job_id).status != "completed":
            raise RefreshJobError(f"Cannot complete refresh job {job_id}.")

    def mark_failed(
        self,
        job_id: str,
        *,
        return_code: int | None,
        error_message: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE refresh_jobs
                SET status = 'failed', completed_at_utc = ?, return_code = ?,
                    error_message = ?
                WHERE job_id = ? AND status IN ('queued', 'running')
                """,
                (_utc_now(), return_code, error_message.strip()[:2000], job_id),
            )

    def reconcile_active_jobs(self, *, now: datetime | None = None) -> None:
        checked_at = _as_utc(now or datetime.now(UTC))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM refresh_jobs WHERE status IN ('queued', 'running')"
            ).fetchall()
        for row in rows:
            job = _row_to_job(row)
            created = _as_utc(datetime.fromisoformat(job.created_at_utc))
            too_old = checked_at - created > MAXIMUM_ACTIVE_AGE
            dead = job.process_id > 0 and not _process_is_alive(job.process_id)
            unclaimed = job.process_id <= 0 and checked_at - created > timedelta(seconds=30)
            if too_old or dead or unclaimed:
                reason = (
                    "Job exceeded the 45-minute local safety limit."
                    if too_old
                    else "Job process is no longer running."
                )
                self.mark_failed(job.job_id, return_code=None, error_message=reason)

    def read_log_tail(self, job_id: str, *, max_characters: int = 6000) -> str:
        job = self.get(job_id)
        path = Path(job.log_path)
        if not path.is_file() or path.parent.resolve() != self.log_dir:
            return ""
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-max(100, int(max_characters)) :]


def run_job_worker(
    store: RefreshJobStore,
    job_id: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[object]] = subprocess.run,
) -> int:
    job = store.get(job_id)
    if job.status in TERMINAL_STATUSES:
        return int(job.return_code or 0)
    store.mark_running(job_id)
    with store._connect() as connection:
        row = connection.execute(
            "SELECT command_json, root_path FROM refresh_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        raise RefreshJobError(f"Unknown refresh job: {job_id}")
    command = json.loads(str(row["command_json"]))
    if not isinstance(command, list) or not command or any(
        not isinstance(part, str) for part in command
    ):
        store.mark_failed(job_id, return_code=None, error_message="Stored command is invalid.")
        return 2
    if command != _job_command(job.job_type):
        store.mark_failed(
            job_id,
            return_code=None,
            error_message="Stored command does not match the whitelisted job type.",
        )
        return 2
    root = Path(str(row["root_path"])).resolve()
    if root != store.root:
        store.mark_failed(job_id, return_code=None, error_message="Stored job root is invalid.")
        return 2
    load_dotenv(root / ".env", override=False)
    print(f"[{_utc_now()}] Starting {job.label} ({job.job_id})", flush=True)
    print("Command: " + " ".join(command), flush=True)
    try:
        result = runner(command, cwd=root, check=False)  # noqa: S603 - whitelisted command
    except OSError as exc:
        store.mark_failed(job_id, return_code=None, error_message=f"Execution failed: {exc}")
        print(f"Execution failed: {exc}", flush=True)
        return 2
    return_code = int(result.returncode)
    if return_code == 0:
        store.mark_completed(job_id, return_code=0)
        print(f"[{_utc_now()}] Completed {job.label}", flush=True)
    else:
        message = f"Command exited with code {return_code}. Review the job log."
        store.mark_failed(job_id, return_code=return_code, error_message=message)
        print(message, flush=True)
    return return_code


def _job_command(job_type: str) -> list[str]:
    executable = Path(sys.executable).with_name("swing-trader")
    command = str(executable) if executable.is_file() else "swing-trader"
    if job_type == "stock_daily":
        return [command, "stock-daily"]
    if job_type == "stock_evaluate":
        return [command, "shadow", "evaluate-stock-lineage"]
    if job_type == "etf_daily":
        return [command, "daily"]
    raise RefreshJobError(f"Unsupported refresh job: {job_type}")


def _row_to_job(row: sqlite3.Row) -> RefreshJob:
    return RefreshJob(
        job_id=str(row["job_id"]),
        job_type=str(row["job_type"]),
        label=str(row["label"]),
        status=str(row["status"]),
        created_at_utc=str(row["created_at_utc"]),
        started_at_utc=str(row["started_at_utc"]),
        completed_at_utc=str(row["completed_at_utc"]),
        process_id=int(row["process_id"]),
        return_code=None if row["return_code"] is None else int(row["return_code"]),
        log_path=str(row["log_path"]),
        error_message=str(row["error_message"]),
    )


def _process_is_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except (OSError, ValueError):
        return False
    return True


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_args(arguments: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Swing Trader local refresh worker")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    args = _parse_args(arguments)
    if not args.worker:
        raise RefreshJobError("refresh_jobs may only be invoked in worker mode.")
    store = RefreshJobStore(args.db, log_dir=args.log_dir, root=args.root)
    return run_job_worker(store, str(args.job_id))


if __name__ == "__main__":
    raise SystemExit(main())
