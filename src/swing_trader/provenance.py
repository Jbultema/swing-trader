from __future__ import annotations

import hashlib
from pathlib import Path


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
