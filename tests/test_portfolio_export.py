import numpy as np
import pandas as pd

from swing_trader.config import load_config
from swing_trader.portfolio_export import research_targets


def sample():
    config = load_config("config/default.toml")
    dates = pd.bdate_range("2010-01-01", periods=500)
    rng = np.random.default_rng(8)
    close = pd.DataFrame(
        100 * np.exp(rng.normal(0.0008, 0.01, (500, len(config.data.tickers))).cumsum(axis=0)),
        index=dates,
        columns=config.data.tickers,
    )
    return config, pd.concat(
        {
            "Close": close,
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Volume": close * 0 + 1000000,
        },
        axis=1,
    )


def test_swing_export_has_frozen_primary_and_distinct_diagnostics():
    config, p = sample()
    targets = research_targets(p, config)
    assert targets["swing_primary"][2] == "primary"
    assert targets["swing_fast_rejected"][2] == "rejected_diagnostic"
    for w, _, _ in targets.values():
        assert w.min().min() >= 0
        assert w.sum(axis=1).max() <= 1 + 1e-12


def test_swing_export_cannot_read_future_prices():
    config, p = sample()
    a, b = research_targets(p, config), research_targets(p.iloc[:417], config)
    for name in a:
        pd.testing.assert_frame_equal(a[name][0].iloc[:417], b[name][0])


def test_interpretation_diagnostics_reconcile_and_do_not_promote_stock_shadow():
    from swing_trader.interpretation_export import signals

    config, p = sample()
    result = signals(p["Close"], config)
    targets = result[[c for c in result if c.startswith("target_") and c != "target_exposure"]].rename(columns=lambda c: c[7:])
    pd.testing.assert_frame_equal(targets, research_targets(p, config)["swing_primary"][0])
    pd.testing.assert_frame_equal(result.iloc[:417], signals(p["Close"].iloc[:417], config))
