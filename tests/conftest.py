"""Pytest configuration: ensure src/ and strategies/ are importable."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for p in (_ROOT / "src", _ROOT, _ROOT / "strategies"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
