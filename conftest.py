"""Make the package and the mock server importable however pytest is invoked.

Without this, ``pytest`` and ``python -m pytest`` disagree about ``sys.path``.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
