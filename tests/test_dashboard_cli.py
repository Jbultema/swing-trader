from __future__ import annotations

import socket
from types import SimpleNamespace

from typer.testing import CliRunner

from swing_trader import cli
from swing_trader.forward_journal import ValuationConflictError


def test_dashboard_uses_collision_safe_local_default(
    monkeypatch: object,
) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_local_port_available", lambda _port: True)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command),
    )

    result = CliRunner().invoke(cli.app, ["dashboard"])

    assert result.exit_code == 0
    assert commands
    assert "src/swing_trader/dashboard.py" in commands[0][2]
    assert "--server.address" not in commands[0]
    assert commands[0][commands[0].index("--server.port") + 1] == "8503"


def test_dashboard_accepts_port_override(monkeypatch: object) -> None:
    commands: list[list[str]] = []
    monkeypatch.setattr(cli, "_local_port_available", lambda port: port == 8510)
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, **_kwargs: commands.append(command),
    )

    result = CliRunner().invoke(cli.app, ["dashboard", "--port", "8510"])

    assert result.exit_code == 0
    assert commands[0][commands[0].index("--server.port") + 1] == "8510"


def test_dashboard_fails_clearly_when_port_is_occupied(monkeypatch: object) -> None:
    monkeypatch.setattr(cli, "_local_port_available", lambda _port: False)
    called = False

    def unexpected_run(*_args: object, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(cli.subprocess, "run", unexpected_run)

    result = CliRunner().invoke(cli.app, ["dashboard"])

    assert result.exit_code == 2
    assert "Port 8503 is already in use" in result.output
    assert "--port 8504" in result.output
    assert not called


def test_local_port_probe_detects_actual_listener() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("", 0))
        listener.listen()
        port = int(listener.getsockname()[1])

        assert not cli._local_port_available(port)

    assert cli._local_port_available(port)


def test_stock_daily_preserves_same_session_valuation_on_policy_rollover(
    tmp_path,
    monkeypatch,
) -> None:
    journal = tmp_path / "data/forward-journal/swing-trader.sqlite"
    journal.parent.mkdir(parents=True)
    journal.touch()
    state = tmp_path / "state.json"
    state.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(cli, "_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "_load_local_environment", lambda _root: None)
    monkeypatch.setattr(
        cli,
        "run_stock_shadow_daily",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="state_recorded",
            session="2026-10-21",
            lineage_id="policy-new",
            alpha_status="failed",
            earnings_status="passed",
            finra_status="passed_experimental_context_only",
            sec_status="failed_nonblocking_experimental",
            state_path=state,
        ),
    )
    monkeypatch.setattr(
        cli,
        "mark_local_books_from_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValuationConflictError("different source record")
        ),
    )

    result = CliRunner().invoke(cli.app, ["stock-daily"])

    assert result.exit_code == 0
    assert "preserved it and skipped revaluation" in result.output
