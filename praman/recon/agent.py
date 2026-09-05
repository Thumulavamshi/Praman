"""The reconciliation agent. Sees only the residual, and only in typed answers.

The deterministic matcher has already resolved everything mechanical. What
reaches here is the set of items where matching requires an inference — a payout
split across two credits, a fee that drifted, money that arrived from nobody.

Two constraints shape this, and both are deliberate:

**It answers in a closed vocabulary.** Four kinds, nothing else. An agent that
could write free text would eventually write something plausible and wrong, and
a human would have to read every line to find out which. Four typed shapes are
four things a machine can check.

**It is asked to escalate.** Most reconciliation agents are implicitly rewarded
for closing items, which is exactly backwards: an exception a human never sees
is worse than one they do. The prompt says so, and the metrics report the
escalation rate as its own number rather than as a failure.

Nothing it proposes reaches the book without passing ``verify.py`` first.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from praman.keyring import GeminiKeyRing, call_with_rotation
from praman.ledger.money import money_str
from .models import BankLine, MatchReport, Resolution, SettlementRow

DEFAULT_MODEL = os.getenv("PRAMAN_RECON_MODEL", "gemini-3-flash-preview")


class ProposedResolution(BaseModel):
    """What the model is allowed to say about one exception."""

    ref: str = Field(description="The exception reference this resolves, copied "
                                 "exactly from the list you were given.")
    kind: Literal["match", "split_match", "adjusting_entry", "escalate"]
    reason: str = Field(description="One sentence a reconciliation clerk can "
                                    "read, saying why.")
    settlement_id: str = Field(default="", description="For match/split_match.")
    bank_line_ids: list[str] = Field(default_factory=list,
                                     description="For match/split_match: the "
                                                 "credits, by their exact ids.")
    debit_account: str = Field(default="", description="For adjusting_entry: "
                                                       "the account code to debit.")
    credit_account: str = Field(default="", description="For adjusting_entry: "
                                                        "the account to credit.")
    amount: str = Field(default="0.00", description="For adjusting_entry: a 2dp "
                                                    "rupee string, exactly the "
                                                    "discrepancy shown.")
    memo: str = Field(default="", description="For adjusting_entry: what the "
                                              "line will say in the book.")
    confidence: Literal["low", "medium", "high"]


class Proposals(BaseModel):
    resolutions: list[ProposedResolution] = Field(
        description="One entry per exception you were shown. Do not skip any, "
                    "and do not invent references that were not in the list.")


SYSTEM_PROMPT = """\
You reconcile a merchant's books against a payment gateway's settlement file \
and their bank statement.

A deterministic matcher has already run and resolved everything that could be \
matched by id and amount. What you are shown is the residual: the items where \
matching needs an inference. Your job is to propose a resolution for each, in a \
fixed vocabulary.

  match             one bank credit is this settlement's payout, and the
                    deterministic pass missed it -- usually because the bank
                    mangled the UTR in the description.
  split_match       the payout arrived as SEVERAL credits. Name all of them.
                    Their amounts must sum to the payout exactly.
  adjusting_entry   the gateway kept a different amount than the book expected,
                    and the difference is real and small -- a fee drift. Book it.
                    The amount must be exactly the discrepancy shown to you.
  escalate          you cannot resolve it from what you were given.

RULES THAT ARE ENFORCED, NOT SUGGESTED

Every proposal is checked before it touches the book. A match whose credits do \
not sum EXACTLY to the payout is rejected. An adjusting entry whose amount is \
not a discrepancy the matcher actually found is rejected -- you may resolve a \
real difference, never invent one. An entry naming an account outside the chart \
is rejected. You cannot help by proposing something that looks tidy; you can \
only get it thrown out.

Never claim a bank credit for two different settlements. That is the same money \
counted twice, and it is the error that makes a reconciliation worse than not \
doing one.

ESCALATE IS A GOOD ANSWER

Missing money and unexplained money both belong to a human. A settlement the \
gateway says it paid with no matching credit is not a puzzle to solve with an \
adjusting entry -- it is either the gateway's error or a real shortfall, and \
booking around it hides the one thing the merchant needs to see. The same goes \
for a duplicated credit and for money arriving from a remitter nobody can \
identify.

You are not scored on how many items you close. An exception a human never sees \
is worse than one they do.

CHART OF ACCOUNTS

1100 Bank · 1200 PG Settlement Receivable · 1300 Reserve Held at PG
1400 GST Input Credit · 1500 TDS Receivable · 1600 GST TCS Credit
2100 Refunds Payable · 2200 GST Output Payable · 2300 Chargeback Provision
4000 Sales Revenue · 5000 Payment Gateway Fees (MDR) · 5100 Chargeback Losses

A fee the gateway kept beyond what we booked is more expense: debit 5000, \
credit 1200. A fee it kept LESS of is the reverse.\
"""


@dataclass
class ReconResult:
    proposals: list[Resolution] = field(default_factory=list)
    refs: list[str] = field(default_factory=list)
    latency_ms: float = 0.0
    model: str = ""
    error: str = ""


def _render(engine, report: MatchReport, rows: list[SettlementRow],
            lines: list[BankLine]) -> str:
    """The residual, as compact evidence. Only what the agent needs to decide."""
    from .matcher import _expected_payout
    from collections import defaultdict

    by_settlement = defaultdict(list)
    for r in rows:
        by_settlement[r.settlement_id].append(r)

    blocks = ["EXCEPTIONS TO RESOLVE", ""]
    for e in report.exceptions:
        blocks.append(f"  ref={e.ref}  kind={e.kind}  "
                      f"amount=INR {money_str(e.amount)}")
        blocks.append(f"    {e.detail}")
    blocks += ["", "UNMATCHED SETTLEMENTS (what the gateway says it paid out)", ""]
    for sid in report.unmatched_settlements:
        srows = by_settlement.get(sid, [])
        blocks.append(f"  {sid}  payout=INR "
                      f"{money_str(_expected_payout(engine, sid, srows))}  "
                      f"payments={len(srows)}  "
                      f"utr={next((r.utr for r in srows if r.utr), '?')}")
    blocks += ["", "UNMATCHED BANK CREDITS (what actually landed)", ""]
    for l in report.unmatched_bank:
        blocks.append(f"  {l.line_id}  INR {money_str(l.amount)}  "
                      f"{l.value_date}  \"{l.description[:60]}\"")
    return "\n".join(blocks)


class ReconAgent:
    def __init__(self, ring: GeminiKeyRing | None = None,
                 model: str = DEFAULT_MODEL):
        self.model = model
        self.ring = ring or GeminiKeyRing(
            rpm_per_key=float(os.getenv("PRAMAN_GEMINI_RPM", "5")), model=model)

    def propose(self, engine, report: MatchReport, rows: list[SettlementRow],
                lines: list[BankLine]) -> ReconResult:
        from google.genai import types

        out = ReconResult(model=self.model,
                          refs=[e.ref for e in report.exceptions])
        if not report.exceptions:
            return out
        if self.ring.live == 0:
            out.error = f"no key has budget left for {self.model} today"
            return out

        started = time.perf_counter()
        prompt = _render(engine, report, rows, lines)
        cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=Proposals,
            # Serialisation, not judgment -- the reasoning happens before the
            # JSON. Leaving thinking on spends the output budget and truncates
            # the answer, which was a real failure in the dispute defender.
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
            max_output_tokens=8192)

        def emit(c):
            chunks = []
            for chunk in c.models.generate_content_stream(
                    model=self.model, contents=prompt, config=cfg):
                if getattr(chunk, "text", None):
                    chunks.append(chunk.text)
            return "".join(chunks)

        try:
            raw = call_with_rotation(self.ring, emit, max_attempts=5)
            parsed = Proposals.model_validate_json(raw.strip())
        except Exception as exc:                        # noqa: BLE001
            out.latency_ms = (time.perf_counter() - started) * 1000
            out.error = f"{exc.__class__.__name__}: {exc}"
            return out

        out.latency_ms = (time.perf_counter() - started) * 1000
        for r in parsed.resolutions:
            out.proposals.append(Resolution(
                kind=r.kind, reason=f"[{r.ref}] {r.reason}",
                settlement_id=r.settlement_id,
                bank_line_ids=list(r.bank_line_ids),
                debit_account=r.debit_account, credit_account=r.credit_account,
                amount=r.amount, memo=r.memo, confidence=r.confidence))
        return out
