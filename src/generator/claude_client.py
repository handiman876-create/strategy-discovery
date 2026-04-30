"""Anthropic SDK wrapper for strategy generation.

Tool-use call: Claude must call the `submit_strategy_spec` tool whose
input_schema is StrategySpec's JSON schema. Strict mode is enabled so the
SDK validates output before our code sees it.

Prompt caching: the system prompt + archetype prompt are static across
calls (per archetype) and tagged with `cache_control: ephemeral`. Diversity
context and retry feedback go AFTER the cache breakpoint so they don't bust
the cache.

Logging: every call writes a JSON record to results/generations/ with
prompt, raw response, parsed spec, model, tokens, and cost. The spend
tracker is updated atomically: pending entry written BEFORE the API call,
moved to completed (or marked failed) AFTER.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
from anthropic import APIStatusError

from .archetypes import ARCHETYPES, get_archetype
from .spec import StrategySpec
from .spend_tracker import (
    DEFAULT_INPUT_PRICE_PER_MTOK,
    DEFAULT_OUTPUT_PRICE_PER_MTOK,
    SpendTracker,
    estimate_cost,
)

DEFAULT_MODEL = "claude-sonnet-4-6"

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
GENERATIONS_DIR = Path(__file__).resolve().parents[2] / "results" / "generations"

TOOL_NAME = "submit_strategy_spec"
TOOL_DESCRIPTION = (
    "Submit a complete strategy specification matching the StrategySpec schema. "
    "All fields are validated by code; invalid specs will be rejected and you "
    "will be asked to retry with feedback."
)


@dataclass
class GenerationLog:
    timestamp: str
    archetype: str
    model: str
    prompt_hash: str
    system_prompt: str
    user_prompt: str
    raw_tool_input: dict | None
    spec: StrategySpec | None
    error: str | None
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    actual_cost_usd: float
    call_id: str
    attempt: int
    raw_response_path: str | None = None  # set by _save_log; single source of truth for the archived JSON path


class ClaudeClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        spend_tracker: SpendTracker | None = None,
        max_tokens: int = 4096,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self.model = model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
        self.client = anthropic.Anthropic(api_key=self.api_key)
        self.spend_tracker = spend_tracker or SpendTracker()
        self.max_tokens = max_tokens

    # ── Public API ───────────────────────────────────────────────────────────

    def generate_spec(
        self,
        archetype: str,
        *,
        diversity_context: list[dict] | None = None,
        retry_feedback: str | None = None,
        attempt: int = 1,
    ) -> tuple[StrategySpec | None, GenerationLog]:
        """Run one generation attempt. Returns (spec, log). If the spec
        cannot be parsed/validated, spec is None and log carries the error."""
        arch = get_archetype(archetype)
        system_blocks, user_text = self._build_messages(
            archetype, diversity_context=diversity_context, retry_feedback=retry_feedback
        )
        prompt_hash = _hash_prompt(system_blocks, user_text)

        # Reserve estimated cost. We assume ~5K input / 2K output as a coarse cap.
        estimated_input_tokens = 5000
        estimated_output_tokens = 2000
        est = estimate_cost(estimated_input_tokens, estimated_output_tokens)
        call_id = self.spend_tracker.estimate_and_reserve(
            est, model=self.model, archetype=archetype
        )

        log = GenerationLog(
            timestamp=_now_iso(),
            archetype=archetype,
            model=self.model,
            prompt_hash=prompt_hash,
            system_prompt=_render_system(system_blocks),
            user_prompt=user_text,
            raw_tool_input=None,
            spec=None,
            error=None,
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            actual_cost_usd=0.0,
            call_id=call_id,
            attempt=attempt,
        )

        # TODO(strict-tool-use): we considered adding "strict": True to the tool
        # definition to force server-side schema validation. Deferred because
        # (a) the StrategySpec safety-net validator already auto-recovers from
        # the known stringified-DSL quirk, and (b) strict mode would convert
        # that recoverable case into a hard rejection that costs another retry.
        # Revisit once results/generation_quirks.json shows the quirk has gone
        # to zero — at that point strict mode adds defense-in-depth without
        # downside. See docs/coding-conventions.md and Phase 3 E2E notes.
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_blocks,
                tools=[
                    {
                        "name": TOOL_NAME,
                        "description": TOOL_DESCRIPTION,
                        "input_schema": StrategySpec.tool_input_schema(),
                    }
                ],
                tool_choice={"type": "tool", "name": TOOL_NAME},
                messages=[{"role": "user", "content": user_text}],
            )
        except APIStatusError as e:
            self.spend_tracker.record_failure(call_id, error=str(e))
            log.error = f"API error: {e}"
            self._save_log(log)
            return None, log
        except Exception as e:
            self.spend_tracker.record_failure(call_id, error=str(e))
            log.error = f"Unexpected error: {e}"
            self._save_log(log)
            return None, log

        # Reconcile actual usage to spend tracker.
        usage = resp.usage
        actual = estimate_cost(
            usage.input_tokens, usage.output_tokens
        )  # cache reads/writes priced separately, but for Phase 3 audit-trail this is sufficient
        self.spend_tracker.record_actual(
            call_id,
            actual_cost_usd=actual,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            model=self.model,
            archetype=archetype,
        )
        log.input_tokens = usage.input_tokens
        log.output_tokens = usage.output_tokens
        log.cache_read_input_tokens = getattr(usage, "cache_read_input_tokens", 0) or 0
        log.cache_creation_input_tokens = (
            getattr(usage, "cache_creation_input_tokens", 0) or 0
        )
        log.actual_cost_usd = actual

        # Extract tool_use block.
        tool_use = next(
            (b for b in resp.content if getattr(b, "type", None) == "tool_use"), None
        )
        if tool_use is None:
            log.error = "No tool_use block in response"
            self._save_log(log)
            return None, log

        log.raw_tool_input = dict(tool_use.input)

        try:
            spec = StrategySpec.model_validate(
                tool_use.input, context={"model": resp.model}
            )
        except Exception as e:
            log.error = f"StrategySpec validation: {e}"
            self._save_log(log)
            return None, log

        log.spec = spec
        self._save_log(log)
        return spec, log

    # ── Internals ────────────────────────────────────────────────────────────

    def _build_messages(
        self,
        archetype: str,
        *,
        diversity_context: list[dict] | None,
        retry_feedback: str | None,
    ) -> tuple[list[dict], str]:
        """Assemble the system prompt blocks and user message.

        Stable content (system instructions, archetype prompt) is cached.
        Volatile content (diversity context, retry feedback) goes in the
        user message AFTER the cache breakpoint.
        """
        system_text = (PROMPTS_DIR / "_system.md").read_text()
        archetype_text = (PROMPTS_DIR / f"{archetype}.md").read_text()

        # System prompt: two blocks, with a cache breakpoint on the archetype block.
        system_blocks = [
            {"type": "text", "text": system_text},
            {
                "type": "text",
                "text": archetype_text,
                "cache_control": {"type": "ephemeral"},
            },
        ]

        # User message: archetype trigger + dynamic context.
        parts: list[str] = [
            f"Generate a strategy spec for archetype `{archetype}`. "
            "Output ONLY by calling the `submit_strategy_spec` tool."
        ]
        if diversity_context:
            parts.append("\nAlready explored — your spec must materially differ from these:")
            for entry in diversity_context:
                parts.append(
                    f"- name={entry.get('name')}; thesis={entry.get('thesis', '')[:200]}; "
                    f"indicators={entry.get('indicators', [])}"
                )
        if retry_feedback:
            parts.append(
                "\nPrevious attempt failed validation. Specific feedback:\n"
                f"{retry_feedback}\n"
                "Address every issue above before submitting."
            )

        return system_blocks, "\n".join(parts)

    def _save_log(self, log: GenerationLog) -> Path:
        GENERATIONS_DIR.mkdir(parents=True, exist_ok=True)
        slug = log.spec.name if log.spec else "failed"
        path = GENERATIONS_DIR / f"{log.timestamp.replace(':', '-')}_{log.archetype}_{slug}.json"
        payload = {
            "timestamp": log.timestamp,
            "archetype": log.archetype,
            "model": log.model,
            "prompt_hash": log.prompt_hash,
            "system_prompt": log.system_prompt,
            "user_prompt": log.user_prompt,
            "raw_tool_input": log.raw_tool_input,
            "spec": log.spec.model_dump(mode="json") if log.spec else None,
            "error": log.error,
            "input_tokens": log.input_tokens,
            "output_tokens": log.output_tokens,
            "cache_read_input_tokens": log.cache_read_input_tokens,
            "cache_creation_input_tokens": log.cache_creation_input_tokens,
            "actual_cost_usd": log.actual_cost_usd,
            "call_id": log.call_id,
            "attempt": log.attempt,
        }
        path.write_text(json.dumps(payload, indent=2, default=str))
        log.raw_response_path = str(path)
        return path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_prompt(system_blocks: list[dict], user_text: str) -> str:
    h = hashlib.sha256()
    h.update(_render_system(system_blocks).encode())
    h.update(b"\n--user--\n")
    h.update(user_text.encode())
    return h.hexdigest()


def _render_system(system_blocks: list[dict]) -> str:
    return "\n\n".join(b["text"] for b in system_blocks)
