"""Generation orchestrator.

generate_strategy(archetype) → GenerateResult (spec, logs)
generate_and_translate(archetype) → GenerateResult (spec, logs, hash, code_path)

Retry loop: up to 3 attempts. Each retry feeds the prior attempt's failure
reason back to Claude as `retry_feedback`. Validation errors include
StrategySpec parse errors, translator validation errors, and behavioral-dedup
collisions.

Diversity context: scans `results/generations/` for the most recent N
successful generations of the same archetype and includes a short summary
in the prompt under "Already explored — your spec must materially differ".
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .archetypes import get_archetype
from .claude_client import ClaudeClient, GenerationLog
from .dedup import behavioral_hash
from .spec import StrategySpec
from .translator import GENERATED_DIR, TranslationError, translate_to_file

DEFAULT_MAX_RETRIES = 3
DEFAULT_DIVERSITY_N = 5

GENERATIONS_DIR = Path(__file__).resolve().parents[2] / "results" / "generations"


@dataclass
class GenerateResult:
    spec: StrategySpec | None
    logs: list[GenerationLog]
    behavioral_hash: str | None = None
    code_path: Path | None = None
    failure_reason: str | None = None


def generate_strategy(
    archetype: str,
    *,
    client: ClaudeClient | None = None,
    diversity_n: int = DEFAULT_DIVERSITY_N,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> GenerateResult:
    """Generate a single valid spec for `archetype`. Retries up to `max_retries`
    times with error feedback. Returns the spec + the full attempt log."""
    client = client or ClaudeClient()
    arch = get_archetype(archetype)
    diversity = _load_diversity_context(archetype, n=diversity_n)
    prior_hashes = _load_prior_behavioral_hashes(archetype)

    logs: list[GenerationLog] = []
    feedback: str | None = None

    for attempt in range(1, max_retries + 1):
        spec, log = client.generate_spec(
            archetype,
            diversity_context=diversity,
            retry_feedback=feedback,
            attempt=attempt,
        )
        logs.append(log)

        if spec is None:
            feedback = f"Attempt {attempt} failed: {log.error}"
            continue

        # Translate-level validation (archetype/asset compat, daily_return, pairs).
        try:
            from .translator import validate_for_translation

            validate_for_translation(spec)
        except TranslationError as e:
            feedback = f"Attempt {attempt} translator rejected: {e}"
            continue

        return GenerateResult(spec=spec, logs=logs)

    return GenerateResult(
        spec=None,
        logs=logs,
        failure_reason=f"all {max_retries} attempts failed",
    )


def generate_and_translate(
    archetype: str,
    *,
    client: ClaudeClient | None = None,
    diversity_n: int = DEFAULT_DIVERSITY_N,
    max_retries: int = DEFAULT_MAX_RETRIES,
    dedup: bool = True,
) -> GenerateResult:
    """Generate + translate. Adds behavioral-dedup retry on top of generate_strategy."""
    client = client or ClaudeClient()
    prior_hashes = _load_prior_behavioral_hashes(archetype) if dedup else set()

    feedback: str | None = None
    diversity = _load_diversity_context(archetype, n=diversity_n)
    logs: list[GenerationLog] = []

    for attempt in range(1, max_retries + 1):
        spec, log = client.generate_spec(
            archetype,
            diversity_context=diversity,
            retry_feedback=feedback,
            attempt=attempt,
        )
        logs.append(log)

        if spec is None:
            feedback = f"Attempt {attempt} failed: {log.error}"
            continue

        try:
            path = translate_to_file(spec, overwrite=True)
        except TranslationError as e:
            feedback = f"Attempt {attempt} translator rejected: {e}"
            continue
        except Exception as e:
            feedback = f"Attempt {attempt} translator crashed: {e}"
            continue

        if dedup:
            try:
                strategy_class = _import_generated(spec.name, path)
                bh = behavioral_hash(strategy_class)
            except Exception as e:
                feedback = f"Attempt {attempt} behavioral hash failed: {e}"
                continue

            if bh in prior_hashes:
                feedback = (
                    f"Attempt {attempt} duplicate of an existing strategy (behavioral "
                    f"hash {bh[:12]}). Choose materially different parameters or logic."
                )
                continue
        else:
            bh = None

        return GenerateResult(spec=spec, logs=logs, behavioral_hash=bh, code_path=path)

    return GenerateResult(
        spec=None,
        logs=logs,
        failure_reason=f"all {max_retries} attempts failed",
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _load_diversity_context(archetype: str, n: int) -> list[dict]:
    """Scan results/generations/ for recent successful generations of this
    archetype, return short summaries for the prompt."""
    if not GENERATIONS_DIR.exists():
        return []
    matches: list[tuple[str, dict]] = []
    for path in sorted(GENERATIONS_DIR.glob(f"*_{archetype}_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        if payload.get("spec") is None:
            continue
        spec = payload["spec"]
        matches.append(
            (
                payload["timestamp"],
                {
                    "name": spec.get("name"),
                    "thesis": spec.get("thesis", ""),
                    "indicators": [i["type"] for i in spec.get("indicators", [])],
                },
            )
        )
        if len(matches) >= n:
            break
    return [m[1] for m in matches]


def _load_prior_behavioral_hashes(archetype: str) -> set[str]:
    if not GENERATIONS_DIR.exists():
        return set()
    out: set[str] = set()
    for path in GENERATIONS_DIR.glob(f"*_{archetype}_*.json"):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        h = payload.get("behavioral_hash")
        if h:
            out.add(h)
    return out


def _import_generated(name: str, path: Path):
    spec_mod = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    # Strategy class name is CamelCase of the snake name.
    class_name = "".join(p.capitalize() for p in name.split("_"))
    return getattr(mod, class_name)
