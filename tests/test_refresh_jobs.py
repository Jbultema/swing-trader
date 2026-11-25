from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from swing_trader import refresh_jobs
from swing_trader.refresh_jobs import RefreshJobError, RefreshJobStore, run_job_worker


class _DummyPopen:
    def __init__(self, command: list[str], **kwargs: object) -> None:
        self.command = command
        self.kwargs = kwargs
        self.pid = os.getpid()


def _store(tmp_path: Path) -> RefreshJobStore:
    return RefreshJobStore(
        tmp_path / "jobs.sqlite",
        log_dir=tmp_path / "logs",
        root=tmp_path,
    )


def test_queue_records_background_worker_and_reuses_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[_DummyPopen] = []

    def fake_popen(command: list[str], **kwargs: object) -> _DummyPopen:
        process = _DummyPopen(command, **kwargs)
        calls.append(process)
        return process

    monkeypatch.setattr(refresh_jobs.subprocess, "Popen", fake_popen)
    store = _store(tmp_path)

    first = store.queue("stock_daily")
    duplicate = store.queue("stock_daily")

    assert first.status == "queued"
    assert duplicate.job_id == first.job_id
    assert duplicate.reused
    assert len(calls) == 1
    assert calls[0].command[:3] == [
        refresh_jobs.sys.executable,
        "-m",
        "swing_trader.refresh_jobs",
    ]
    assert Path(first.log_path).is_file()
    assert store.list_jobs().iloc[0]["job_id"] == first.job_id


def test_queue_blocks_concurrent_different_refresh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(refresh_jobs.subprocess, "Popen", _DummyPopen)
    store = _store(tmp_path)
    store.queue("stock_daily")

    with pytest.raises(RefreshJobError, match="already queued"):
        store.queue("etf_daily")


def test_worker_marks_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(refresh_jobs.subprocess, "Popen", _DummyPopen)
    store = _store(tmp_path)
    success = store.queue("stock_evaluate")
    commands: list[list[str]] = []

    def successful_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    assert run_job_worker(store, success.job_id, runner=successful_runner) == 0
    assert store.get(success.job_id).status == "completed"
    assert commands[0][-2:] == ["shadow", "evaluate-stock-lineage"]

    failed = store.queue("etf_daily")

    def failing_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[object]:
        return subprocess.CompletedProcess(command, 3)

    assert run_job_worker(store, failed.job_id, runner=failing_runner) == 3
    failed_record = store.get(failed.job_id)
    assert failed_record.status == "failed"
    assert failed_record.return_code == 3
    assert "Review the job log" in failed_record.error_message


def test_reconcile_marks_dead_or_old_job_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(refresh_jobs.subprocess, "Popen", _DummyPopen)
    store = _store(tmp_path)
    job = store.queue("stock_daily")
    monkeypatch.setattr(refresh_jobs, "_process_is_alive", lambda _process_id: False)

    store.reconcile_active_jobs()

    assert store.get(job.job_id).status == "failed"
    assert "no longer running" in store.get(job.job_id).error_message

    old = store.queue("stock_daily")
    monkeypatch.setattr(refresh_jobs, "_process_is_alive", lambda _process_id: True)
    store.reconcile_active_jobs(
        now=datetime.now(UTC) + timedelta(minutes=46),
    )
    assert store.get(old.job_id).status == "failed"
    assert "45-minute" in store.get(old.job_id).error_message


def test_queue_records_spawn_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken_popen(*_args: object, **_kwargs: object) -> object:
        raise OSError("spawn unavailable")

    monkeypatch.setattr(refresh_jobs.subprocess, "Popen", broken_popen)
    store = _store(tmp_path)

    with pytest.raises(RefreshJobError, match="spawn unavailable"):
        store.queue("stock_daily")

    jobs = store.list_jobs()
    assert jobs.iloc[0]["status"] == "failed"
    assert "Could not start" in jobs.iloc[0]["error_message"]


def test_job_type_is_whitelisted(tmp_path: Path) -> None:
    with pytest.raises(RefreshJobError, match="Unsupported"):
        _store(tmp_path).queue("arbitrary-shell-command")


def test_worker_rejects_tampered_stored_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(refresh_jobs.subprocess, "Popen", _DummyPopen)
    store = _store(tmp_path)
    job = store.queue("stock_evaluate")
    with store._connect() as connection:
        connection.execute(
            "UPDATE refresh_jobs SET command_json = ? WHERE job_id = ?",
            ('["/bin/echo", "not-allowed"]', job.job_id),
        )

    assert run_job_worker(store, job.job_id) == 2
    assert store.get(job.job_id).status == "failed"
    assert "whitelisted" in store.get(job.job_id).error_message
