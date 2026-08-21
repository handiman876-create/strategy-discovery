"""Single source of truth for the two paths the generator writes observability
data to: the generation archive and the quirk-counter file.

WHY THIS MODULE EXISTS
----------------------
Both paths were spelled out independently in four modules
(generator.claude_client, generator.pipeline, generator.translator,
evaluation.diagnostics) as module-level constants. Two consequences, both of
which bit on 2026-08-21:

  1. Nothing could redirect them. The test suite therefore wrote fixture
     records into the PRODUCTION archive — 26 in a single pytest run, 104 over
     an afternoon — and incremented the production quirk counter. `ls -t
     results/generations/` stopped returning real failures at all, which
     intercepted three separate attempts to inspect the live regression, and
     the counter that exists to tell us whether the stringify safety net is
     still earning its keep began measuring our own test fixtures.
  2. Individual tests worked around (1) by monkeypatching whichever constant
     they happened to touch (tests/unit/test_translator.py,
     test_session_warmup_gate.py, test_pipeline_mocked.py, test_diagnostics.py
     each did it differently). Any test that forgot — and any new code path
     reaching a fifth copy of the constant — silently polluted again.

Resolved at CALL time, not import time, so an env var set by a fixture (or by
systemd, or on the command line) actually takes effect. A module-level constant
is bound before any fixture runs, which is why the obvious
`GENERATIONS_DIR = os.environ.get(...)` at import scope does not work for test
isolation.

Defaults are ABSOLUTE, anchored to the repo root rather than the process cwd:
the nightly run executes under systemd with a working directory that is not
guaranteed to be the repo, and a relative default would scatter the archive.
"""

from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

ENV_GENERATIONS_DIR = "GENERATIONS_DIR"
ENV_QUIRKS_PATH = "GENERATION_QUIRKS_PATH"


def generations_dir() -> Path:
    """Directory holding archived generation records (one JSON per API attempt).

    Override with $GENERATIONS_DIR. Read fresh on every call — see module
    docstring for why this is not a constant.
    """
    override = os.environ.get(ENV_GENERATIONS_DIR)
    return Path(override) if override else _ROOT / "results" / "generations"


def quirks_path() -> Path:
    """The persistent quirk-counter file.

    Override with $GENERATION_QUIRKS_PATH. Every counter-bearing validator
    writes here (see feedback_observability_in_validators in the project
    memory), so redirecting it is the only way to keep a test run from
    corrupting the series those counters exist to produce.
    """
    override = os.environ.get(ENV_QUIRKS_PATH)
    return Path(override) if override else _ROOT / "results" / "generation_quirks.json"
