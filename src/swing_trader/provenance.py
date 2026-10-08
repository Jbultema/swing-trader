from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

STOCK_POLICY_SOURCE_FILES = (
    "alpha_validation.py",
    "data.py",
    "events.py",
    "stock_candidates.py",
    "stock_live_data.py",
    "stock_shares.py",
    "stock_shadow_state.py",
    "stock_signals.py",
    "stock_turnover.py",
    "stock_universe.py",
)
STOCK_EVALUATION_SOURCE_FILES = (
    "stock_prospective.py",
    "stock_validation.py",
)
PUBLISHED_COMPARATOR_SOURCE_FILES = (
    "published_momentum.py",
    "stock_validation.py",
)


def implementation_sha256() -> str:
    """Hash the complete installed strategy package with stable path ordering."""
    package_root = Path(__file__).parent
    return _source_files_sha256(
        package_root,
        tuple(path.name for path in sorted(package_root.glob("*.py"))),
    )


def stock_policy_sha256(package_root: Path | None = None) -> str:
    """Hash only code that can change stock inputs, gates, targets, or paper accounting."""
    return _source_files_sha256(
        package_root or Path(__file__).parent,
        STOCK_POLICY_SOURCE_FILES,
        domain="stock-decision-policy-v1",
    )


def stock_evaluation_sha256(package_root: Path | None = None) -> str:
    """Hash prospective scoring code without forcing the paper portfolio to restart."""
    return _source_files_sha256(
        package_root or Path(__file__).parent,
        STOCK_EVALUATION_SOURCE_FILES,
        domain="stock-prospective-evaluation-v1",
    )


def published_comparator_sha256(package_root: Path | None = None) -> str:
    """Hash only code that constructs the official published comparator."""
    return _source_files_sha256(
        package_root or Path(__file__).parent,
        PUBLISHED_COMPARATOR_SOURCE_FILES,
        domain="published-momentum-comparator-v1",
    )


def tabular_sha256(value: pd.DataFrame | pd.Series) -> str:
    """Hash an ordered pandas object including labels, schema, values, and nulls."""
    kind = "series" if isinstance(value, pd.Series) else "dataframe"
    frame = value.to_frame() if isinstance(value, pd.Series) else value
    schema = {
        "kind": kind,
        "series_name": repr(value.name) if isinstance(value, pd.Series) else None,
        "index_names": [repr(name) for name in frame.index.names],
        "column_names": [repr(name) for name in frame.columns.names],
        "columns": [repr(column) for column in frame.columns],
        "dtypes": [str(dtype) for dtype in frame.dtypes],
        "shape": list(frame.shape),
    }
    digest = hashlib.sha256(json.dumps(schema, sort_keys=True).encode("utf-8"))
    row_hashes = pd.util.hash_pandas_object(frame, index=True, categorize=True)
    digest.update(row_hashes.to_numpy(dtype=np.uint64, copy=False).tobytes())
    return digest.hexdigest()


def file_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _source_files_sha256(
    package_root: Path,
    filenames: tuple[str, ...],
    *,
    domain: str | None = None,
) -> str:
    digest = hashlib.sha256()
    if domain is not None:
        digest.update(domain.encode("utf-8"))
        digest.update(b"\0")
    for name in sorted(filenames):
        path = package_root / name
        if not path.is_file():
            raise FileNotFoundError(f"Implementation source is unavailable: {path}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()
