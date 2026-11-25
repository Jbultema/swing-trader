from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np
import pandas as pd

from swing_trader.provenance import file_sha256
from swing_trader.stock_live_data import audit_current_stock_price_snapshot
from swing_trader.stock_shadow_state import verify_stock_shadow_state


class ForwardJournalError(ValueError):
    """Raised when a journal operation would make the book internally inconsistent."""


class ValuationConflictError(ForwardJournalError):
    """Raised when an immutable session valuation is bound to another state."""


@dataclass(frozen=True)
class JournalBook:
    book_id: str
    name: str
    mode: str
    account_label: str
    starting_cash: float
    created_at_utc: str


@dataclass(frozen=True)
class BookSnapshot:
    book: JournalBook
    cash: float
    positions_value: float
    total_equity: float
    gross_exposure: float
    realized_pnl: float
    unrealized_pnl: float
    cumulative_fees: float
    positions: pd.DataFrame


@dataclass(frozen=True)
class JournalValuationRun:
    market_session: str
    books_seen: int
    valuations_recorded: int


_SCHEMA = """
CREATE TABLE IF NOT EXISTS journal_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS books (
    book_id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    mode TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    account_label TEXT NOT NULL,
    starting_cash REAL NOT NULL CHECK (starting_cash > 0),
    created_at_utc TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recommendation_tickets (
    ticket_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(book_id),
    locked_at_utc TEXT NOT NULL,
    as_of_session TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    lineage_id TEXT NOT NULL,
    arm_name TEXT NOT NULL,
    stock_policy_sha256 TEXT NOT NULL,
    state_record_sha256 TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    target_weight REAL NOT NULL CHECK (target_weight >= 0 AND target_weight <= 1),
    current_weight REAL NOT NULL CHECK (current_weight >= 0),
    delta_value REAL NOT NULL,
    reference_price REAL NOT NULL CHECK (reference_price > 0),
    planned_quantity REAL NOT NULL CHECK (planned_quantity > 0),
    reasons_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('OPEN', 'PARTIAL', 'EXECUTED', 'SKIPPED', 'EXPIRED')),
    notes TEXT NOT NULL DEFAULT '',
    UNIQUE (book_id, state_record_sha256, arm_name, ticker, side)
);

CREATE TABLE IF NOT EXISTS executions (
    execution_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(book_id),
    ticket_id TEXT REFERENCES recommendation_tickets(ticket_id),
    executed_at_utc TEXT NOT NULL,
    ticker TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity REAL NOT NULL CHECK (quantity > 0),
    price REAL NOT NULL CHECK (price > 0),
    fees REAL NOT NULL CHECK (fees >= 0),
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS valuations (
    valuation_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(book_id),
    valued_at_utc TEXT NOT NULL,
    market_session TEXT NOT NULL,
    cash REAL NOT NULL,
    positions_value REAL NOT NULL,
    total_equity REAL NOT NULL CHECK (total_equity >= 0),
    gross_exposure REAL NOT NULL CHECK (gross_exposure >= 0),
    realized_pnl REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    cumulative_fees REAL NOT NULL CHECK (cumulative_fees >= 0),
    spy_close REAL NOT NULL CHECK (spy_close > 0),
    marks_json TEXT NOT NULL,
    source_record_sha256 TEXT NOT NULL,
    source_lineage_id TEXT NOT NULL,
    source_gate_passed INTEGER NOT NULL CHECK (source_gate_passed IN (0, 1)),
    source_policy_current INTEGER NOT NULL CHECK (source_policy_current IN (0, 1)),
    UNIQUE (book_id, market_session)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    recorded_at_utc TEXT NOT NULL,
    book_id TEXT NOT NULL REFERENCES books(book_id),
    event_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_tickets_book_status
ON recommendation_tickets(book_id, status, locked_at_utc);

CREATE INDEX IF NOT EXISTS ix_executions_book_time
ON executions(book_id, executed_at_utc);

CREATE INDEX IF NOT EXISTS ix_valuations_book_session
ON valuations(book_id, market_session);
"""


class ForwardJournal:
    """Local append-oriented paper/live journal with no broker connectivity."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO journal_meta(key, value) VALUES ('schema_version', '1')"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def create_book(
        self,
        name: str,
        *,
        mode: str = "paper",
        account_label: str = "retirement",
        starting_cash: float = 100_000.0,
        now: datetime | None = None,
    ) -> JournalBook:
        clean_name = name.strip()
        clean_account = account_label.strip()
        clean_mode = mode.strip().lower()
        if not clean_name:
            raise ForwardJournalError("Book name is required.")
        if clean_mode not in {"paper", "live"}:
            raise ForwardJournalError("Book mode must be paper or live.")
        if not clean_account:
            raise ForwardJournalError("Account label is required.")
        if not np.isfinite(starting_cash) or starting_cash <= 0.0:
            raise ForwardJournalError("Starting cash must be positive and finite.")
        book = JournalBook(
            book_id=f"book-{uuid4().hex[:16]}",
            name=clean_name,
            mode=clean_mode,
            account_label=clean_account,
            starting_cash=float(starting_cash),
            created_at_utc=_utc_text(now),
        )
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO books(book_id, name, mode, account_label, starting_cash, created_at_utc)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        book.book_id,
                        book.name,
                        book.mode,
                        book.account_label,
                        book.starting_cash,
                        book.created_at_utc,
                    ),
                )
                self._append_event(connection, book.book_id, "BOOK_CREATED", book.book_id, book.__dict__)
        except sqlite3.IntegrityError as exc:
            raise ForwardJournalError(f"Could not create book: {exc}") from exc
        return book

    def ensure_default_book(self) -> JournalBook:
        books = self.list_books()
        if not books.empty:
            return self.get_book(str(books.iloc[0]["book_id"]))
        return self.create_book("Primary consensus paper", mode="paper")

    def list_books(self) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                "SELECT * FROM books ORDER BY created_at_utc, name",
                connection,
            )

    def get_book(self, book_id: str) -> JournalBook:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM books WHERE book_id = ?", (book_id,)
            ).fetchone()
        if row is None:
            raise ForwardJournalError(f"Unknown book: {book_id}")
        return JournalBook(**dict(row))

    def build_alignment(
        self,
        book_id: str,
        state: Mapping[str, object],
        prices: Mapping[str, float],
        *,
        arm_name: str = "consensus",
        minimum_trade_value: float = 1.0,
    ) -> pd.DataFrame:
        if minimum_trade_value < 0.0:
            raise ForwardJournalError("Minimum trade value cannot be negative.")
        arm = _state_arm(state, arm_name)
        target = {
            str(ticker): float(weight)
            for ticker, weight in _mapping(arm.get("target_for_next_open"), "target weights").items()
        }
        if any(weight < 0.0 for weight in target.values()) or sum(target.values()) > 1.000001:
            raise ForwardJournalError("State target is not a funded long-only allocation.")
        clean_prices = _clean_prices(prices)
        snapshot = self.book_snapshot(book_id, clean_prices)
        position_rows = (
            snapshot.positions.set_index("ticker").to_dict("index")
            if not snapshot.positions.empty
            else {}
        )
        reason_by_ticker = {
            str(row.get("ticker")): row.get("reasons", [])
            for row in _records(arm.get("decisions"), "arm decisions")
        }
        rows: list[dict[str, object]] = []
        for ticker in sorted(set(target) | set(position_rows)):
            if ticker not in clean_prices:
                raise ForwardJournalError(f"No current mark is available for held/target ticker {ticker}.")
            price = clean_prices[ticker]
            current = position_rows.get(ticker, {})
            current_value = float(current.get("market_value", 0.0))
            current_weight = current_value / snapshot.total_equity if snapshot.total_equity else 0.0
            target_weight = float(target.get(ticker, 0.0))
            target_value = snapshot.total_equity * target_weight
            delta_value = target_value - current_value
            if abs(delta_value) < minimum_trade_value:
                side = "ALIGNED"
                planned_quantity = 0.0
            elif delta_value > 0.0:
                side = "BUY"
                planned_quantity = delta_value / price
            else:
                side = "SELL"
                planned_quantity = min(
                    abs(delta_value) / price,
                    float(current.get("shares", 0.0)),
                )
            rows.append(
                {
                    "ticker": ticker,
                    "side": side,
                    "current_weight": current_weight,
                    "target_weight": target_weight,
                    "delta_weight": target_weight - current_weight,
                    "current_value": current_value,
                    "target_value": target_value,
                    "delta_value": delta_value,
                    "reference_price": price,
                    "planned_quantity": planned_quantity,
                    "reasons": reason_by_ticker.get(ticker, ["book_alignment_to_model_target"]),
                }
            )
        return pd.DataFrame(rows)

    def lock_alignment(
        self,
        book_id: str,
        state: Mapping[str, object],
        alignment: pd.DataFrame,
        *,
        arm_name: str = "consensus",
        notes: str = "",
        now: datetime | None = None,
    ) -> pd.DataFrame:
        self.get_book(book_id)
        required = {
            "ticker",
            "side",
            "target_weight",
            "current_weight",
            "delta_value",
            "reference_price",
            "planned_quantity",
            "reasons",
        }
        if not required.issubset(alignment.columns):
            raise ForwardJournalError(f"Alignment is missing columns: {sorted(required - set(alignment))}")
        record_hash = _required_text(state, "record_sha256")
        lineage = _required_text(state, "lineage_id")
        policy_hash = _required_text(state, "stock_policy_sha256")
        as_of = _required_text(state, "as_of_session")
        locked_at = _utc_text(now)
        actionable = alignment.loc[alignment["side"].isin(["BUY", "SELL"])].copy()
        with self._connect() as connection:
            for row in actionable.to_dict("records"):
                ticker = str(row["ticker"])
                side = str(row["side"])
                ticket_id = _stable_id(
                    "ticket",
                    book_id,
                    record_hash,
                    arm_name,
                    ticker,
                    side,
                )
                payload = (
                    ticket_id,
                    book_id,
                    locked_at,
                    as_of,
                    "next_regular_session_open",
                    lineage,
                    arm_name,
                    policy_hash,
                    record_hash,
                    ticker,
                    side,
                    float(row["target_weight"]),
                    float(row["current_weight"]),
                    float(row["delta_value"]),
                    float(row["reference_price"]),
                    float(row["planned_quantity"]),
                    json.dumps(_json_list(row["reasons"]), sort_keys=True),
                    "OPEN",
                    notes.strip(),
                )
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO recommendation_tickets(
                        ticket_id, book_id, locked_at_utc, as_of_session, effective_at,
                        lineage_id, arm_name, stock_policy_sha256, state_record_sha256,
                        ticker, side, target_weight, current_weight, delta_value,
                        reference_price, planned_quantity, reasons_json, status, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload,
                )
                if cursor.rowcount == 1:
                    self._append_event(
                        connection,
                        book_id,
                        "RECOMMENDATION_LOCKED",
                        ticket_id,
                        {"ticker": ticker, "side": side, "state_record_sha256": record_hash},
                    )
        return self.load_tickets(book_id, state_record_sha256=record_hash)

    def load_tickets(
        self,
        book_id: str,
        *,
        statuses: tuple[str, ...] | None = None,
        state_record_sha256: str | None = None,
    ) -> pd.DataFrame:
        query = "SELECT * FROM recommendation_tickets WHERE book_id = ?"
        parameters: list[object] = [book_id]
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" AND status IN ({placeholders})"
            parameters.extend(statuses)
        if state_record_sha256:
            query += " AND state_record_sha256 = ?"
            parameters.append(state_record_sha256)
        query += " ORDER BY locked_at_utc DESC, ticker"
        with self._connect() as connection:
            frame = pd.read_sql_query(query, connection, params=parameters)
        if "reasons_json" in frame:
            frame["reasons"] = frame["reasons_json"].map(json.loads)
        return frame

    def set_ticket_status(
        self,
        ticket_id: str,
        status: str,
        *,
        notes: str = "",
        now: datetime | None = None,
    ) -> None:
        clean_status = status.strip().upper()
        if clean_status not in {"SKIPPED", "EXPIRED"}:
            raise ForwardJournalError("Only SKIPPED or EXPIRED may be set manually.")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT book_id, status FROM recommendation_tickets WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
            if row is None:
                raise ForwardJournalError(f"Unknown ticket: {ticket_id}")
            if row["status"] in {"EXECUTED", "SKIPPED", "EXPIRED"}:
                raise ForwardJournalError(f"Ticket is already terminal: {row['status']}")
            connection.execute(
                "UPDATE recommendation_tickets SET status = ?, notes = ? WHERE ticket_id = ?",
                (clean_status, notes.strip(), ticket_id),
            )
            self._append_event(
                connection,
                str(row["book_id"]),
                f"TICKET_{clean_status}",
                ticket_id,
                {"notes": notes.strip(), "recorded_at_utc": _utc_text(now)},
            )

    def record_execution(
        self,
        book_id: str,
        *,
        ticker: str,
        side: str,
        quantity: float,
        price: float,
        fees: float = 0.0,
        ticket_id: str | None = None,
        notes: str = "",
        executed_at: datetime | None = None,
    ) -> str:
        clean_ticker = ticker.strip().upper()
        clean_side = side.strip().upper()
        if not clean_ticker:
            raise ForwardJournalError("Ticker is required.")
        if clean_side not in {"BUY", "SELL"}:
            raise ForwardJournalError("Execution side must be BUY or SELL.")
        if not np.isfinite(quantity) or quantity <= 0.0:
            raise ForwardJournalError("Quantity must be positive and finite.")
        if not np.isfinite(price) or price <= 0.0:
            raise ForwardJournalError("Execution price must be positive and finite.")
        if not np.isfinite(fees) or fees < 0.0:
            raise ForwardJournalError("Fees must be non-negative and finite.")
        execution_id = f"execution-{uuid4().hex[:20]}"
        with self._connect() as connection:
            book = self._book_from_connection(connection, book_id)
            ticket = None
            if ticket_id:
                ticket = connection.execute(
                    "SELECT * FROM recommendation_tickets WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()
                if ticket is None or str(ticket["book_id"]) != book_id:
                    raise ForwardJournalError("Ticket does not belong to the selected book.")
                if str(ticket["status"]) not in {"OPEN", "PARTIAL"}:
                    raise ForwardJournalError(f"Ticket is not open: {ticket['status']}")
                if str(ticket["ticker"]) != clean_ticker or str(ticket["side"]) != clean_side:
                    raise ForwardJournalError("Execution ticker/side does not match the ticket.")

            state = self._execution_state(connection, book)
            current_shares = float(state["positions"].get(clean_ticker, {}).get("shares", 0.0))
            cash = float(state["cash"])
            if clean_side == "SELL" and quantity > current_shares + 1e-9:
                raise ForwardJournalError(
                    f"Sell quantity {quantity:g} exceeds held shares {current_shares:g}; shorts are prohibited."
                )
            cash_change = quantity * price + fees if clean_side == "BUY" else 0.0
            if clean_side == "BUY" and cash_change > cash + 1e-6:
                raise ForwardJournalError(
                    f"Execution requires ${cash_change:,.2f}, but the journal book has ${cash:,.2f} cash."
                )

            connection.execute(
                """
                INSERT INTO executions(
                    execution_id, book_id, ticket_id, executed_at_utc, ticker,
                    side, quantity, price, fees, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    execution_id,
                    book_id,
                    ticket_id,
                    _utc_text(executed_at),
                    clean_ticker,
                    clean_side,
                    float(quantity),
                    float(price),
                    float(fees),
                    notes.strip(),
                ),
            )
            if ticket is not None:
                filled = connection.execute(
                    "SELECT COALESCE(SUM(quantity), 0) FROM executions WHERE ticket_id = ?",
                    (ticket_id,),
                ).fetchone()[0]
                status = (
                    "EXECUTED"
                    if float(filled) + 1e-9 >= float(ticket["planned_quantity"])
                    else "PARTIAL"
                )
                connection.execute(
                    "UPDATE recommendation_tickets SET status = ? WHERE ticket_id = ?",
                    (status, ticket_id),
                )
            self._append_event(
                connection,
                book_id,
                "EXECUTION_RECORDED",
                execution_id,
                {
                    "ticker": clean_ticker,
                    "side": clean_side,
                    "quantity": float(quantity),
                    "price": float(price),
                    "fees": float(fees),
                    "ticket_id": ticket_id,
                },
            )
        return execution_id

    def load_executions(self, book_id: str) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT * FROM executions
                WHERE book_id = ?
                ORDER BY executed_at_utc DESC, execution_id DESC
                """,
                connection,
                params=[book_id],
            )

    def book_snapshot(
        self,
        book_id: str,
        prices: Mapping[str, float] | None = None,
    ) -> BookSnapshot:
        with self._connect() as connection:
            book = self._book_from_connection(connection, book_id)
            state = self._execution_state(connection, book)
        marks = _clean_prices(prices or {})
        position_rows: list[dict[str, float | str]] = []
        positions_value = 0.0
        unrealized = 0.0
        for ticker, position in sorted(state["positions"].items()):
            shares = float(position["shares"])
            if shares <= 1e-12:
                continue
            if ticker not in marks:
                raise ForwardJournalError(f"No current mark is available for held ticker {ticker}.")
            market_price = marks[ticker]
            cost_basis = float(position["cost_basis"])
            market_value = shares * market_price
            unrealized_pnl = market_value - cost_basis
            positions_value += market_value
            unrealized += unrealized_pnl
            position_rows.append(
                {
                    "ticker": ticker,
                    "shares": shares,
                    "average_cost": cost_basis / shares,
                    "cost_basis": cost_basis,
                    "mark_price": market_price,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_return": unrealized_pnl / cost_basis if cost_basis else 0.0,
                }
            )
        cash = float(state["cash"])
        total = cash + positions_value
        positions = pd.DataFrame(position_rows)
        if not positions.empty:
            positions["current_weight"] = positions["market_value"] / total if total else 0.0
        return BookSnapshot(
            book=book,
            cash=cash,
            positions_value=positions_value,
            total_equity=total,
            gross_exposure=positions_value / total if total else 0.0,
            realized_pnl=float(state["realized_pnl"]),
            unrealized_pnl=unrealized,
            cumulative_fees=float(state["cumulative_fees"]),
            positions=positions,
        )

    def record_valuation(
        self,
        book_id: str,
        *,
        market_session: str,
        prices: Mapping[str, float],
        spy_close: float,
        source_record_sha256: str,
        source_lineage_id: str = "unknown",
        source_gate_passed: bool = True,
        source_policy_current: bool = True,
        now: datetime | None = None,
    ) -> str:
        if not np.isfinite(spy_close) or spy_close <= 0.0:
            raise ForwardJournalError("SPY close must be positive and finite.")
        snapshot = self.book_snapshot(book_id, prices)
        session = pd.Timestamp(market_session).date().isoformat()
        marks = _clean_prices(prices)
        valuation_id = _stable_id("valuation", book_id, session, source_record_sha256)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT valuation_id, source_record_sha256 FROM valuations WHERE book_id = ? AND market_session = ?",
                (book_id, session),
            ).fetchone()
            if existing is not None:
                if str(existing["source_record_sha256"]) != source_record_sha256:
                    raise ValuationConflictError(
                        "A valuation already exists for this market session from a different source record."
                    )
                return str(existing["valuation_id"])
            connection.execute(
                """
                INSERT INTO valuations(
                    valuation_id, book_id, valued_at_utc, market_session, cash,
                    positions_value, total_equity, gross_exposure, realized_pnl,
                    unrealized_pnl, cumulative_fees, spy_close, marks_json,
                    source_record_sha256, source_lineage_id, source_gate_passed,
                    source_policy_current
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    valuation_id,
                    book_id,
                    _utc_text(now),
                    session,
                    snapshot.cash,
                    snapshot.positions_value,
                    snapshot.total_equity,
                    snapshot.gross_exposure,
                    snapshot.realized_pnl,
                    snapshot.unrealized_pnl,
                    snapshot.cumulative_fees,
                    float(spy_close),
                    json.dumps(marks, sort_keys=True),
                    source_record_sha256,
                    source_lineage_id,
                    int(source_gate_passed),
                    int(source_policy_current),
                ),
            )
            self._append_event(
                connection,
                book_id,
                "VALUATION_RECORDED",
                valuation_id,
                {
                    "market_session": session,
                    "total_equity": snapshot.total_equity,
                    "spy_close": float(spy_close),
                    "source_record_sha256": source_record_sha256,
                    "source_lineage_id": source_lineage_id,
                    "source_gate_passed": source_gate_passed,
                    "source_policy_current": source_policy_current,
                },
            )
        return valuation_id

    def record_all_book_valuations(
        self,
        state: Mapping[str, object],
        prices: Mapping[str, float],
        *,
        source_policy_current: bool,
        now: datetime | None = None,
    ) -> JournalValuationRun:
        books = self.list_books()
        market = _mapping(state.get("market_state"), "market state")
        session = _required_text(state, "as_of_session")
        spy_close = float(market.get("close", 0.0))
        recorded = 0
        for row in books.to_dict("records"):
            self.record_valuation(
                str(row["book_id"]),
                market_session=session,
                prices=prices,
                spy_close=spy_close,
                source_record_sha256=_required_text(state, "record_sha256"),
                source_lineage_id=_required_text(state, "lineage_id"),
                source_gate_passed=bool(
                    state.get("eligible_for_primary_prospective_performance")
                ),
                source_policy_current=source_policy_current,
                now=now,
            )
            recorded += 1
        return JournalValuationRun(session, len(books), recorded)

    def performance_frame(self, book_id: str) -> pd.DataFrame:
        with self._connect() as connection:
            frame = pd.read_sql_query(
                """
                SELECT * FROM valuations
                WHERE book_id = ?
                ORDER BY market_session, valued_at_utc
                """,
                connection,
                params=[book_id],
                parse_dates=["market_session", "valued_at_utc"],
            )
        if frame.empty:
            return frame
        first_equity = float(frame.iloc[0]["total_equity"])
        first_spy = float(frame.iloc[0]["spy_close"])
        frame["book_growth"] = frame["total_equity"] / first_equity
        frame["spy_growth"] = frame["spy_close"] / first_spy
        frame["book_cumulative_return"] = frame["book_growth"] - 1.0
        frame["spy_cumulative_return"] = frame["spy_growth"] - 1.0
        frame["cumulative_excess_return"] = (
            frame["book_cumulative_return"] - frame["spy_cumulative_return"]
        )
        frame["book_return"] = frame["total_equity"].pct_change(fill_method=None).fillna(0.0)
        frame["spy_return"] = frame["spy_close"].pct_change(fill_method=None).fillna(0.0)
        frame["book_drawdown"] = frame["book_growth"] / frame["book_growth"].cummax() - 1.0
        frame["source_gate_passed"] = frame["source_gate_passed"].astype(bool)
        frame["source_policy_current"] = frame["source_policy_current"].astype(bool)
        frame["eligible_model_context"] = (
            frame["source_gate_passed"] & frame["source_policy_current"]
        )
        return frame

    def audit_events(self, book_id: str) -> pd.DataFrame:
        with self._connect() as connection:
            return pd.read_sql_query(
                """
                SELECT * FROM audit_events
                WHERE book_id = ?
                ORDER BY recorded_at_utc DESC, event_id DESC
                """,
                connection,
                params=[book_id],
            )

    def _book_from_connection(
        self,
        connection: sqlite3.Connection,
        book_id: str,
    ) -> JournalBook:
        row = connection.execute("SELECT * FROM books WHERE book_id = ?", (book_id,)).fetchone()
        if row is None:
            raise ForwardJournalError(f"Unknown book: {book_id}")
        return JournalBook(**dict(row))

    def _execution_state(
        self,
        connection: sqlite3.Connection,
        book: JournalBook,
    ) -> dict[str, object]:
        rows = connection.execute(
            """
            SELECT * FROM executions
            WHERE book_id = ?
            ORDER BY executed_at_utc, rowid
            """,
            (book.book_id,),
        ).fetchall()
        cash = book.starting_cash
        positions: dict[str, dict[str, float]] = {}
        realized_pnl = 0.0
        cumulative_fees = 0.0
        for row in rows:
            ticker = str(row["ticker"])
            side = str(row["side"])
            quantity = float(row["quantity"])
            price = float(row["price"])
            fees = float(row["fees"])
            cumulative_fees += fees
            position = positions.setdefault(ticker, {"shares": 0.0, "cost_basis": 0.0})
            if side == "BUY":
                position["shares"] += quantity
                position["cost_basis"] += quantity * price + fees
                cash -= quantity * price + fees
            else:
                shares_before = position["shares"]
                if quantity > shares_before + 1e-9:
                    raise ForwardJournalError(
                        f"Stored journal oversells {ticker}; the database requires reconciliation."
                    )
                average_cost = position["cost_basis"] / shares_before if shares_before else 0.0
                removed_basis = average_cost * quantity
                proceeds = quantity * price - fees
                realized_pnl += proceeds - removed_basis
                position["shares"] -= quantity
                position["cost_basis"] -= removed_basis
                cash += proceeds
                if position["shares"] <= 1e-9:
                    position["shares"] = 0.0
                    position["cost_basis"] = 0.0
        if cash < -1e-6:
            raise ForwardJournalError("Stored journal has negative cash; the database requires reconciliation.")
        return {
            "cash": cash,
            "positions": positions,
            "realized_pnl": realized_pnl,
            "cumulative_fees": cumulative_fees,
        }

    def _append_event(
        self,
        connection: sqlite3.Connection,
        book_id: str,
        event_type: str,
        entity_id: str,
        payload: Mapping[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO audit_events(
                event_id, recorded_at_utc, book_id, event_type, entity_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"event-{uuid4().hex[:20]}",
                _utc_text(),
                book_id,
                event_type,
                entity_id,
                json.dumps(dict(payload), sort_keys=True, default=str),
            ),
        )


def load_state_price_marks(
    root: Path,
    state: Mapping[str, object],
) -> dict[str, float]:
    """Load integrity-checked closing marks bound to an immutable stock state."""
    inputs = _mapping(state.get("inputs"), "state inputs")
    manifest_name = _required_text(inputs, "price_manifest")
    if Path(manifest_name).name != manifest_name:
        raise ForwardJournalError("State price manifest must be a local filename.")
    manifest_path = root / "data/stock-shadow/prices" / manifest_name
    if not manifest_path.exists():
        raise ForwardJournalError(f"Bound price manifest is missing: {manifest_name}")
    if file_sha256(manifest_path) != _required_text(inputs, "price_manifest_sha256"):
        raise ForwardJournalError("State price-manifest hash does not match the bound file.")
    audit = audit_current_stock_price_snapshot(
        manifest_path,
        max_age_hours=float("inf"),
    )
    if not audit.integrity_passed:
        raise ForwardJournalError(
            "Bound price snapshot failed integrity verification: " + "; ".join(audit.errors)
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ForwardJournalError("Bound price manifest must be an object.")
    data_path = manifest_path.parent / str(manifest["data_file"])
    frame = pd.read_parquet(data_path)
    frame.columns = pd.MultiIndex.from_tuples(frame.columns, names=["field", "ticker"])
    session = pd.Timestamp(_required_text(state, "as_of_session"))
    matches = frame.loc[frame.index.normalize() == session.normalize()]
    if matches.empty:
        raise ForwardJournalError(f"Bound price snapshot has no row for {session.date()}.")
    closes = matches.iloc[-1]["Close"]
    marks = {
        str(ticker).upper(): float(value)
        for ticker, value in closes.items()
        if pd.notna(value) and np.isfinite(float(value)) and float(value) > 0.0
    }
    if "SPY" not in marks:
        raise ForwardJournalError("Bound price snapshot has no valid SPY close.")
    return marks


def mark_local_books_from_state(
    root: Path,
    journal_path: Path,
    state_path: Path,
    *,
    source_policy_current: bool,
    now: datetime | None = None,
) -> JournalValuationRun:
    """Mark every local book from the exact snapshot bound to an intact state."""
    if not verify_stock_shadow_state(state_path):
        raise ForwardJournalError("Source stock state failed content-hash verification.")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ForwardJournalError("Source stock state must be an object.")
    marks = load_state_price_marks(root, state)
    return ForwardJournal(journal_path).record_all_book_valuations(
        state,
        marks,
        source_policy_current=source_policy_current,
        now=now,
    )


def _state_arm(state: Mapping[str, object], arm_name: str) -> Mapping[str, object]:
    arms = _mapping(state.get("arms"), "state arms")
    arm = arms.get(arm_name)
    if not isinstance(arm, Mapping):
        raise ForwardJournalError(f"State is missing arm {arm_name}.")
    return arm


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ForwardJournalError(f"{label} must be a mapping.")
    return value


def _records(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or any(not isinstance(row, Mapping) for row in value):
        raise ForwardJournalError(f"{label} must be a list of records.")
    return value


def _required_text(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result.strip():
        raise ForwardJournalError(f"State is missing {key}.")
    return result.strip()


def _clean_prices(prices: Mapping[str, float]) -> dict[str, float]:
    cleaned: dict[str, float] = {}
    for ticker, raw_price in prices.items():
        price = float(raw_price)
        if not np.isfinite(price) or price <= 0.0:
            raise ForwardJournalError(f"Invalid price for {ticker}: {raw_price}")
        cleaned[str(ticker).strip().upper()] = price
    return cleaned


def _json_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _utc_text(value: datetime | None = None) -> str:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat()
