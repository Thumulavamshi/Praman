"""The mandate compiler, on Gemini. Same prompt, same schema, same assembly.

``build_mandate`` is shared with the Claude path, so ids, timestamps and
validity bounds are still assigned by us and never by the model -- a mandate_id
chosen by prompt is a mandate_id an attacker can choose, and that is true
whichever provider is answering.
"""
from __future__ import annotations

import os
from datetime import datetime

from ..keyring import GeminiKeyRing, call_with_rotation
from .compiler import (CompiledMandate, KNOWN_CATEGORIES, SYSTEM_PROMPT,
                       build_mandate)
from .schema import IST, Mandate

DEFAULT_GEMINI_MODEL = os.getenv("PRAMAN_GEMINI_MODEL", "gemini-2.5-flash")


def compile_mandate_gemini(
    delegation: str, *, principal: str, agent: str,
    ring: GeminiKeyRing | None = None,
    model: str = DEFAULT_GEMINI_MODEL,
    thinking_budget: int | None = 1024,
    issued_at: datetime | None = None,
) -> tuple[Mandate, CompiledMandate]:
    from google.genai import types

    ring = ring or GeminiKeyRing(
        rpm_per_key=float(os.getenv("PRAMAN_GEMINI_RPM", "10")))
    issued = issued_at or datetime.now(IST)

    cfg_kwargs = dict(
        system_instruction=SYSTEM_PROMPT,
        response_mime_type="application/json",
        response_schema=CompiledMandate,
        # Compilation emits a full mandate plus 6-10 acceptance cases, so it
        # needs materially more room than an adjudication does.
        max_output_tokens=8192,
    )
    if thinking_budget is not None:
        cfg_kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=thinking_budget)
    config = types.GenerateContentConfig(**cfg_kwargs)

    prompt = (f"Known categories: {', '.join(KNOWN_CATEGORIES)}\n\n"
              f"The delegation, exactly as the person said it:\n"
              f"\"{delegation}\"")

    def call(client):
        return client.models.generate_content(model=model, contents=prompt,
                                              config=config)

    resp = call_with_rotation(ring, call)
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, CompiledMandate):
        c = parsed
    elif isinstance(parsed, dict):
        c = CompiledMandate.model_validate(parsed)
    else:
        c = CompiledMandate.model_validate_json((resp.text or "").strip())

    return build_mandate(c, delegation=delegation, principal=principal,
                         agent=agent, issued=issued), c
