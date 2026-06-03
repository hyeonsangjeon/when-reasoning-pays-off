"""Local conftest for ``batch-runner/tests/``.

Adds the sibling ``batch-runner`` directory to ``sys.path`` so the
``batch_runner`` package is importable when pytest is invoked from the
repo root (no installed wheel required).
"""

from __future__ import annotations

import sys
from pathlib import Path

_BR_ROOT = Path(__file__).resolve().parent.parent
if str(_BR_ROOT) not in sys.path:
    sys.path.insert(0, str(_BR_ROOT))
