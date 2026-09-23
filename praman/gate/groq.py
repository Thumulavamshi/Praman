"""A Groq-backed adjudicator, behind the same protocol as the others.

Third provider, same contract. The gate, the bounds checker, the fencing, the
decision record and every test are untouched; only the thing that answers the
semantic question changes. What carries over unchanged is what matters:

  * **The same system prompt.** Identical fencing contract, identical STEP_UP
    definition. A prompt rewritten per provider would mean the three sets of
    numbers measured three different systems, and comparing them would be
    meaningless.
  * **Failing closed.** An adjudicator that cannot answer returns STEP_UP,
    never ALLOW. Free-tier pools run dry mid-run -- that is not theoretical
    here, it is what sent us to Groq -- and a pool that runs out must degrade
    to asking a human.

**The numbers do not transfer.** Metrics measured on one model describe that
model. The run metadata records which one answered, so a report cannot quietly
inherit a figure produced by another.

**Groq rate limits are per organisation, not per key.** Three keys cut from one
account share one allowance: the rotation buys resilience against a single bad
key and nothing whatsoever in throughput. Keys from separate accounts do
multiply. Worth knowing before concluding that a pool of six will go six times
as far, which is the assumption the Gemini pool was built on and Groq does not
honour.
"""
from __future__ import annotations

import json
import os
import time

from ..keyring import (AllKeysExhausted, GeminiKeyRing, call_with_rotation,
                       groq_classify, load_groq_keys_from_env)
from ..mandate.schema import Mandate, Proposal
from .adjudicator import (Adjudication, AdjudicationResult, SYSTEM_PROMPT,
                          render_mandate_block, render_proposal_block)
from .bounds import BoundsResult
from .fencing import Fence

# gpt-oss-120b over the 70B llama: this is a nuanced judgment task under
# adversarial input, and the free tier's daily token ceiling is the binding
# constraint rather than requests -- 200K/day here against 100K on
# llama-3.3-70b-versatile, for a prompt that runs ~2K tokens a case.
DEFAULT_GROQ_MODEL = os.getenv("PRAMAN_GROQ_MODEL", "openai/gpt-oss-120b")

# Requests per minute for the POOL, not per key -- which is the opposite of the
# Gemini ring's convention, and deliberately so. Google meters per key, so more
# keys is more throughput. Groq meters per ORGANISATION, so three keys cut from
# one account share one 30/min allowance and pacing each of them at 30 asks for
# 90. The ring multiplies by the key count internally, so the per-key figure is
# derived by division below.
#
# The default assumes one organisation, which is the safe direction: too slow
# costs minutes, too fast costs the run. Anyone whose keys are genuinely from
# separate accounts can raise it to 30 x accounts.
DEFAULT_GROQ_POOL_RPM = float(os.getenv("PRAMAN_GROQ_RPM", "30"))


def _schema() -> dict:
    """The verdict schema, as JSON Schema, for structured outputs."""
    return {
        "name": "adjudication",
        "description": "An authorization verdict on a proposed purchase.",
        "schema": Adjudication.model_json_schema(),
        "strict": False,
    }


class GroqKeyRing(GeminiKeyRing):
    """The same pool mechanics, reading Groq's variables.

    Subclassed rather than copied: the rotation, pacing, daily-exhaustion
    retirement and masked labels have nothing provider-specific in them, and a
    second copy would be a second place for the bugs already fixed here to come
    back.
    """

    def __init__(self, keys: list[str] | None = None, *,
                 pool_rpm: float = DEFAULT_GROQ_POOL_RPM,
                 model: str = DEFAULT_GROQ_MODEL):
        keys = keys if keys is not None else load_groq_keys_from_env()
        if not keys:
            raise RuntimeError(
                "no Groq keys found. Set GROQ_API_KEYS in .env as a "
                "comma-separated list, e.g.\n"
                '  GROQ_API_KEYS="gsk_...one,gsk_...two,gsk_...three"\n'
                "GROQ_API_KEY is read too, and may hold a list. Keys are\n"
                "issued at console.groq.com/keys.")
        # The parent paces at rpm_per_key * len(keys); divide so the product is
        # the pool figure the caller actually meant.
        super().__init__(keys, rpm_per_key=pool_rpm / max(1, len(set(keys))),
                         model=model)

    def _client_for(self, state):
        from groq import Groq
        with self._lock:
            c = self._clients.get(state.key)
            if c is None:
                c = Groq(api_key=state.key)
                self._clients[state.key] = c
            return c


class GroqAdjudicator:
    """Same job, same prompt, same output schema. Different provider."""

    def __init__(self, ring: GroqKeyRing | None = None,
                 model: str = DEFAULT_GROQ_MODEL,
                 max_tokens: int = 2048, on_retry=None):
        self.ring = ring or GroqKeyRing(model=model)
        self.model = model
        self.max_tokens = max_tokens
        self.on_retry = on_retry
        # Structured outputs are only on the newer models, and which ones moves.
        # Rather than carry a list that goes stale, try the schema and remember
        # if this model refuses it -- once per process, not once per case.
        self._json_schema_ok = True

    def _create(self, client, prompt: str, use_schema: bool):
        if use_schema:
            fmt = {"type": "json_schema", "json_schema": _schema()}
            # The schema is already in response_format; repeating it in the
            # message body spends ~310 tokens a case to say the same thing
            # twice, and on a free tier the binding limit is tokens per minute
            # rather than requests. Over a 118-case run that is ~37k tokens
            # bought for nothing.
            user = prompt
        else:
            # The json_object format has no schema field, so the shape has to
            # be stated in the message -- and that format additionally requires
            # the word JSON to appear somewhere in the messages at all.
            fmt = {"type": "json_object"}
            user = (f"{prompt}\n\nAnswer with JSON matching this schema:\n"
                    f"{json.dumps(Adjudication.model_json_schema())}")
        return client.chat.completions.create(
            model=self.model,
            messages=[
                # SYSTEM_PROMPT stays byte-identical to the other providers'.
                # Anything this adjudicator needs to add goes in the user turn,
                # because a prompt that differs per provider makes the three
                # sets of numbers incomparable.
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            response_format=fmt,
            max_completion_tokens=self.max_tokens,
            temperature=0.0,
        )

    def adjudicate(self, mandate: Mandate, proposal: Proposal,
                   bounds: BoundsResult) -> AdjudicationResult:
        from groq import APIError, BadRequestError

        fence = Fence.new()
        mandate_block = render_mandate_block(mandate)
        proposal_block, signals = render_proposal_block(proposal, bounds, fence)
        prompt = f"{mandate_block}\n\n{proposal_block}"

        started = time.perf_counter()
        try:
            def call(client):
                if self._json_schema_ok:
                    try:
                        return self._create(client, prompt, use_schema=True)
                    except BadRequestError:
                        # This model does not do structured outputs. Note it and
                        # fall back for the rest of the run; a per-case retry
                        # would double the request count on the tier where
                        # requests are the scarce thing.
                        self._json_schema_ok = False
                return self._create(client, prompt, use_schema=False)

            resp = call_with_rotation(self.ring, call, on_retry=self.on_retry,
                                      api_error=APIError,
                                      classify=groq_classify)
            a = _parsed(resp)
        except Exception as exc:                        # noqa: BLE001
            reason = (
                "every Groq key in the pool has spent its daily quota"
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
        usage = getattr(resp, "usage", None)
        return AdjudicationResult(
            verdict=a.verdict, reason=a.reason, cited_clause=a.cited_clause,
            confidence=a.confidence,
            listing_attempted_instruction=a.listing_attempted_instruction,
            sanitization_signals=signals, latency_ms=elapsed, model=self.model,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0)


def _parsed(resp) -> Adjudication:
    """Get the structured verdict out of a Groq chat completion.

    A genuinely malformed answer is allowed to raise. It reaches the caller as a
    failure, which fails closed to STEP_UP -- coercing it into some default
    would fail open, which is the one direction this system must never fail in.
    """
    choices = getattr(resp, "choices", None) or []
    if not choices:
        raise ValueError("Groq returned no choices")
    content = (getattr(choices[0].message, "content", "") or "").strip()
    if not content:
        raise ValueError("Groq returned an empty verdict "
                         "(likely truncated by max_completion_tokens)")
    return Adjudication.model_validate_json(content)
