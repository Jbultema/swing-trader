from __future__ import annotations

import os
from pathlib import Path

import pytest

from swing_trader.cli import _load_local_environment


def test_local_env_loads_without_overriding_process_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("ALPHA_VANTAGE_API_KEY=local-alpha\n", encoding="utf-8")
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "process-alpha")

    _load_local_environment(tmp_path)

    assert os.environ["ALPHA_VANTAGE_API_KEY"] == "process-alpha"
