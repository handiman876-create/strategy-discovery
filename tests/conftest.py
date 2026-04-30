"""Pytest configuration: ensure src/ and strategies/ are importable, and
load .env so tests see the same configuration as scripts/.

Without the load_dotenv call, pytest sees stale shell environment values
that shadow the .env file. That caused the Phase 4 step 11 integration
test to authenticate with a placeholder ANTHROPIC_API_KEY and fail with
HTTP 401 despite .env having a valid key. Per project convention
(docs/coding-conventions.md), only top-level entry points call
load_dotenv — and tests/conftest.py is now recognized as the third
such entry point alongside scripts/ and the future paper-trading
runner. The load is idempotent and harmless for tests that don't need
env values."""

from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
for p in (_ROOT / "src", _ROOT, _ROOT / "strategies"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

load_dotenv(_ROOT / ".env", override=True)
