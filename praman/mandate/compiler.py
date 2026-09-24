"""Natural-language delegation -> typed, machine-checkable mandate.

    "You can order my groceries, keep it under 2k a week, nothing from liquor
     stores, and don't wake me up at 3am about it"

becomes a ``Mandate`` with a period cap, a denied category, and a time window.

Two design commitments here, and the second is the one that matters.

**The compiler must not invent bounds.** A cap the human did not state is a cap
that will block a purchase they wanted, and they will never know why. Where the
delegation is silent, the field stays null. "Under 2k a week" is a period cap and
*not* a per-transaction cap, however tempting the inference.

**What cannot be reduced to a bound is not discarded.** "My usual stores" and
"keep it sensible" are real constraints that no schema field expresses. Dropping
them would silently widen the mandate -- the human said something restrictive and
the system would behave as though they had not. So they are carried into
``scope.soft_constraints`` and handed to the adjudicator as part of the delegated
intent. This is the seam between the deterministic and the semantic layers, and
putting it here rather than hiding it is most of why the split works.

Every compiled mandate ships with generated acceptance tests: concrete purchases
the human would expect to be allowed and refused. A compiler that produces a
plausible-looking mandate nobody has checked is worse than no compiler, because
it looks like it worked.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from .schema import IST, Mandate, Scope

DEFAULT_MODEL = os.getenv("PRAMAN_MODEL", "claude-opus-5")

KNOWN_CATEGORIES = [
    "groceries", "household", "appliances", "electronics", "personal_care",
    "pharmacy", "baby_care", "pet_supplies", "apparel", "gifting", "services",
    "alcohol", "tobacco",
]


class AcceptanceCase(BaseModel):
    """One purchase the human would expect a specific answer on."""
    description: str = Field(description="A concrete purchase, in plain words.")
    amount: str = Field(description="Rupee amount as a 2dp string.")
    category: str = Field(description="One of the known categories.")
    expected: Literal["ALLOW", "BLOCK", "STEP_UP"]
    why: str = Field(description="Which part of the delegation decides it.")


class CompiledMandate(BaseModel):
    """What the model returns. Deliberately not a Mandate: ids and timestamps
    are ours to assign, and letting the model mint a mandate_id would make the
    whole record forgeable by prompt."""

    categories_allowed: list[str] = Field(
        description="Categories the delegation permits, from the known list. "
                    "Use ['*'] only if the human really did say anything goes.")
    categories_denied: list[str] = Field(
        description="Categories explicitly refused. Empty if none were.")
    merchants_allowed: list[str] = Field(
        description="Merchant ids if the human named specific stores, else ['*']. "
                    "A phrase like 'my usual stores' is NOT a merchant list -- it "
                    "goes in soft_constraints.")
    per_transaction_cap: str | None = Field(
        description="Rupee cap per purchase as a 2dp string, ONLY if the human "
                    "stated a per-purchase limit. Null otherwise.")
    period_cap_amount: str | None = Field(
        description="Rupee cap across a period, ONLY if stated. Null otherwise.")
    period_cap_window: str | None = Field(
        description="ISO-8601 duration for the period cap: P7D, P1D, P30D, PT24H.")
    velocity_max_txns: int | None = Field(
        description="Maximum number of transactions in the velocity window, "
                    "ONLY if the human limited how often. Null otherwise.")
    velocity_window: str | None = Field(description="ISO-8601 duration, e.g. P7D.")
    time_start: str | None = Field(description="HH:MM, only if hours were stated.")
    time_end: str | None = Field(description="HH:MM, only if hours were stated.")
    requires_step_up_above: str | None = Field(
        description="Amount above which the human wants to be asked first, only "
                    "if they said so. Null otherwise.")
    soft_constraints: list[str] = Field(
        description="Every restriction the human stated that does NOT reduce to "
                    "one of the fields above, quoted or closely paraphrased. "
                    "'My usual stores', 'nothing fancy', 'only what we normally "
                    "buy'. Never drop one of these.")
    validity_days: int = Field(
        description="How long the delegation should last, in days. Use what the "
                    "human said; if they said nothing, use 90.")
    interpretation_notes: str = Field(
        description="Anything ambiguous in the delegation, and how you read it. "
                    "This is shown to the human for confirmation.")
    acceptance_cases: list[AcceptanceCase] = Field(
        description="6 to 10 concrete purchases testing the boundaries of this "
                    "mandate. Include at least one that should be blocked by "
                    "each restriction the human stated, and at least one "
                    "ordinary purchase that should be allowed.")


SYSTEM_PROMPT = """\
You compile a spoken delegation of purchasing authority into a formal mandate \
that a payment gate can enforce mechanically.

The person speaking is handing an AI shopping agent the ability to spend their \
money. Your output decides what the agent can and cannot do with it.

Two failure modes, and they are not symmetric.

Inventing a restriction they did not state means their agent refuses purchases \
they wanted, and they will never learn why. Dropping a restriction they DID \
state means their agent spends money they did not authorise. The second is \
worse, but the first is the one that quietly makes the system useless.

So:

- Every field is null unless the delegation actually supports it. Do not infer \
a per-transaction cap from a weekly one, or a time window from "don't bother me".
- Distinguish per-transaction from per-period carefully. "Under 2k a week" is a \
period cap of 2000 over P7D and says NOTHING about a single purchase.
- Anything restrictive that does not fit a field goes in soft_constraints, \
quoted closely. "My usual stores", "nothing extravagant", "only the basics" are \
real limits that the semantic layer will enforce. Never silently drop one -- \
dropping it widens the mandate.
- Categories come from the known list you are given. If the person names \
something outside it, put it in soft_constraints rather than inventing a category.
- A denied category and an allowed category list can both be present. Denied wins.

Then write acceptance cases: concrete purchases, with the answer the person \
themselves would give. These are how the compilation gets checked, so make them \
sharp -- test each restriction, and include realistic amounts near the \
boundaries rather than obviously-over ones.\
"""


def compile_mandate(
    delegation: str,
    *,
    principal: str,
    agent: str,
    client=None,
    model: str = DEFAULT_MODEL,
    effort: str = "high",
    issued_at: datetime | None = None,
) -> tuple[Mandate, CompiledMandate]:
    """Compile a delegation. Returns the mandate and the model's full output.

    The second return value carries the acceptance cases and the interpretation
    notes, which is what gets shown back to the human for confirmation. A
    compiler whose output nobody confirms is a compiler that quietly rewrote
    someone's intent.
    """
    if client is None:
        import anthropic
        client = anthropic.Anthropic()

    issued = issued_at or datetime.now(IST)

    resp = client.messages.parse(
        model=model,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"effort": effort},
        system=[{"type": "text", "text": SYSTEM_PROMPT,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content":
                   f"Known categories: {', '.join(KNOWN_CATEGORIES)}\n\n"
                   f"The delegation, exactly as the person said it:\n"
                   f"\"{delegation}\""}],
        output_format=CompiledMandate,
    )
    c: CompiledMandate = resp.parsed_output
    return build_mandate(c, delegation=delegation, principal=principal,
                         agent=agent, issued=issued), c


def build_mandate(c: CompiledMandate, *, delegation: str, principal: str,
                  agent: str, issued: datetime) -> Mandate:
    """Assemble the Mandate from the compiled fields.

    The ids, timestamps and validity bounds are set here rather than by the
    model. A mandate_id chosen by prompt is a mandate_id an attacker can choose.
    """
    scope_kwargs: dict = {
        "categories_allowed": c.categories_allowed or ["*"],
        "categories_denied": c.categories_denied,
        "merchants_allowed": c.merchants_allowed or ["*"],
        "per_transaction_cap": c.per_transaction_cap,
        "requires_step_up_above": c.requires_step_up_above,
        "soft_constraints": c.soft_constraints,
    }
    if c.period_cap_amount:
        scope_kwargs["period_cap"] = {"amount": c.period_cap_amount,
                                      "window": c.period_cap_window or "P7D"}
    if c.velocity_max_txns:
        scope_kwargs["velocity"] = {"max_txns": c.velocity_max_txns,
                                    "window": c.velocity_window or "P7D"}
    if c.time_start and c.time_end:
        scope_kwargs["time_window"] = {"start": c.time_start, "end": c.time_end,
                                       "tz": "Asia/Kolkata"}

    return Mandate(
        mandate_id="mnd_" + uuid.uuid4().hex[:16],
        principal=principal,
        agent=agent,
        scope=Scope(**scope_kwargs),
        issued_at=issued.isoformat(),
        expires_at=(issued + timedelta(days=max(1, c.validity_days))).isoformat(),
        source_text=delegation,
    )
