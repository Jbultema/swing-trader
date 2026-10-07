from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class SharadarStockPanel:
    """Canonical adjusted OHLCV and point-in-time S&P 500 membership."""

    ohlcv: pd.DataFrame
    membership: pd.DataFrame
    provider: str = "Sharadar Prices"
    security_identity: str = "Sharadar unique ticker including recycled-symbol suffix"
    terminal_return_overrides: pd.DataFrame | None = None
    unsupported_terminal_events: pd.DataFrame | None = None


@dataclass(frozen=True)
class SharadarTerminalReturns:
    overrides: pd.DataFrame
    unsupported_events: pd.DataFrame


def load_sharadar_panel(
    stocks_path: Path | str,
    sp500_path: Path | str,
    actions_path: Path | str | None = None,
) -> SharadarStockPanel:
    stocks = pd.read_csv(stocks_path)
    sp500 = pd.read_csv(sp500_path)
    ohlcv = normalize_sharadar_prices(stocks)
    membership = build_sharadar_membership(sp500, ohlcv.index)
    if actions_path is None:
        return SharadarStockPanel(ohlcv=ohlcv, membership=membership)
    terminal = build_sharadar_terminal_returns(pd.read_csv(actions_path), stocks, ohlcv.index)
    return SharadarStockPanel(
        ohlcv=ohlcv,
        membership=membership,
        terminal_return_overrides=terminal.overrides,
        unsupported_terminal_events=terminal.unsupported_events,
    )


def write_sharadar_panel(
    stocks_path: Path | str,
    sp500_path: Path | str,
    output_dir: Path | str,
    *,
    actions_path: Path | str | None = None,
) -> SharadarStockPanel:
    stocks_source = Path(stocks_path)
    sp500_source = Path(sp500_path)
    destination = Path(output_dir)
    actions_source = Path(actions_path) if actions_path is not None else None
    panel = load_sharadar_panel(stocks_source, sp500_source, actions_source)
    destination.mkdir(parents=True, exist_ok=True)
    prices_output = destination / "ohlcv.parquet"
    membership_output = destination / "membership.parquet"
    panel.ohlcv.to_parquet(prices_output)
    panel.membership.to_parquet(membership_output)
    if panel.terminal_return_overrides is not None:
        panel.terminal_return_overrides.to_parquet(destination / "terminal_returns.parquet")
    if panel.unsupported_terminal_events is not None:
        panel.unsupported_terminal_events.to_parquet(
            destination / "unsupported_terminal_events.parquet"
        )
    manifest = {
        "created_at_utc": datetime.now(UTC).isoformat(),
        "provider": panel.provider,
        "security_identity": panel.security_identity,
        "price_adjustment": (
            "total-return OHLC imputed with closeadj/close; split-adjusted volume unchanged"
        ),
        "membership_semantics": "current anchor reversed through adds/removes; effective inclusive",
        "data_start": str(panel.ohlcv.index.min().date()),
        "data_end": str(panel.ohlcv.index.max().date()),
        "sessions": len(panel.ohlcv),
        "price_tickers": len(panel.ohlcv.columns.get_level_values("ticker").unique()),
        "membership_tickers": len(panel.membership.columns),
        "terminal_event_model_status": (
            "not_supplied"
            if actions_source is None
            else (
                "complete_for_supported_cash_and_bankruptcy_events"
                if panel.unsupported_terminal_events is not None
                and panel.unsupported_terminal_events.empty
                else "partial_unsupported_events_require_fail_closed_backtest"
            )
        ),
        "unsupported_terminal_event_rows": (
            0
            if panel.unsupported_terminal_events is None
            else len(panel.unsupported_terminal_events)
        ),
        "inputs": {
            "stocks": {
                "filename": stocks_source.name,
                "sha256": _sha256(stocks_source),
            },
            "sp500": {
                "filename": sp500_source.name,
                "sha256": _sha256(sp500_source),
            },
        },
        "outputs": {
            "ohlcv.parquet": _sha256(prices_output),
            "membership.parquet": _sha256(membership_output),
        },
    }
    if actions_source is not None:
        manifest["inputs"]["actions"] = {  # type: ignore[index]
            "filename": actions_source.name,
            "sha256": _sha256(actions_source),
        }
        manifest["outputs"]["terminal_returns.parquet"] = _sha256(  # type: ignore[index]
            destination / "terminal_returns.parquet"
        )
        manifest["outputs"]["unsupported_terminal_events.parquet"] = _sha256(  # type: ignore[index]
            destination / "unsupported_terminal_events.parquet"
        )
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return panel


def normalize_sharadar_prices(raw: pd.DataFrame) -> pd.DataFrame:
    """Convert documented Sharadar price fields into total-return-adjusted OHLCV.

    Sharadar's OHLC fields are split-adjusted while ``closeadj`` additionally
    includes cash dividends and spinoffs. Multiplying all OHLC fields by
    ``closeadj / close`` creates a consistent total-return price basis. Volume is
    already split-adjusted and is not changed for cash distributions.
    """
    required = {"ticker", "date", "open", "high", "low", "close", "volume", "closeadj"}
    if missing := required - set(raw.columns):
        raise ValueError(f"Sharadar stocks table is missing columns: {sorted(missing)}")
    frame = raw.loc[:, sorted(required)].copy()
    frame["ticker"] = frame["ticker"].astype(str).str.strip()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame[["date", "ticker"]].duplicated().any():
        raise ValueError("Sharadar stocks table contains duplicate date/ticker rows.")
    numeric = ["open", "high", "low", "close", "volume", "closeadj"]
    frame[numeric] = frame[numeric].apply(pd.to_numeric, errors="coerce")
    if frame[["open", "high", "low", "close", "closeadj"]].le(0.0).any(axis=None):
        raise ValueError("Sharadar price fields must be positive.")
    if frame[numeric].isna().any(axis=None):
        raise ValueError("Sharadar stocks table contains null required price fields.")
    if (frame["high"] < frame["low"]).any():
        raise ValueError("Sharadar stocks table contains a high below its low.")

    adjustment = frame["closeadj"].div(frame["close"])
    fields = {
        "Open": frame["open"].mul(adjustment),
        "High": frame["high"].mul(adjustment),
        "Low": frame["low"].mul(adjustment),
        "Close": frame["closeadj"],
        "Volume": frame["volume"],
    }
    wide = {
        name: pd.Series(
            values.to_numpy(), index=pd.MultiIndex.from_frame(frame[["date", "ticker"]])
        )
        .unstack("ticker")
        .sort_index()
        for name, values in fields.items()
    }
    result = pd.concat(wide, axis=1)
    result.columns.names = ["field", "ticker"]
    return result.sort_index().sort_index(axis=1)


def build_sharadar_membership(
    raw: pd.DataFrame,
    sessions: pd.DatetimeIndex,
) -> pd.DataFrame:
    """Reconstruct daily S&P 500 membership from current/add/remove events.

    Event dates are effective inclusively. Events on a non-session date take
    effect on the next supplied session.
    """
    required = {"date", "action", "ticker"}
    if missing := required - set(raw.columns):
        raise ValueError(f"Sharadar S&P 500 table is missing columns: {sorted(missing)}")
    events = raw.loc[:, ["date", "action", "ticker"]].copy()
    events["date"] = pd.to_datetime(events["date"], errors="raise")
    events["action"] = events["action"].astype(str).str.strip().str.lower()
    events["ticker"] = events["ticker"].astype(str).str.strip()
    allowed = {"current", "added", "removed", "historical"}
    if unknown := set(events["action"]) - allowed:
        raise ValueError(f"Unsupported Sharadar S&P 500 actions: {sorted(unknown)}")
    current = set(events.loc[events["action"] == "current", "ticker"])
    if not current:
        raise ValueError("Sharadar S&P 500 table has no current membership anchor.")

    transitions = events.loc[events["action"].isin(["added", "removed"])].sort_values(
        ["date", "action", "ticker"]
    )
    state = set(current)
    for row in transitions.iloc[::-1].itertuples(index=False):
        if row.action == "added":
            state.discard(row.ticker)
        else:
            state.add(row.ticker)

    dates = pd.DatetimeIndex(pd.to_datetime(sessions)).tz_localize(None).sort_values().unique()
    tickers = sorted(set(events["ticker"]))
    membership = pd.DataFrame(False, index=dates, columns=tickers)
    event_rows = list(transitions.itertuples(index=False))
    event_position = 0
    for date in dates:
        while event_position < len(event_rows) and event_rows[event_position].date <= date:
            row = event_rows[event_position]
            if row.action == "added":
                state.add(row.ticker)
            else:
                state.discard(row.ticker)
            event_position += 1
        active = membership.columns.intersection(state)
        membership.loc[date, active] = True
    history_rows = events.loc[events["action"].isin(["added", "removed", "historical"])]
    history_floor = history_rows["date"].min() if not history_rows.empty else events["date"].min()
    membership.loc[membership.index < history_floor] = False
    membership.index.name = "date"
    membership.columns.name = "ticker"
    historical = events.loc[events["action"] == "historical"]
    for snapshot_date, snapshot in historical.groupby("date"):
        prior_sessions = dates[dates <= snapshot_date]
        if not len(prior_sessions):
            continue
        effective_session = prior_sessions[-1]
        reconstructed = set(membership.columns[membership.loc[effective_session].astype(bool)])
        observed = set(snapshot["ticker"])
        if reconstructed != observed:
            missing = sorted(observed - reconstructed)
            extra = sorted(reconstructed - observed)
            raise ValueError(
                "Sharadar historical membership snapshot disagrees with events "
                f"on {snapshot_date.date()}: missing={missing}, extra={extra}"
            )
    return membership


def build_sharadar_terminal_returns(
    actions: pd.DataFrame,
    split_adjusted_prices: pd.DataFrame,
    sessions: pd.DatetimeIndex,
) -> SharadarTerminalReturns:
    """Model only terminal events whose economic value is unambiguous.

    Cash-only acquisitions use cash consideration divided by the final session's
    split-adjusted open. Bankruptcy/liquidation receives a conservative -100%.
    Stock consideration, elections, and unexplained delistings remain unsupported
    and therefore cannot silently enter a backtest.
    """
    action_columns = {"date", "action", "ticker", "value", "contraticker"}
    if missing := action_columns - set(actions.columns):
        raise ValueError(f"Sharadar actions table is missing columns: {sorted(missing)}")
    price_columns = {"date", "ticker", "open"}
    if missing := price_columns - set(split_adjusted_prices.columns):
        raise ValueError(f"Sharadar prices table is missing columns: {sorted(missing)}")

    events = actions.loc[:, sorted(action_columns)].copy()
    events["date"] = pd.to_datetime(events["date"], errors="raise")
    events["action"] = events["action"].astype(str).str.strip().str.lower()
    events["ticker"] = events["ticker"].astype(str).str.strip()
    events["value"] = pd.to_numeric(events["value"], errors="coerce")
    prices = split_adjusted_prices.loc[:, ["date", "ticker", "open"]].copy()
    prices["date"] = pd.to_datetime(prices["date"], errors="raise")
    prices["ticker"] = prices["ticker"].astype(str).str.strip()
    prices["open"] = pd.to_numeric(prices["open"], errors="coerce")
    price_lookup = prices.set_index(["date", "ticker"])["open"]

    terminal_actions = {
        "acquisitioncash",
        "acquisitionstock",
        "acquisitionelectcash",
        "acquisitionelectstock",
        "bankruptcyliquidation",
        "delisted",
        "regulatorydelisting",
        "voluntarydelisting",
    }
    relevant = events.loc[events["action"].isin(terminal_actions)].copy()
    dates = pd.DatetimeIndex(pd.to_datetime(sessions)).tz_localize(None).sort_values().unique()
    tickers = sorted(set(relevant["ticker"]))
    overrides = pd.DataFrame(float("nan"), index=dates, columns=tickers)
    unsupported: list[pd.DataFrame] = []

    for (date, ticker), group in relevant.groupby(["date", "ticker"], sort=True):
        if date not in overrides.index:
            unsupported.append(group)
            continue
        kinds = set(group["action"])
        if "bankruptcyliquidation" in kinds:
            overrides.loc[date, ticker] = -1.0
            continue
        cash = group.loc[group["action"] == "acquisitioncash", "value"]
        has_stock_or_election = bool(
            kinds
            & {
                "acquisitionstock",
                "acquisitionelectcash",
                "acquisitionelectstock",
            }
        )
        if len(cash) and not has_stock_or_election and cash.notna().all():
            key = (pd.Timestamp(date), ticker)
            final_open = price_lookup.get(key)
            if pd.notna(final_open) and float(final_open) > 0.0:
                overrides.loc[date, ticker] = float(cash.sum()) / float(final_open) - 1.0
                continue
        unsupported.append(group)

    unsupported_frame = (
        pd.concat(unsupported, ignore_index=True) if unsupported else relevant.iloc[0:0].copy()
    )
    overrides.index.name = "date"
    overrides.columns.name = "ticker"
    return SharadarTerminalReturns(overrides=overrides, unsupported_events=unsupported_frame)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
