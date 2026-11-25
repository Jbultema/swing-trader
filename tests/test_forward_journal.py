from __future__ import annotations

import json
from datetime import UTC, datetime

import pandas as pd
import pytest

from swing_trader.forward_journal import (
    ForwardJournal,
    ForwardJournalError,
    ValuationConflictError,
    load_state_price_marks,
)
from swing_trader.provenance import file_sha256, tabular_sha256
from swing_trader.stock_live_data import PRICE_PROVIDER_ROLE, SOURCE_QUALITY_POLICY


def _state() -> dict[str, object]:
    return {
        "record_sha256": "state-hash",
        "lineage_id": "policy-test-config-test",
        "stock_policy_sha256": "policy-test",
        "as_of_session": "2026-10-07",
        "eligible_for_primary_prospective_performance": True,
        "market_state": {"close": 100.0},
        "arms": {
            "consensus": {
                "target_for_next_open": {"AAA": 0.6, "BBB": 0.3},
                "target_cash_weight": 0.1,
                "decisions": [
                    {"ticker": "AAA", "action": "BUY", "reasons": ["top_consensus_entry"]},
                    {"ticker": "BBB", "action": "BUY", "reasons": ["top_consensus_entry"]},
                ],
            }
        },
    }


def test_forward_journal_locks_executes_and_values_long_only_book(tmp_path) -> None:
    journal = ForwardJournal(tmp_path / "journal.sqlite")
    book = journal.create_book(
        "Primary paper",
        starting_cash=10_000.0,
        now=datetime(2026, 10, 8, tzinfo=UTC),
    )

    alignment = journal.build_alignment(
        book.book_id,
        _state(),
        {"AAA": 100.0, "BBB": 50.0},
    )
    assert alignment.set_index("ticker").loc["AAA", "planned_quantity"] == 60.0
    assert alignment.set_index("ticker").loc["BBB", "planned_quantity"] == 60.0

    tickets = journal.lock_alignment(book.book_id, _state(), alignment)
    assert set(tickets["status"]) == {"OPEN"}
    aaa = tickets.loc[tickets["ticker"] == "AAA"].iloc[0]
    execution_id = journal.record_execution(
        book.book_id,
        ticket_id=str(aaa["ticket_id"]),
        ticker="AAA",
        side="BUY",
        quantity=60.0,
        price=100.0,
        fees=2.0,
        executed_at=datetime(2026, 10, 8, 14, 30, tzinfo=UTC),
    )
    assert execution_id.startswith("execution-")
    assert journal.load_tickets(book.book_id).set_index("ticker").loc["AAA", "status"] == (
        "EXECUTED"
    )

    snapshot = journal.book_snapshot(book.book_id, {"AAA": 105.0})
    assert snapshot.cash == pytest.approx(3_998.0)
    assert snapshot.total_equity == pytest.approx(10_298.0)
    assert snapshot.unrealized_pnl == pytest.approx(298.0)
    assert snapshot.positions.iloc[0]["current_weight"] == pytest.approx(6_300 / 10_298)

    first = journal.record_valuation(
        book.book_id,
        market_session="2026-10-08",
        prices={"AAA": 105.0},
        spy_close=780.0,
        source_record_sha256="state-hash-next",
        source_lineage_id="policy-test-config-test",
        now=datetime(2026, 10, 8, 21, tzinfo=UTC),
    )
    assert journal.record_valuation(
        book.book_id,
        market_session="2026-10-08",
        prices={"AAA": 105.0},
        spy_close=780.0,
        source_record_sha256="state-hash-next",
        source_lineage_id="policy-test-config-test",
    ) == first
    performance = journal.performance_frame(book.book_id)
    assert performance.iloc[0]["book_cumulative_return"] == 0.0
    assert performance.iloc[0]["spy_cumulative_return"] == 0.0
    assert bool(performance.iloc[0]["eligible_model_context"])
    assert set(journal.audit_events(book.book_id)["event_type"]) == {
        "BOOK_CREATED",
        "RECOMMENDATION_LOCKED",
        "EXECUTION_RECORDED",
        "VALUATION_RECORDED",
    }


def test_forward_journal_rejects_oversell_and_unfunded_buy(tmp_path) -> None:
    journal = ForwardJournal(tmp_path / "journal.sqlite")
    book = journal.create_book("Paper", starting_cash=1_000.0)

    with pytest.raises(ForwardJournalError, match="shorts are prohibited"):
        journal.record_execution(
            book.book_id,
            ticker="AAA",
            side="SELL",
            quantity=1.0,
            price=100.0,
        )
    with pytest.raises(ForwardJournalError, match="requires"):
        journal.record_execution(
            book.book_id,
            ticker="AAA",
            side="BUY",
            quantity=11.0,
            price=100.0,
        )


def test_forward_journal_tracks_realized_pnl_and_partial_ticket(tmp_path) -> None:
    journal = ForwardJournal(tmp_path / "journal.sqlite")
    book = journal.create_book("Paper", starting_cash=10_000.0)
    alignment = journal.build_alignment(book.book_id, _state(), {"AAA": 100.0, "BBB": 50.0})
    ticket = journal.lock_alignment(book.book_id, _state(), alignment).iloc[0]
    journal.record_execution(
        book.book_id,
        ticket_id=str(ticket["ticket_id"]),
        ticker=str(ticket["ticker"]),
        side=str(ticket["side"]),
        quantity=float(ticket["planned_quantity"]) / 2.0,
        price=float(ticket["reference_price"]),
    )
    assert journal.load_tickets(book.book_id).iloc[0]["status"] == "PARTIAL"

    ticker = str(ticket["ticker"])
    journal.record_execution(
        book.book_id,
        ticker=ticker,
        side="SELL",
        quantity=float(ticket["planned_quantity"]) / 4.0,
        price=float(ticket["reference_price"]) * 1.1,
        fees=1.0,
    )
    snapshot = journal.book_snapshot(
        book.book_id,
        {ticker: float(ticket["reference_price"]) * 1.1},
    )
    assert snapshot.realized_pnl > 0.0
    assert snapshot.positions.iloc[0]["shares"] == pytest.approx(
        float(ticket["planned_quantity"]) / 4.0
    )


def test_alignment_shows_sell_when_model_removes_a_held_name(tmp_path) -> None:
    journal = ForwardJournal(tmp_path / "journal.sqlite")
    book = journal.create_book("Paper", starting_cash=10_000.0)
    journal.record_execution(
        book.book_id,
        ticker="OLD",
        side="BUY",
        quantity=10.0,
        price=100.0,
    )
    alignment = journal.build_alignment(
        book.book_id,
        _state(),
        {"AAA": 100.0, "BBB": 50.0, "OLD": 90.0},
    ).set_index("ticker")

    assert alignment.loc["OLD", "side"] == "SELL"
    assert alignment.loc["OLD", "planned_quantity"] == pytest.approx(10.0)
    assert isinstance(journal.load_executions(book.book_id), pd.DataFrame)


def test_record_all_book_valuations_marks_each_local_book(tmp_path) -> None:
    journal = ForwardJournal(tmp_path / "journal.sqlite")
    first = journal.create_book("First", starting_cash=10_000.0)
    second = journal.create_book("Second", starting_cash=20_000.0)

    result = journal.record_all_book_valuations(
        _state(),
        {"AAA": 100.0, "BBB": 50.0, "SPY": 100.0},
        source_policy_current=True,
    )

    assert result.books_seen == 2
    assert result.valuations_recorded == 2
    assert result.market_session == "2026-10-07"
    assert len(journal.performance_frame(first.book_id)) == 1
    assert len(journal.performance_frame(second.book_id)) == 1


def test_session_valuation_refuses_a_different_source_record(tmp_path) -> None:
    journal = ForwardJournal(tmp_path / "journal.sqlite")
    book = journal.create_book("Paper", starting_cash=10_000.0)
    journal.record_valuation(
        book.book_id,
        market_session="2026-10-07",
        prices={"AAA": 100.0},
        spy_close=100.0,
        source_record_sha256="original",
    )

    with pytest.raises(ValuationConflictError, match="different source record"):
        journal.record_valuation(
            book.book_id,
            market_session="2026-10-07",
            prices={"AAA": 101.0},
            spy_close=101.0,
            source_record_sha256="replacement",
        )

    valuations = journal.performance_frame(book.book_id)
    assert len(valuations) == 1
    assert valuations.iloc[0]["source_record_sha256"] == "original"


def test_load_state_price_marks_verifies_bound_snapshot(tmp_path) -> None:
    snapshot_dir = tmp_path / "data/stock-shadow/prices"
    snapshot_dir.mkdir(parents=True)
    frame = pd.DataFrame(
        [[10.0, 100.0], [11.0, 101.0]],
        index=pd.to_datetime(["2026-10-06", "2026-10-07"]),
        columns=pd.MultiIndex.from_tuples(
            [("Close", "AAA"), ("Close", "SPY")],
            names=["field", "ticker"],
        ),
    )
    frame.index.name = "Date"
    data_path = snapshot_dir / "prices.parquet"
    frame.to_parquet(data_path)
    manifest = {
        "schema_version": 2,
        "provider_role": PRICE_PROVIDER_ROLE,
        "captured_at_utc": "2026-10-08T00:00:00+00:00",
        "data_file": data_path.name,
        "data_file_sha256": file_sha256(data_path),
        "tabular_sha256": tabular_sha256(frame),
        "rows": len(frame),
        "price_data_gate_passed": True,
        "source_quality": {"policy": SOURCE_QUALITY_POLICY, "status": "passed"},
    }
    manifest_path = snapshot_dir / "prices.manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    state = _state() | {
        "inputs": {
            "price_manifest": manifest_path.name,
            "price_manifest_sha256": file_sha256(manifest_path),
        }
    }

    assert load_state_price_marks(tmp_path, state) == {"AAA": 11.0, "SPY": 101.0}

    manifest_path.write_text(manifest_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ForwardJournalError, match="manifest hash"):
        load_state_price_marks(tmp_path, state)
