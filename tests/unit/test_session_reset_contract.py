"""Source-level contract test for session-reset dispatch.

Asserts that no module under src/ calls `Session.is_session_start(...)`
directly except for files explicitly allowlisted at the top of this
module. The intent: when a future module needs session-reset behavior,
the test fails loudly and forces the author to either route through
`should_reset_session_at_bar` (the centralized helper) or add the file
to the allowlist with a justification.

Spirit-cousin to test_indicator_kwarg_contract.py — both prevent the
"we forgot to update the second consumer" class of bug. This was the
direct cause of the Fix #5 follow-up bug: diagnose_signal_frequency
called Session.is_session_start without the bar_timeframe gate the
backtester had, so the diagnostic over-reported cold for daily
strategies.

Implementation: AST-walk every .py under src/, look for any Call whose
function is an Attribute with attr=='is_session_start'. The test fails
on the first non-allowlisted occurrence, with a message that names the
helper and points at this file's allowlist.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


# Files under src/ that may legitimately call Session.is_session_start
# directly. Every entry needs a one-line "why" so the next reader knows
# whether the entry should still be there.
ALLOWLIST: dict[str, str] = {
    # Defines the Session abstract method + concrete implementations and
    # implements the centralized dispatch helper that wraps it. The single
    # legitimate direct call to Session.is_session_start lives inside
    # should_reset_session_at_bar.
    "engine/session.py": "implements should_reset_session_at_bar (the helper)",
}


def _find_is_session_start_calls(tree: ast.AST) -> list[int]:
    """Return line numbers of Call(func=Attribute(attr='is_session_start'))
    occurrences. Catches `obj.is_session_start(...)` regardless of what
    `obj` is — Session, self.session, config.session, etc."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "is_session_start":
                lines.append(node.lineno)
    return lines


def _iter_src_py_files():
    for path in _SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        yield path


def test_no_unauthorized_is_session_start_calls():
    """Every direct call to Session.is_session_start under src/ must be in
    a file on the allowlist. Otherwise the centralized dispatch is being
    bypassed and a divergence-class bug is one PR away."""
    violations: list[str] = []
    for path in _iter_src_py_files():
        rel = path.relative_to(_SRC_ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:
            pytest.fail(f"AST parse failed on {rel}: {e}")
        lines = _find_is_session_start_calls(tree)
        if not lines:
            continue
        if rel in ALLOWLIST:
            continue
        for ln in lines:
            violations.append(f"{rel}:{ln}")

    assert not violations, (
        "Found direct call(s) to Session.is_session_start in non-allowlisted "
        "module(s):\n  "
        + "\n  ".join(violations)
        + "\n\nUse should_reset_session_at_bar from src/engine/session.py "
        "instead, or add this file to the contract-test allowlist in "
        "tests/unit/test_session_reset_contract.py with a justification "
        "comment."
    )


def test_allowlist_entries_actually_exist():
    """Catch stale allowlist entries: every file in ALLOWLIST must exist
    under src/ (otherwise the entry is dead weight and the file-level
    'is this allowed?' check is trivially permissive). Also confirms each
    allowlisted file actually contains an is_session_start call — if it
    doesn't, the entry is no longer needed and should be removed."""
    for rel, justification in ALLOWLIST.items():
        path = _SRC_ROOT / rel
        assert path.exists(), (
            f"Allowlist entry {rel!r} (justification: {justification!r}) "
            f"does not exist under src/. Remove the stale entry."
        )
        tree = ast.parse(path.read_text())
        lines = _find_is_session_start_calls(tree)
        assert lines, (
            f"Allowlist entry {rel!r} (justification: {justification!r}) "
            f"has no calls to is_session_start. Remove the stale entry."
        )
