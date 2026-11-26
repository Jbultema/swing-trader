"""Describe the frozen ETF primary, not the faster rejected stock/ETF experiments."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from swing_trader.config import load_config


def identity(root=Path(".")):
    path = root / "config/default.toml"
    config = load_config(path)
    return {"schema_version": 1, "system": "swing-trader", "role": "monthly_momentum",
        "primary": f"monthly_12m_top{config.strategy.top_n}", "export_primary": "swing_primary",
        "configuration_consistent": True, "primary_source": "portfolio_export.research_targets:classic_dual_momentum_weights",
        "native_clock": "next_adjusted_open_open_to_open",
        "configuration_sha256": {"config/default.toml": hashlib.sha256(path.read_bytes()).hexdigest()},
        "action_authorized": False, "portfolio_allocation_allowed": False}


if __name__ == "__main__":
    print(json.dumps(identity(), sort_keys=True))
