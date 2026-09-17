"""Put the repo root and ``scripts/`` on ``sys.path`` for every test runner.

``unittest`` ignores ``conftest.py``, and bare ``pytest`` does not add the
working directory to ``sys.path``, so neither runner can be trusted to make
``import jev_recon`` and ``import mock_typesafe_server`` work on its own. Each
test module imports this first and both runners behave the same.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT, ROOT / "scripts"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
