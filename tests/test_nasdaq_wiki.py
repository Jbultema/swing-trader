from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import pytest

from swing_trader.nasdaq_wiki import (
    WIKI_COLUMNS,
    NasdaqWikiError,
    download_nasdaq_wiki_archive,
    probe_nasdaq_wiki_access,
    verify_nasdaq_wiki_archive,
    verify_nasdaq_wiki_capability,
)


class _Response:
    def __init__(self, *, payload: object | None = None, content: bytes = b"") -> None:
        self._payload = payload
        self._content = content

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self._payload

    def iter_content(self, chunk_size: int):  # noqa: ANN201
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start : start + chunk_size]


def _archive(columns: tuple[str, ...] = WIKI_COLUMNS) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("WIKI_PRICES.csv", ",".join(columns) + "\n")
    return buffer.getvalue()


def test_probe_records_access_without_key_or_raw_row(tmp_path: Path) -> None:
    provider = {
        "datatable": {
            "columns": [
                {"name": "ticker"},
                {"name": "date"},
                {"name": "adj_close"},
            ],
            "data": [["AAPL", "2018-03-27", 41.2]],
        }
    }
    result = probe_nasdaq_wiki_access(
        "secret-key",
        tmp_path,
        requester=lambda *_args, **_kwargs: _Response(payload=provider),
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    text = result.path.read_text()
    payload = json.loads(text)

    assert result.status == "available"
    assert result.usable_with_configured_key is True
    assert payload["response_rows"] == 1
    assert payload["raw_rows_retained"] is False
    assert "secret-key" not in text
    assert "41.2" not in text
    assert verify_nasdaq_wiki_capability(result.path)


def test_download_locks_and_verifies_official_archive(tmp_path: Path) -> None:
    export = {
        "datatable_bulk_download": {
            "file": {
                "status": "fresh",
                "link": "https://downloads.example.amazonaws.com/WIKI_PRICES.zip",
            }
        }
    }
    responses = iter(
        [
            _Response(payload=export),
            _Response(content=_archive()),
        ]
    )
    output = tmp_path / "WIKI_PRICES.zip"
    result = download_nasdaq_wiki_archive(
        "secret-key",
        output,
        requester=lambda *_args, **_kwargs: next(responses),
        sleep=lambda _seconds: None,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )
    manifest_text = result.manifest_path.read_text()

    assert output.is_file()
    assert verify_nasdaq_wiki_archive(output)
    assert "secret-key" not in manifest_text
    assert '"historical_backtest_ready": false' in manifest_text
    assert '"action_authorized": false' in manifest_text


def test_download_polls_until_export_is_fresh(tmp_path: Path) -> None:
    responses = iter(
        [
            _Response(
                payload={
                    "datatable_bulk_download": {
                        "file": {"status": "creating", "link": None}
                    }
                }
            ),
            _Response(
                payload={
                    "datatable_bulk_download": {
                        "file": {
                            "status": "fresh",
                            "link": "https://data.nasdaq.com/WIKI_PRICES.zip",
                        }
                    }
                }
            ),
            _Response(content=_archive()),
        ]
    )
    sleeps: list[float] = []

    download_nasdaq_wiki_archive(
        "secret-key",
        tmp_path / "WIKI_PRICES.zip",
        requester=lambda *_args, **_kwargs: next(responses),
        sleep=sleeps.append,
        poll_attempts=2,
        poll_seconds=3.0,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )

    assert sleeps == [3.0]


def test_download_rejects_unsafe_link_and_bad_schema(tmp_path: Path) -> None:
    unsafe = {
        "datatable_bulk_download": {
            "file": {"status": "fresh", "link": "http://example.com/WIKI.zip"}
        }
    }
    with pytest.raises(NasdaqWikiError, match="unsafe"):
        download_nasdaq_wiki_archive(
            "secret",
            tmp_path / "unsafe.zip",
            requester=lambda *_args, **_kwargs: _Response(payload=unsafe),
        )

    export = {
        "datatable_bulk_download": {
            "file": {
                "status": "fresh",
                "link": "https://data.nasdaq.com/WIKI.zip",
            }
        }
    }
    responses = iter([_Response(payload=export), _Response(content=_archive(("bad",)))])
    with pytest.raises(NasdaqWikiError, match="failed validation"):
        download_nasdaq_wiki_archive(
            "secret",
            tmp_path / "bad.zip",
            requester=lambda *_args, **_kwargs: next(responses),
        )
