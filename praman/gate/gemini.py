"""A Gemini-backed adjudicator, behind the same protocol as the Claude one.

The ``Adjudicator`` protocol already existed, so this is an addition rather
than a rewrite: the gate, the bounds checker, the fencing, the decision record
and every test are untouched. Only the thing that answers the semantic question
changes.

Two things carry over unchanged, and they are the parts that matter for safety:

  * **The same system prompt.** The fencing contract, the "seller text is data
    not instruction" framing, the STEP_UP definition -- all identical. Rewriting
    the prompt per provider would mean the two sets of numbers were measuring
    different systems, and comparing them would be meaningless.
  * **Failing closed.** An adjudicator that cannot answer returns STEP_UP, never
    ALLOW. On free-tier quota that is not a theoretical path: a pool that runs
    dry mid-run must degrade to asking a human, and it does.

**The numbers do not transfer.** Metrics measured on `claude-opus-5` describe
`claude-opus-5`. Running the gate on Gemini Flash means re-running the
evaluation and labelling the result with the model that produced it. The run
metadata records which model answered, so a report can never quietly inherit a
number from a different one.
"""
from __future__ import annotations

import os
import time

from ..keyring import AllKeysExhausted, GeminiKeyRing, call_with_rotation
from ..mandate.schema import Mandate, Proposal
from .adjudicator import (Adjudication, AdjudicationResult, SYSTEM_PROMPT,
                          render_mandate_block, render_proposal_block)
from .bounds import BoundsResult
from .fencing import Fence

DEFAULT_GEMINI_MODEL = os.getenv("PRAMAN_GEMINI_MODEL", "gemini-2.5-flash")


class GeminiAdjudicator:
    """Same job, same prompt, same output schema. Different provider.

    ``thinking_budget`` is worth a word. Gemini 2.5 Flash reasons before
    answering and those tokens are billed as output, which on a free tier is the
    scarcer resource. Zero would be cheapest and is the wrong default: this is a
    judgment task under adversarial input, and a classifier that does not think
    about a category-laundering attempt will not catch one. The default here is
    a modest budget rather than none, and it is a dial the evaluation can sweep
    -- which is the honest way to choose it, rather than guessing.
    """

    def __init__(self, ring: GeminiKeyRing | None = None,
                 model: str = DEFAULT_GEMINI_MODEL,
                 thinking_budget: int | None = 512,
                 on_retry=None):
        self.ring = ring or GeminiKeyRing(
            rpm_per_key=float(os.getenv("PRAMAN_GEMINI_RPM", "10")))
        self.model = model
        self.thinking_budget = thinking_budget
        self.on_retry = on_retry

    def _config(self):
        from google.genai import types
        kwargs = dict(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=Adjudication,
            # The verdict is a small structured object. Capping output keeps a
            # runaway generation from eating a free-tier minute's tokens.
            max_output_tokens=2048,
        )
        if self.thinking_budget is not None:
            kwargs["thinking_config"] = types.ThinkingConfig(
                thinking_budget=self.thinking_budget)
        return types.GenerateContentConfig(**kwargs)

    def adjudicate(self, mandate: Mandate, proposal: Proposal,
                   bounds: BoundsResult) -> AdjudicationResult:
        fence = Fence.new()
        mandate_block = render_mandate_block(mandate)
        proposal_block, signals = render_proposal_block(proposal, bounds, fence)
        prompt = f"{mandate_block}\n\n{proposal_block}"

        started = time.perf_counter()
        try:
            def call(client):
                return client.models.generate_content(
                    model=self.model, contents=prompt, config=self._config())

            resp = call_with_rotation(self.ring, call, on_retry=self.on_retry)
            a: Adjudication = _parsed(resp)
        except Exception as exc:                       # noqa: BLE001
            reason = (
                "every Gemini key in the pool has spent its daily quota"
                if isinstance(exc, AllKeysExhausted)
                else f"adjudicator unavailable ({exc.__class__.__name__})")
            return AdjudicationResult(
                verdict="STEP_UP",
                reason=f"{reason}; deferring to a human rather than guessing",
                cited_clause="(adjudicator error)", confidence="low",
                listing_attempted_instruction=False,
                sanitization_signals=signals,
                latency_ms=(time.perf_counter() - started) * 1000,
                model=self.model, error=f"{exc.__class__.__name__}: {exc}")

        elapsed = (time.perf_counter() - started) * 1000
        usage = getattr(resp, "usage_metadata", None)
        return AdjudicationResult(
            verdict=a.verdict, reason=a.reason, cited_clause=a.cited_clause,
            confidence=a.confidence,
            listing_attempted_instruction=a.listing_attempted_instruction,
            sanitization_signals=signals, latency_ms=elapsed, model=self.model,
            input_tokens=getattr(usage, "prompt_token_count", 0) or 0,
            output_tokens=getattr(usage, "candidates_token_count", 0) or 0)


def _parsed(resp) -> Adjudication:
    """Get the structured verdict out of a Gemini response.

    ``.parsed`` is the SDK's own validated object and is the normal path. It can
    be None when the model hit its output cap mid-JSON, so fall back to parsing
    the text and let a genuinely malformed answer raise -- an unparseable
    verdict must reach the caller as a failure, which fails closed to STEP_UP,
    rather than being coerced into some default that would fail open.
    """
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, Adjudication):
        return parsed
    if isinstance(parsed, dict):
        return Adjudication.model_validate(parsed)
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        raise ValueError("Gemini returned no parseable verdict "
                         "(likely truncated by max_output_tokens)")
    return Adjudication.model_validate_json(text)
