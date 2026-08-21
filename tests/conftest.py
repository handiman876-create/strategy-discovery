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

import pytest
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
for p in (_ROOT / "src", _ROOT, _ROOT / "strategies"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)

load_dotenv(_ROOT / ".env", override=True)


@pytest.fixture(autouse=True)
def isolated_generator_paths(tmp_path, monkeypatch):
    """Redirect the generation archive and the quirk counter into tmp_path for
    EVERY test.

    WHY AUTOUSE: before 2026-08-21 these two paths were module-level constants
    in four separate modules with no seam to redirect, so the suite wrote into
    production. One pytest run deposited 26 fixture records into
    results/generations/ — enough that `ls -t` on the archive stopped returning
    real failures and intercepted three consecutive attempts to inspect a live
    regression — and incremented the real results/generation_quirks.json, which
    is the series we use to judge whether the stringify safety net still earns
    its keep.

    Individual tests used to monkeypatch whichever constant they happened to
    touch. That only ever protected the paths someone remembered; autouse makes
    isolation the default so a new test cannot silently pollute.

    setenv works here (where an import-time `os.environ.get` constant would
    not) because generator.paths resolves both paths on every call.
    """
    from generator import paths

    # Deliberately NOT tmp_path/"generations": several tests build their own
    # fixture archive at that exact path and call an unguarded .mkdir() on it.
    gens = tmp_path / "_isolated_generations"
    gens.mkdir(exist_ok=True)
    monkeypatch.setenv(paths.ENV_GENERATIONS_DIR, str(gens))
    monkeypatch.setenv(paths.ENV_QUIRKS_PATH, str(tmp_path / "generation_quirks.json"))
    return gens
