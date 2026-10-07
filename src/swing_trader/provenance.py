from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def implementation_sha256() -> str:
    """Hash the complete installed strategy package with stable path ordering."""
    package_root = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(package_root.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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
