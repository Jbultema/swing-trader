from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import dotenv_values

from swing_trader.audit import audit_operational_artifacts
from swing_trader.dashboard_models import (
    StockDashboardState,
    latest_stock_dashboard_state,
    percentage_points_frame,
    policy_forward_frame,
    prospective_maturity,
    prospective_performance_rows,
    stock_action_frame,
    stock_decision_headline,
    stock_price_map,
    stock_risk_summary,
    stock_target_frame,
)
from swing_trader.forward_journal import (
    ForwardJournal,
    ForwardJournalError,
    load_state_price_marks,
)
from swing_trader.published_momentum import (
    audit_published_momentum_report,
    latest_published_momentum_report,
)
from swing_trader.refresh_jobs import RefreshJobError, RefreshJobStore
from swing_trader.stock_audit import audit_stock_research_bundle

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports/latest"
SHADOW = ROOT / "reports/shadow"
STOCK_REPORTS = ROOT / "reports/stock/latest"
STOCK_LINEAGES = ROOT / "reports/stock-shadow/lineages"
STOCK_SHADOW_CONFIG = ROOT / "config/stock_shadow.toml"
PUBLISHED_REPORTS = ROOT / "reports/published"
PUBLISHED_SOURCES = ROOT / "data/published"
JOURNAL_PATH = ROOT / "data/forward-journal/swing-trader.sqlite"
JOB_CONTROL = ROOT / "data/job-control"
JOB_STORE_PATH = JOB_CONTROL / "refresh-jobs.sqlite"
JOB_LOG_DIR = JOB_CONTROL / "logs"


def _read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    return value if isinstance(value, dict) else {}


def _latest_evaluation(state: StockDashboardState | None) -> dict[str, object] | None:
    if state is None:
        return None
    paths = sorted((state.path.parents[1] / "evaluations").glob("evaluation-*.json"))
    return _read_json(paths[-1]) if paths else None


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if pd.notna(number) else None


def _pct(value: object, *, digits: int = 1) -> str:
    number = _number(value)
    return "Collecting" if number is None else f"{number:.{digits}%}"


def _money(value: object) -> str:
    number = _number(value)
    return "N/A" if number is None else f"${number:,.0f}"


def _calendar_age_label(value: object) -> str:
    try:
        observed = pd.Timestamp(str(value)).date()
    except (TypeError, ValueError):
        return "Unknown age"
    today = pd.Timestamp.now(tz="America/Denver").date()
    days = (today - observed).days
    if days <= 0:
        return "Today"
    return f"{days} calendar day{'s' if days != 1 else ''} old"


def _alpha_configured() -> bool:
    if os.getenv("ALPHA_VANTAGE_API_KEY", "").strip():
        return True
    values = dotenv_values(ROOT / ".env")
    return bool(str(values.get("ALPHA_VANTAGE_API_KEY") or "").strip())


def _queue_refresh(job_type: str) -> None:
    store = RefreshJobStore(JOB_STORE_PATH, log_dir=JOB_LOG_DIR, root=ROOT)
    try:
        job = store.queue(job_type)
    except RefreshJobError as exc:
        st.error(str(exc))
        return
    st.session_state["swing_refresh_watch_job_id"] = job.job_id
    st.session_state.pop("swing_refresh_reloaded_job_id", None)
    if job.reused:
        st.info(f"Already watching {job.label}: {job.job_id}")
    else:
        st.success(f"Queued {job.label}: {job.job_id}")
    st.rerun()


@st.fragment(run_every="3s")
def _render_job_status_fragment() -> None:
    store = RefreshJobStore(JOB_STORE_PATH, log_dir=JOB_LOG_DIR, root=ROOT)
    jobs = store.list_jobs(limit=8)
    if jobs.empty:
        st.caption("No local refresh jobs have run yet.")
        return
    latest = jobs.iloc[0]
    status = str(latest["status"])
    label = str(latest["label"])
    if status in {"queued", "running"}:
        st.info(f"{label}: {status.upper()}")
    elif status == "completed":
        st.success(f"{label}: COMPLETED")
    else:
        message = str(latest.get("error_message") or "Review the job log.")
        st.error(f"{label}: FAILED · {message}")

    watch_id = st.session_state.get("swing_refresh_watch_job_id")
    if watch_id:
        try:
            watched = store.get(str(watch_id))
        except RefreshJobError:
            watched = None
        if (
            watched is not None
            and watched.status in {"completed", "failed"}
            and st.session_state.get("swing_refresh_reloaded_job_id") != watched.job_id
        ):
            st.session_state["swing_refresh_reloaded_job_id"] = watched.job_id
            st.rerun(scope="app")

    display = jobs[
        ["label", "status", "created_at_utc", "completed_at_utc", "return_code"]
    ].copy()
    display.columns = ["Job", "Status", "Queued (UTC)", "Finished (UTC)", "Code"]
    st.dataframe(display, hide_index=True, width="stretch")
    with st.expander("Latest job log", expanded=status == "failed"):
        tail = store.read_log_tail(str(latest["job_id"]))
        st.code(tail or "The worker has not written log output yet.", language="text")


def _render_refresh_sidebar(state: StockDashboardState | None) -> None:
    st.sidebar.markdown("## Refresh center")
    if state is None:
        st.sidebar.warning("Stock state: not started")
    else:
        as_of = state.payload.get("as_of_session", "Unknown")
        st.sidebar.markdown(f"**Stock signals:** {as_of}")
        st.sidebar.caption(
            f"{_calendar_age_label(as_of)} · "
            f"{'current policy' if state.current_policy else 'policy refresh required'} · "
            f"gate {'passed' if state.payload.get('eligible_for_primary_prospective_performance') else 'failed closed'}"
        )
    decisions = _read_json(REPORTS / "latest_decisions.json")
    etf_as_of = decisions.get("as_of_close", "Not available")
    st.sidebar.markdown(f"**ETF research:** {etf_as_of}")
    st.sidebar.caption(_calendar_age_label(etf_as_of))
    st.sidebar.caption(
        "Alpha Vantage: CONFIGURED" if _alpha_configured() else "Alpha Vantage: NOT CONFIGURED"
    )

    with st.sidebar.expander("Run refresh job", expanded=state is None or not state.current_policy):
        st.caption(
            "Jobs run in the background. A completed job refreshes evidence; it never places or authorizes a trade."
        )
        quota_confirmed = st.checkbox(
            "I understand network refreshes spend free-provider quota",
            key="swing_refresh_quota_confirmed",
        )
        if st.button(
            "Run stock daily refresh",
            type="primary",
            disabled=not quota_confirmed,
            width="stretch",
            help="Refresh current roster, prices, independent checks, decisions, forward evaluation, and local book valuations.",
        ):
            _queue_refresh("stock_daily")
        if st.button(
            "Re-evaluate forward evidence",
            disabled=state is None or not state.current_policy,
            width="stretch",
            help="Recompute outcomes from already locked current-policy states. This does not download market data.",
        ):
            _queue_refresh("stock_evaluate")
        if st.button(
            "Refresh historical ETF research",
            disabled=not quota_confirmed,
            width="stretch",
            help="Refresh the separate long-horizon ETF research bundle and its independent data gate.",
        ):
            _queue_refresh("etf_daily")
        st.warning(
            "Do not run both network refreshes in one provider day. Together they can exceed the free Alpha Vantage request allowance."
        )
    with st.sidebar.expander("Recent jobs", expanded=True):
        _render_job_status_fragment()
    if st.sidebar.button("Reload dashboard from disk", width="stretch"):
        st.cache_data.clear()
        st.rerun()


def _cards(items: list[tuple[str, str, str]]) -> None:
    columns = st.columns(len(items))
    for column, (label, value, detail) in zip(columns, items, strict=True):
        with column:
            st.markdown(
                f"""
                <div class="metric-card">
                  <div class="metric-label">{label}</div>
                  <div class="metric-value">{value}</div>
                  <div class="metric-detail">{detail}</div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def _decision_hero(summary: Mapping[str, object]) -> None:
    tone = str(summary.get("tone", "warning"))
    st.markdown(
        f"""
        <div class="decision-hero {tone}">
          <div class="decision-kicker">TODAY'S DECISION</div>
          <div class="decision-label">{summary.get('label', 'WAIT')}</div>
          <div class="decision-title">{summary.get('headline', '')}</div>
          <div class="decision-detail">{summary.get('detail', '')}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _section(title: str, caption: str = "") -> None:
    st.markdown(f"<h2 class='section-title'>{title}</h2>", unsafe_allow_html=True)
    if caption:
        st.caption(caption)


def _gate_frame(state: StockDashboardState) -> pd.DataFrame:
    payload = state.payload
    price = payload.get("independent_price_validation", {})
    earnings = payload.get("earnings_risk_validation", {})
    shares = payload.get("share_turnover_data_validation", {})
    rows = [
        {
            "Check": "State integrity",
            "Status": "PASS" if state.integrity_passed else "FAIL",
            "Effect": "Invalid state is never actionable",
        },
        {
            "Check": "Current frozen policy",
            "Status": "PASS" if state.current_policy else "STALE",
            "Effect": "Older lineage remains audit-only",
        },
        {
            "Check": "Independent prices",
            "Status": "PASS" if isinstance(price, Mapping) and price.get("passed") else "FAIL",
            "Effect": "Primary performance requires reconciled prices",
        },
        {
            "Check": "Earnings-event risk",
            "Status": "PASS" if isinstance(earnings, Mapping) and earnings.get("passed") else "FAIL",
            "Effect": "Unverified events block affected entries",
        },
        {
            "Check": "Share-turnover feed",
            "Status": "PASS" if isinstance(shares, Mapping) and shares.get("passed") else "FAIL",
            "Effect": "Failure excludes only diagnostic turnover arms",
        },
        {
            "Check": "Primary prospective eligibility",
            "Status": "PASS"
            if payload.get("eligible_for_primary_prospective_performance")
            else "FAIL",
            "Effect": "Failed sessions do not enter primary evidence",
        },
    ]
    return pd.DataFrame(rows)


def _render_today(state: StockDashboardState | None) -> None:
    if state is None:
        _decision_hero(
            {
                "label": "NOT STARTED",
                "headline": "No prospective stock decision has been recorded",
                "detail": "Run swing-trader stock-daily after a completed U.S. session.",
                "tone": "warning",
            }
        )
        return

    payload = state.payload
    summary = stock_decision_headline(state)
    actions = stock_action_frame(payload)
    targets = stock_target_frame(payload)
    risk = stock_risk_summary(payload)
    primary = payload.get("arms", {}).get("consensus", {}) if isinstance(payload.get("arms"), Mapping) else {}
    account = primary.get("paper_account_at_close", {}) if isinstance(primary, Mapping) else {}
    buys = int(summary.get("BUY", 0))
    sells = int(summary.get("SELL", 0))

    _decision_hero(summary)
    _cards(
        [
            ("Signal close", str(payload.get("as_of_session", "Unknown")), "Next-open accounting"),
            ("Market / breadth", f"{risk['market_regime']} / {risk['breadth_regime']}", "Both must permit new risk"),
            ("Next actions", f"{buys} BUY · {sells} SELL", f"{len(targets.loc[targets['Ticker'] != 'CASH'])} target names"),
            ("Target exposure", _pct(1.0 - float(primary.get("target_cash_weight", 1.0))), "Long-only; cash is allowed"),
            ("Policy paper equity", _money(account.get("total_equity")), "Automatic frozen-policy shadow"),
        ]
    )

    view = st.pills(
        "Today view",
        ["Actions", "Why & exits", "Model target", "Evidence status"],
        default="Actions",
        label_visibility="collapsed",
    )
    if view == "Actions":
        _section(
            "Next-open action ledger",
            "Signals use closing information and are never modeled as executable before the next regular-session open.",
        )
        if actions.empty:
            st.info("The policy produced no ticker-level decision rows for this session.")
        else:
            display = percentage_points_frame(
                actions[["Action", "Ticker", "Sector", "Why", "Target weight", "Close", "Effective"]],
                ("Target weight",),
            )
            st.dataframe(
                display,
                hide_index=True,
                width="stretch",
                column_config={
                    "Target weight": st.column_config.NumberColumn(format="%.1f%%"),
                    "Close": st.column_config.NumberColumn(format="$%.2f"),
                },
            )
            st.download_button(
                "Download action ledger",
                actions.to_csv(index=False),
                file_name=f"swing-actions-{payload.get('as_of_session', 'latest')}.csv",
                mime="text/csv",
            )
        if payload.get("initialization"):
            st.info(
                "This state initializes a new policy lineage from cash. The displayed BUY rows are a frozen paper target, not executed trades."
            )

    elif view == "Why & exits":
        _section(
            "Why each action exists",
            "Entries need ranked momentum agreement. Exits are evaluated every session and do not wait for the next ranking cycle.",
        )
        if actions.empty:
            st.info("No action explanations are available for this state.")
        else:
            for row in actions.to_dict("records"):
                title = f"{row['Action']} {row['Ticker']} · target {_pct(row['Target weight'])}"
                with st.expander(title, expanded=row["Action"] in {"BUY", "SELL"}):
                    st.write(str(row["Why"]))
                    facts = pd.DataFrame(
                        [
                            {"Measure": "Close", "Value": _money(row.get("Close"))},
                            {"Measure": "21-session return", "Value": _pct(row.get("21-session return"))},
                            {"Measure": "63-session return", "Value": _pct(row.get("63-session return"))},
                            {"Measure": "Distance from 52-week high", "Value": _pct(row.get("Distance from 52-week high"))},
                            {"Measure": "Holding sessions", "Value": row.get("Holding sessions") or "New"},
                            {"Measure": "Hard-loss exit", "Value": _money(row.get("Hard-loss exit"))},
                            {"Measure": "ATR trailing exit", "Value": _money(row.get("ATR trailing exit"))},
                        ]
                    )
                    st.dataframe(facts, hide_index=True, width="stretch")
            st.warning(
                "Exit levels respond after observed closes. They cannot prevent overnight gaps or guarantee an execution price."
            )

    elif view == "Model target":
        _section("Target portfolio", "The next-open target includes explicit cash and never uses leverage, shorts, options, or derivatives.")
        figure = px.bar(
            targets.sort_values("Target weight"),
            x="Target weight",
            y="Ticker",
            orientation="h",
            color="Ticker",
        )
        figure.update_layout(showlegend=False, height=max(360, len(targets) * 30))
        st.plotly_chart(figure, width="stretch")
        st.dataframe(
            percentage_points_frame(targets, ("Target weight",)),
            hide_index=True,
            width="stretch",
            column_config={"Target weight": st.column_config.NumberColumn(format="%.1f%%")},
        )

    else:
        _section("Decision evidence status", "Readable gates replace the former raw JSON wall. Exact records remain available under Research & Evidence.")
        st.dataframe(_gate_frame(state), hide_index=True, width="stretch")
        st.caption(
            f"Lineage {payload.get('lineage_id', 'unknown')} · state {state.path.name} · "
            f"record {str(payload.get('record_sha256', 'unknown'))[:12]}…"
        )


def _render_policy_forward(state: StockDashboardState | None) -> None:
    evaluation = _latest_evaluation(state)
    maturity = prospective_maturity(evaluation)
    _section(
        "Frozen-policy forward test",
        "This is the automatic paper shadow created by the daily decision engine. Historical backtests never enter these results.",
    )
    _cards(
        [
            ("Eligible sessions", f"{maturity['sessions']} / {maturity['required_sessions']}", "Preregistered minimum"),
            ("Completed exits", f"{maturity['completed_exits']} / {maturity['required_exits']}", "Tests get-out behavior"),
            ("Risk-off sessions", f"{maturity['risk_off_sessions']} / {maturity['required_risk_off_sessions']}", "Tests capital defense"),
        ]
    )
    st.write(str(maturity["message"]))
    milestones = [
        ("Eligible sessions", float(maturity.get("session_progress", 0.0))),
        ("Completed exits", float(maturity.get("exit_progress", 0.0))),
        ("Risk-off sessions", float(maturity.get("risk_off_progress", 0.0))),
    ]
    for label, value in milestones:
        st.caption(label)
        st.progress(value)

    if state is None:
        st.info("No policy lineage exists yet.")
        return
    lineage = str(state.payload.get("lineage_id", ""))
    forward = policy_forward_frame(list(state.path.parents[1].glob("states/state-*.json")), lineage_id=lineage)
    if len(forward) < 2:
        st.info(
            "The lineage currently has only its initialization close. Performance begins after the first independent state transition; no NULL metrics are shown."
        )
    else:
        chart = forward[["date", "Policy", "SPY"]].melt("date", var_name="Series", value_name="Growth of $1")
        st.plotly_chart(px.line(chart, x="date", y="Growth of $1", color="Series"), width="stretch")
        recovery_sessions = int(forward["recovery"].sum())
        if recovery_sessions:
            st.warning(
                f"{recovery_sessions} missed closes are visible only to preserve paper-accounting continuity. "
                "They generated no backdated signals and are excluded from eligible prospective evidence."
            )
        latest = forward.iloc[-1]
        _cards(
            [
                ("Policy return", _pct(latest["Policy return"]), "Net of modeled costs"),
                ("SPY return", _pct(latest["SPY return"]), "Same-session benchmark"),
                ("Excess return", _pct(latest["Excess return"]), "Policy minus SPY"),
                ("Max drawdown", _pct(forward["Policy drawdown"].min()), "Observed forward sequence"),
            ]
        )
    performance = prospective_performance_rows(evaluation)
    if not performance.empty:
        st.dataframe(performance, hide_index=True, width="stretch")


def _render_book_history(journal: ForwardJournal, book_id: str) -> None:
    performance = journal.performance_frame(book_id)
    if performance.empty:
        st.info("No close valuation has been recorded for this book yet.")
        return
    if len(performance) >= 2:
        chart = performance[["market_session", "book_growth", "spy_growth"]].rename(
            columns={"book_growth": "Execution book", "spy_growth": "SPY"}
        ).melt("market_session", var_name="Series", value_name="Growth of $1")
        st.plotly_chart(px.line(chart, x="market_session", y="Growth of $1", color="Series"), width="stretch")
    latest = performance.iloc[-1]
    _cards(
        [
            ("Book return", _pct(latest["book_cumulative_return"]), "Recorded close marks"),
            ("SPY return", _pct(latest["spy_cumulative_return"]), "Same valuation sessions"),
            ("Excess", _pct(latest["cumulative_excess_return"]), "Book minus SPY"),
            ("Drawdown", _pct(performance["book_drawdown"].min()), "Observed book peak-to-trough"),
        ]
    )


def _render_execution_book(state: StockDashboardState | None) -> None:
    _section(
        "My paper / live execution book",
        "Recommendation tickets, human executions, close valuations, and deviations are stored locally. Live mode is recordkeeping only—there is no broker connection.",
    )
    journal = ForwardJournal(JOURNAL_PATH)
    books = journal.list_books()
    if books.empty:
        journal.ensure_default_book()
        books = journal.list_books()

    with st.expander("Create another tracked book"), st.form("create-book"):
        name = st.text_input("Book name", value="Secondary paper")
        mode = st.selectbox("Mode", ["paper", "live"])
        account = st.text_input("Account label", value="retirement")
        starting_cash = st.number_input(
            "Starting cash", min_value=1.0, value=100_000.0, step=1_000.0
        )
        if st.form_submit_button("Create book"):
            try:
                journal.create_book(
                    name,
                    mode=mode,
                    account_label=account,
                    starting_cash=starting_cash,
                )
                st.success("Book created.")
                st.rerun()
            except ForwardJournalError as exc:
                st.error(str(exc))

    labels = {
        str(row["book_id"]): f"{row['name']} · {str(row['mode']).upper()} · {row['account_label']}"
        for row in books.to_dict("records")
    }
    book_id = st.selectbox("Tracked book", list(labels), format_func=labels.get)
    book = journal.get_book(book_id)
    prices: dict[str, float] = {}
    if state is not None:
        try:
            prices = load_state_price_marks(ROOT, state.payload)
        except ForwardJournalError as exc:
            prices = stock_price_map(state.payload)
            st.warning(
                "The full bound price panel could not be loaded. Model-name marks remain visible, "
                f"but divergent book positions may be unmarkable: {exc}"
            )
    try:
        snapshot = journal.book_snapshot(book_id, prices)
    except ForwardJournalError as exc:
        snapshot = None
        st.error(f"The book cannot be marked from the current state: {exc}")

    if snapshot is not None:
        _cards(
            [
                ("Book", book.name, f"{book.mode.upper()} · {book.account_label}"),
                ("Equity", _money(snapshot.total_equity), f"Cash {_money(snapshot.cash)}"),
                ("Exposure", _pct(snapshot.gross_exposure), f"{len(snapshot.positions)} held names"),
                ("Realized P&L", _money(snapshot.realized_pnl), f"Fees {_money(snapshot.cumulative_fees)}"),
                ("Unrealized P&L", _money(snapshot.unrealized_pnl), "Current state marks"),
            ]
        )

    if state is None or snapshot is None:
        st.info("A valid current stock state is required to align this book to the model.")
        _render_book_history(journal, book_id)
        return

    payload = state.payload
    try:
        alignment = journal.build_alignment(book_id, payload, prices, minimum_trade_value=1.0)
    except ForwardJournalError as exc:
        st.error(str(exc))
        return

    _section("Book-to-model alignment", "Only differences between the human execution book and the frozen model target become tickets.")
    st.dataframe(
        percentage_points_frame(
            alignment,
            ("current_weight", "target_weight", "delta_weight"),
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "current_weight": st.column_config.NumberColumn("Current", format="%.1f%%"),
            "target_weight": st.column_config.NumberColumn("Target", format="%.1f%%"),
            "delta_weight": st.column_config.NumberColumn("Difference", format="%+.1f%%"),
            "delta_value": st.column_config.NumberColumn("Trade value", format="$%,.2f"),
            "reference_price": st.column_config.NumberColumn("Reference close", format="$%.2f"),
            "planned_quantity": st.column_config.NumberColumn("Planned shares", format="%.4f"),
        },
    )
    lock_allowed = bool(
        state.current_policy
        and payload.get("eligible_for_primary_prospective_performance")
        and state.integrity_passed
    )
    if st.button(
        "Lock current recommendation tickets",
        type="primary",
        disabled=not lock_allowed,
        help=None if lock_allowed else "Requires an intact current-policy state that passed its original primary data gate.",
    ):
        journal.lock_alignment(book_id, payload, alignment)
        st.success("Current model differences were locked as immutable recommendation tickets.")
        st.rerun()

    tickets = journal.load_tickets(book_id)
    if tickets.empty:
        st.info("No recommendation tickets are locked for this book yet.")
    else:
        ticket_display = percentage_points_frame(
            tickets[
                ["status", "as_of_session", "ticker", "side", "planned_quantity", "reference_price", "target_weight", "notes"]
            ],
            ("target_weight",),
        )
        st.dataframe(
            ticket_display,
            hide_index=True,
            width="stretch",
            column_config={
                "planned_quantity": st.column_config.NumberColumn(format="%.4f"),
                "reference_price": st.column_config.NumberColumn(format="$%.2f"),
                "target_weight": st.column_config.NumberColumn(format="%.1f%%"),
            },
        )

    open_tickets = journal.load_tickets(book_id, statuses=("OPEN", "PARTIAL"))
    if not open_tickets.empty:
        _section("Record what actually happened", "Recording an execution changes only the local tracking book. It does not place an order.")
        open_labels = {
            str(row["ticket_id"]): f"{row['side']} {row['ticker']} · planned {float(row['planned_quantity']):.4f}"
            for row in open_tickets.to_dict("records")
        }
        with st.form("record-execution"):
            ticket_id = st.selectbox("Recommendation ticket", list(open_labels), format_func=open_labels.get)
            ticket = open_tickets.loc[open_tickets["ticket_id"] == ticket_id].iloc[0]
            quantity = st.number_input("Executed shares", min_value=0.0001, value=float(ticket["planned_quantity"]), format="%.4f")
            price = st.number_input("Execution price", min_value=0.01, value=float(ticket["reference_price"]), format="%.2f")
            fees = st.number_input("Fees", min_value=0.0, value=0.0, format="%.2f")
            notes = st.text_input("Execution notes")
            if st.form_submit_button("Record execution"):
                try:
                    journal.record_execution(
                        book_id,
                        ticker=str(ticket["ticker"]),
                        side=str(ticket["side"]),
                        quantity=quantity,
                        price=price,
                        fees=fees,
                        ticket_id=ticket_id,
                        notes=notes,
                    )
                    st.success("Execution recorded in the local audit trail.")
                    st.rerun()
                except ForwardJournalError as exc:
                    st.error(str(exc))
        with st.form("skip-ticket"):
            skip_id = st.selectbox("Ticket not executed", list(open_labels), format_func=open_labels.get)
            skip_notes = st.text_input("Reason not executed")
            if st.form_submit_button("Mark ticket skipped"):
                try:
                    journal.set_ticket_status(skip_id, "SKIPPED", notes=skip_notes)
                    st.success("Ticket marked skipped; the deviation remains visible in the audit trail.")
                    st.rerun()
                except ForwardJournalError as exc:
                    st.error(str(exc))

    if st.button("Record this session's close valuation"):
        risk = stock_risk_summary(payload)
        try:
            journal.record_valuation(
                book_id,
                market_session=str(payload["as_of_session"]),
                prices=prices,
                spy_close=float(risk["spy_close"]),
                source_record_sha256=str(payload["record_sha256"]),
                source_lineage_id=str(payload["lineage_id"]),
                source_gate_passed=bool(payload.get("eligible_for_primary_prospective_performance")),
                source_policy_current=state.current_policy,
            )
            st.success("Close valuation recorded idempotently with its source state hash.")
            st.rerun()
        except ForwardJournalError as exc:
            st.error(str(exc))

    _section("Recorded performance", "Only explicitly recorded valuations enter this execution-book chart.")
    _render_book_history(journal, book_id)
    if snapshot is not None and not snapshot.positions.empty:
        _section("Open positions")
        st.dataframe(snapshot.positions, hide_index=True, width="stretch")
    executions = journal.load_executions(book_id)
    if not executions.empty:
        with st.expander("Execution history"):
            st.dataframe(executions, hide_index=True, width="stretch")
    events = journal.audit_events(book_id)
    if not events.empty:
        with st.expander("Immutable audit trail"):
            st.dataframe(events, hide_index=True, width="stretch")


def _render_forward(state: StockDashboardState | None) -> None:
    view = st.pills(
        "Forward tracking view",
        ["Policy shadow", "My execution book"],
        default="Policy shadow",
        label_visibility="collapsed",
    )
    if view == "My execution book":
        _render_execution_book(state)
    else:
        _render_policy_forward(state)


def _render_historical_etf() -> None:
    audit = audit_operational_artifacts(REPORTS, SHADOW)
    metrics = pd.read_csv(REPORTS / "metrics.csv")
    equity = pd.read_csv(REPORTS / "equity.csv", parse_dates=["date"])
    full = metrics.loc[metrics["split"] == "full"].copy()
    strategy = "classic_12m_dual_momentum"
    selected = full.loc[full["strategy"] == strategy]
    _section(
        "Historical ETF reference",
        "Retrospective long-horizon evidence is shown as context, never combined with the stock forward record.",
    )
    if not audit.integrity_passed:
        st.warning(
            "This intact historical snapshot was produced by an older package implementation. Refresh it before using it as the current research readout."
        )
    if not selected.empty:
        row = selected.iloc[0]
        _cards(
            [
                ("CAGR", _pct(row["cagr"]), "Historical full sample"),
                ("Max drawdown", _pct(row["max_drawdown"]), "Peak-to-trough"),
                ("Sharpe", f"{float(row['sharpe_zero_rf']):.2f}", "Zero cash benchmark"),
                ("Turnover", f"{float(row['annualized_turnover']):.1f}×", "Annualized"),
            ]
        )
    normalized = equity.set_index("date").div(equity.set_index("date").iloc[0]).reset_index()
    chart = normalized.melt("date", var_name="Strategy", value_name="Growth of $1")
    st.plotly_chart(px.line(chart, x="date", y="Growth of $1", color="Strategy", log_y=True), width="stretch")
    st.dataframe(
        full[["strategy", "cagr", "annualized_volatility", "sharpe_zero_rf", "max_drawdown", "calmar"]],
        hide_index=True,
        width="stretch",
    )


def _render_published_control() -> None:
    _section(
        "Published momentum control",
        "Official open Fama–French large-stock portfolios test whether momentum exists in aggregate data; they are not this bot's backtest.",
    )
    report = latest_published_momentum_report(PUBLISHED_REPORTS)
    if report is None:
        st.info("No published comparator has been generated yet.")
        return
    audit = audit_published_momentum_report(report, source_dir=PUBLISHED_SOURCES)
    if not audit.integrity_passed:
        st.error("The published comparator failed integrity verification.")
        return
    if not audit.current_implementation_matches or not audit.source_fresh:
        st.warning("The comparator is intact but should be refreshed for current-code/fresh-source status.")
    else:
        st.success("Source archives, report artifacts, implementation, and freshness passed.")
    spreads_path = report / "gross_characteristic_spreads.csv"
    if not spreads_path.exists():
        st.info("This older intact report predates characteristic-spread output.")
        return
    spreads = pd.read_csv(spreads_path)
    display = spreads.loc[
        spreads["period"].isin(["since_2020", "since_2022"])
        & spreads["characteristic"].isin(["largest_quintile_short_term_1_0", "largest_quintile_momentum_12_2"])
    ].copy()
    display["Window"] = display["period"].map({"since_2020": "Since 2020", "since_2022": "Since 2022"})
    display["Signal"] = display["characteristic"].map(
        {"largest_quintile_short_term_1_0": "Prior month (1–0)", "largest_quintile_momentum_12_2": "Intermediate momentum (12–2)"}
    )
    display["Winner minus loser"] = display["annualized_arithmetic_winner_minus_loser"]
    display["Newey–West t-stat"] = display["newey_west_t_statistic"]
    st.dataframe(
        percentage_points_frame(
            display[["Window", "Signal", "Winner minus loser", "Newey–West t-stat", "gross_direction"]],
            ("Winner minus loser",),
        ),
        hide_index=True,
        width="stretch",
        column_config={"Winner minus loser": st.column_config.NumberColumn(format="%.1f%%")},
    )
    st.caption("Gross aggregate CRSP portfolios have unknown turnover and no execution costs or swing-trader exits.")


def _render_retrospective_stocks() -> None:
    _section(
        "Retrospective individual-stock research",
        "Survivorship-safe evidence is required before any historical stock result is displayed.",
    )
    manifest_path = STOCK_REPORTS / "manifest.json"
    if not manifest_path.exists():
        st.info(
            "No survivor-free stock research bundle exists. The free-data historical panel failed its coverage gate, so misleading results remain hidden while the prospective shadow accumulates."
        )
        return
    audit = audit_stock_research_bundle(STOCK_REPORTS)
    if not audit.integrity_passed:
        st.error("The stock research bundle failed integrity/current-code verification and is hidden.")
        return
    metrics = pd.read_csv(STOCK_REPORTS / "metrics.csv")
    st.dataframe(metrics, hide_index=True, width="stretch")


def _render_performance(state: StockDashboardState | None) -> None:
    view = st.pills(
        "Performance evidence",
        ["Forward stock policy", "Historical ETF reference", "Published momentum control", "Retrospective stocks"],
        default="Forward stock policy",
        label_visibility="collapsed",
    )
    if view == "Historical ETF reference":
        _render_historical_etf()
    elif view == "Published momentum control":
        _render_published_control()
    elif view == "Retrospective stocks":
        _render_retrospective_stocks()
    else:
        _render_policy_forward(state)


def _render_risk(state: StockDashboardState | None) -> None:
    if state is None:
        st.info("No prospective stock state exists yet.")
        return
    payload = state.payload
    risk = stock_risk_summary(payload)
    spy = _number(risk["spy_close"])
    average = _number(risk["spy_200d_average"])
    vol = _number(risk["spy_realized_volatility"])
    vol_limit = _number(risk["volatility_limit"])
    trend_margin = spy / average - 1.0 if spy is not None and average else None
    vol_headroom = vol_limit - vol if vol is not None and vol_limit is not None else None
    _section("Risk command center", "The system can move toward cash; it never shorts, hedges with derivatives, or assumes stops eliminate gap risk.")
    _cards(
        [
            ("SPY regime", str(risk["market_regime"]), f"200-day margin {_pct(trend_margin)}"),
            ("Realized volatility", _pct(vol), f"Limit {_pct(vol_limit)}"),
            ("Volatility headroom", _pct(vol_headroom), "Negative means risk-off"),
            ("Breadth regime", str(risk["breadth_regime"]), f"Mean advancing {_pct(risk['mean_advancing_fraction'])}"),
            ("Policy exposure", _pct(risk["gross_exposure"]), f"Cash {_pct(risk['cash_weight'])}"),
        ]
    )
    _section("Gate map")
    st.dataframe(_gate_frame(state), hide_index=True, width="stretch")
    actions = stock_action_frame(payload)
    exits = actions.loc[
        actions["Action"].isin(["HOLD", "SELL"]),
        ["Action", "Ticker", "Why", "Close", "Entry price", "Hard-loss exit", "ATR trailing exit", "Holding sessions"],
    ]
    _section("Position exit map", "Fixed loss, volatility trail, trend/rank decay, market regime, earnings blackout, and maximum hold are evaluated without discretion.")
    if exits.empty:
        st.info("No held or sell position is present in the current state.")
    else:
        st.dataframe(exits, hide_index=True, width="stretch")

    regimes_path = REPORTS / "regime_metrics.csv"
    if regimes_path.exists():
        with st.expander("Historical hostile-regime reference"):
            regimes = pd.read_csv(regimes_path)
            display = regimes.rename(
                columns={
                    "strategy": "Strategy",
                    "regime": "Regime",
                    "start": "Start",
                    "end": "End",
                    "total_return": "Return",
                    "annualized_volatility": "Volatility",
                    "max_drawdown": "Max drawdown",
                    "worst_day": "Worst day",
                }
            )
            st.dataframe(display, hide_index=True, width="stretch")
    st.warning("Exit rules can reduce modeled exposure after observed damage; overnight gaps and actual fills remain uncontrolled risks.")


def _render_research(state: StockDashboardState | None) -> None:
    _section("Evidence ladder", "Each evidence type answers a different question. The dashboard keeps them separate to prevent retrospective results from masquerading as live validation.")
    evaluation = _latest_evaluation(state)
    maturity = prospective_maturity(evaluation)
    ladder = pd.DataFrame(
        [
            {"Evidence": "Published characteristic control", "Question": "Does momentum exist in official aggregate portfolios?", "Status": "Available", "Trading authority": "None"},
            {"Evidence": "Historical ETF simulation", "Question": "Did the long-horizon reference survive costs and hostile regimes?", "Status": "Available; refresh current-code audit", "Trading authority": "None"},
            {"Evidence": "Survivor-free stock backtest", "Question": "Did this exact stock policy work historically without selection bias?", "Status": "Blocked by free-data coverage", "Trading authority": "None"},
            {"Evidence": "Frozen stock policy shadow", "Question": "What happened after decisions were recorded?", "Status": f"{maturity['sessions']} eligible sessions", "Trading authority": "None"},
            {"Evidence": "Human execution book", "Question": "What did the user actually execute and earn?", "Status": "Local paper/live journal", "Trading authority": "Human only"},
        ]
    )
    st.dataframe(ladder, hide_index=True, width="stretch")

    _section("Open-data boundary")
    st.dataframe(
        pd.DataFrame(
            [
                {"Need": "Daily price and volume", "Open source": "Yahoo Finance", "Control": "Independent Alpha Vantage reconciliation"},
                {"Need": "Current S&P 500 roster", "Open source": "Wikipedia capture", "Control": "Point-in-time snapshots; no historical backfill claim"},
                {"Need": "Shares outstanding", "Open source": "SEC company facts", "Control": "Captured prospectively; exact turnover arm is diagnostic"},
                {"Need": "Corporate filing events", "Open source": "SEC EDGAR", "Control": "Event metadata only; no sentiment inference"},
                {"Need": "Published momentum priors", "Open source": "Ken French Data Library", "Control": "Comparator only; no execution equivalence"},
            ]
        ),
        hide_index=True,
        width="stretch",
    )

    if state is not None:
        arms = state.payload.get("arms", {})
        if isinstance(arms, Mapping):
            rows = []
            for name, value in arms.items():
                if not isinstance(value, Mapping):
                    continue
                rows.append(
                    {
                        "Arm": name,
                        "Role": value.get("prospective_role"),
                        "Signal family": value.get("signal_family"),
                        "Entry rank": value.get("entry_rank_threshold"),
                        "Rank exit enabled": value.get("rank_exit_enabled"),
                        "Hold through rank": value.get("hold_rank_threshold"),
                        "Maximum hold": value.get("maximum_holding_sessions"),
                        "Signal available": value.get("signal_data_available_at_close"),
                        "Primary eligible": value.get("eligible_for_primary_prospective_performance"),
                    }
                )
            with st.expander("Frozen experimental arms"):
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    with st.expander("Download exact machine-readable evidence"):
        st.caption("Raw records are retained for audit without making JSON the primary user experience.")
        if state is not None:
            st.download_button(
                "Download current stock state JSON",
                json.dumps(state.payload, indent=2, sort_keys=True),
                file_name=state.path.name,
                mime="application/json",
            )
        if evaluation is not None:
            st.download_button(
                "Download forward evaluation JSON",
                json.dumps(evaluation, indent=2, sort_keys=True),
                file_name="stock-forward-evaluation.json",
                mime="application/json",
            )
        for path in (REPORTS / "data_quality.json", REPORTS / "validation_summary.json", REPORTS / "manifest.json"):
            if path.exists():
                st.download_button(
                    f"Download {path.name}",
                    path.read_text(encoding="utf-8"),
                    file_name=path.name,
                    mime="application/json",
                )


st.set_page_config(page_title="Swing Trader V2", page_icon="↗", layout="wide")
st.markdown(
    """
    <style>
      .block-container {max-width: 1480px; padding-top: 1.6rem; padding-bottom: 4rem;}
      .masthead {background: linear-gradient(120deg,#0b3328,#145a45); color:white; border-radius:18px; padding:24px 30px; margin-bottom:18px; box-shadow:0 10px 30px rgba(11,51,40,.14)}
      .masthead h1 {margin:0; font-size:2.15rem; letter-spacing:-.035em; color:white}
      .masthead p {margin:.4rem 0 0; color:#d6eee5; font-size:.98rem}
      .metric-card {border:1px solid #e3e9e6; border-radius:14px; padding:15px 16px; min-height:116px; background:#fff; box-shadow:0 3px 13px rgba(20,43,35,.05); margin-bottom:12px}
      .metric-label {text-transform:uppercase; letter-spacing:.055em; color:#6a766f; font-size:.72rem; font-weight:700}
      .metric-value {font-size:1.52rem; line-height:1.2; margin:.38rem 0; font-weight:680; color:#18251f}
      .metric-detail {font-size:.78rem; color:#758079}
      .decision-hero {border-radius:18px; padding:24px 28px; margin:12px 0 18px; border-left:8px solid #c28b16; background:#fff7e3}
      .decision-hero.success {border-color:#16855b; background:#eef9f3}
      .decision-hero.critical {border-color:#b84040; background:#fff1f1}
      .decision-kicker {font-size:.72rem; letter-spacing:.11em; font-weight:800; color:#66736c}
      .decision-label {font-size:2.35rem; font-weight:800; letter-spacing:-.04em; color:#17231e; float:left; margin-right:22px}
      .decision-title {font-size:1.2rem; font-weight:700; padding-top:.35rem; color:#17231e}
      .decision-detail {font-size:.9rem; color:#58645e; padding-top:.25rem; clear:both}
      .section-title {font-size:1.42rem; margin:1.55rem 0 .15rem; letter-spacing:-.025em}
      div[data-testid="stPills"] {margin:.4rem 0 1rem}
      section[data-testid="stSidebar"] {background:#f5f8f6; border-right:1px solid #dde7e1}
      section[data-testid="stSidebar"] [data-testid="stVerticalBlock"] {gap:.72rem}
      section[data-testid="stSidebar"] .stButton button {border-radius:10px; font-weight:650}
      @media (max-width: 900px) {.decision-label {float:none; margin:0}.metric-value{font-size:1.25rem}}
    </style>
    """,
    unsafe_allow_html=True,
)
st.markdown(
    """
    <div class="masthead">
      <h1>Swing Trader V2</h1>
      <p>Explainable long-only momentum decisions · disciplined exits · prospective paper/live tracking · human execution only</p>
    </div>
    """,
    unsafe_allow_html=True,
)

state = latest_stock_dashboard_state(STOCK_LINEAGES, STOCK_SHADOW_CONFIG)
if state is None:
    status_label = "NO STOCK STATE"
    as_of = "Not started"
    policy_label = "UNKNOWN"
    primary_gate = "NOT RUN"
else:
    status_label = "INTACT" if state.integrity_passed else "FAILED"
    as_of = str(state.payload.get("as_of_session", "Unknown"))
    policy_label = "CURRENT" if state.current_policy else "REFRESH NEEDED"
    primary_gate = "PASS" if state.payload.get("eligible_for_primary_prospective_performance") else "FAIL CLOSED"
_render_refresh_sidebar(state)
_cards(
    [
        ("Stock state", status_label, "Content-hash verification"),
        ("Signals as of", as_of, "Close-known information"),
        ("Policy lineage", policy_label, "Changed policy starts from cash"),
        ("Decision data", primary_gate, "Failed sessions are excluded"),
    ]
)

route = st.pills(
    "Primary navigation",
    ["Today", "Forward Test", "Performance", "Risk", "Research & Evidence"],
    default="Today",
    label_visibility="collapsed",
)
if route == "Forward Test":
    _render_forward(state)
elif route == "Performance":
    _render_performance(state)
elif route == "Risk":
    _render_risk(state)
elif route == "Research & Evidence":
    _render_research(state)
else:
    _render_today(state)

st.divider()
st.caption(
    "Research and recordkeeping only. This application has no brokerage credentials, cannot place an order, and treats failed or stale evidence as non-actionable."
)
