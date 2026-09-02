"""The authorization gate: bounds first, model second, decision sealed.

The control flow is the design, so it is worth stating plainly:

    1. Verify the mandate's signature and revocation status.
    2. Run every deterministic bound.
    3. If the bounds BLOCK -- stop. Do not call the model. The answer is known,
       and asking would only expose a correct answer to an injection.
    4. If the bounds PASS or STEP_UP, and the case is semantically open, call
       the adjudicator.
    5. Combine, seal the decision into the chain, and return.

Step 3 is the security property. Step 4 is the cost and latency property. Step 5
is the evidence property.

Combining rule, in step 5: **the more conservative answer wins.** A bounds
STEP_UP plus an adjudicator ALLOW is a STEP_UP -- the human said "ask me above
₹1,500" and the model does not get to overrule that. An adjudicator BLOCK on a
bounds PASS is a BLOCK. The model can only ever make the gate more cautious than
the mandate's mechanical reading, never less. That asymmetry is what makes it
safe to put a probabilistic system here at all.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime

from ..ledger.money import money_str
from ..mandate.schema import Mandate, Proposal
from .adjudicator import Adjudicator, AdjudicationResult, StaticAdjudicator
from .bounds import BoundsResult, SpendHistory, check_bounds
from .decision import DecisionChain, DecisionRecord, mandate_hash, proposal_hash

# Conservativeness order. Higher wins when the two layers disagree. The bounds
# checker's PASS and the adjudicator's ALLOW are the same rank -- they are the
# same answer said in the two layers' own vocabularies, and keeping the words
# distinct is deliberate: "the bounds passed" and "this is in scope" are
# different claims and a decision record that conflated them would be harder to
# read back in a dispute, not easier.
# REVIEW ranks with PASS: a review finding imposes no caution of its own, it
# just means the bounds did not settle the question. The adjudicator's answer
# stands on its own there.
_RANK = {"PASS": 0, "ALLOW": 0, "REVIEW": 0, "STEP_UP": 1, "BLOCK": 2}


@dataclass
class GateOutcome:
    decision: DecisionRecord
    bounds: BoundsResult
    adjudication: AdjudicationResult | None

    @property
    def verdict(self) -> str:
        return self.decision.verdict

    @property
    def allowed(self) -> bool:
        return self.decision.verdict == "ALLOW"


class Gate:
    def __init__(self, adjudicator: Adjudicator | None = None,
                 *, issuer=None, chain: DecisionChain | None = None,
                 always_consult: bool = False):
        self.adjudicator = adjudicator or StaticAdjudicator()
        self.issuer = issuer
        self.chain = chain or DecisionChain()
        # When True the model is consulted on every non-BLOCK case. Off by
        # default because it costs latency for nothing on obviously in-scope
        # purchases -- but the eval turns it on, because a per-bucket number that
        # skipped the easy cases would not be comparable across buckets.
        self.always_consult = always_consult

    def decide(self, mandate: Mandate, proposal: Proposal,
               history: SpendHistory | None = None,
               *, revoked: bool = False) -> GateOutcome:
        started = time.perf_counter()

        signature_valid = None
        if self.issuer is not None:
            signature_valid = self.issuer.verify(mandate)

        bounds = check_bounds(mandate, proposal, history,
                              signature_valid=signature_valid, revoked=revoked)

        adjudication: AdjudicationResult | None = None
        if not bounds.decided and (self.always_consult
                                   or self._needs_judgment(mandate, proposal, bounds)):
            adjudication = self.adjudicator.adjudicate(mandate, proposal, bounds)

        verdict, reason, clause = self._combine(bounds, adjudication)

        rec = DecisionRecord(
            decision_id="dec_" + uuid.uuid4().hex[:16],
            proposal_id=proposal.proposal_id,
            mandate_id=mandate.mandate_id,
            agent=proposal.agent,
            principal=mandate.principal,
            verdict=verdict,
            decided_at=datetime.now().astimezone().isoformat(),
            amount=money_str(proposal.total),
            merchant_id=proposal.merchant_id,
            mandate_hash=mandate_hash(mandate),
            proposal_hash=proposal_hash(proposal),
            bounds_verdict=bounds.verdict,
            bounds_findings=[{
                "code": f.code, "clause": f.clause, "verdict": f.verdict,
                "detail": f.detail, "observed": f.observed, "limit": f.limit,
            } for f in bounds.findings],
            adjudicator_consulted=adjudication is not None,
            adjudicator_verdict=adjudication.verdict if adjudication else "",
            adjudicator_reason=adjudication.reason if adjudication else "",
            adjudicator_cited_clause=adjudication.cited_clause if adjudication else "",
            adjudicator_confidence=adjudication.confidence if adjudication else "",
            adjudicator_model=adjudication.model if adjudication else "",
            listing_attempted_instruction=(
                adjudication.listing_attempted_instruction if adjudication else False),
            sanitization_signals=(
                adjudication.sanitization_signals if adjudication else []),
            reason=reason,
            cited_clause=clause,
            adjudicator_latency_ms=adjudication.latency_ms if adjudication else 0.0,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        self.chain.append(rec)
        return GateOutcome(decision=rec, bounds=bounds, adjudication=adjudication)

    # -- policy --------------------------------------------------------------

    def _needs_judgment(self, mandate: Mandate, proposal: Proposal,
                        bounds: BoundsResult) -> bool:
        """Is this case semantically open, or did the bounds settle it?

        Consult the model when any of these hold:
          - a bound came back REVIEW, meaning the checker found something it is
            not entitled to decide alone -- a category outside the compiled
            allowed list, which the taxonomy calls a miss and a person might not
          - the mandate carries soft constraints the compiler could not reduce
            to a bound ("my usual stores", "keep it sensible")
          - the merchant is new or unknown, which is what "usual stores" turns on
          - a step-up threshold fired, so a human might be asked anyway and the
            model's read is worth having on the record
          - the cart is close to a bound rather than comfortably inside it

        Everything else is an ordinary in-scope purchase and does not need a
        model to say so. This is the latency argument made concrete.
        """
        if bounds.reviews:
            return True
        if mandate.scope.soft_constraints:
            return True
        if proposal.merchant_familiarity in ("new", "unknown"):
            return True
        if bounds.step_ups:
            return True
        cap = mandate.scope.cap
        if cap is not None and proposal.total > cap * _NEAR:
            return True
        return False

    def _combine(self, bounds: BoundsResult,
                 adj: AdjudicationResult | None) -> tuple[str, str, str]:
        if bounds.verdict == "BLOCK":
            f = bounds.blocks[0]
            return "BLOCK", f.detail, f.clause

        if adj is None:
            if bounds.verdict == "STEP_UP":
                f = bounds.step_ups[0]
                return "STEP_UP", f.detail, f.clause
            if bounds.verdict == "REVIEW":
                # The checker declined to decide and nobody else did either.
                # Fail closed to a human rather than resolving an open question
                # by default -- a default that resolves open questions is a
                # default that eventually resolves one wrongly.
                f = bounds.reviews[0]
                return "STEP_UP", f.detail, f.clause
            return ("ALLOW",
                    "within every bound the mandate sets, and not a case that "
                    "requires judgment",
                    "per_transaction_cap")

        # The more conservative of the two layers wins. The model can tighten
        # the mandate's mechanical reading; it can never loosen it.
        if _RANK[adj.verdict] >= _RANK[bounds.verdict]:
            return adj.verdict, adj.reason, adj.cited_clause or "(intent)"
        f = (bounds.step_ups or bounds.findings)[0]
        return bounds.verdict, f.detail, f.clause


from decimal import Decimal

# "Close to the cap" means within 25% of it. Chosen so the catalog's deliberate
# near-boundary price bands all land in the consult path; it is a policy dial,
# and the eval reports what it costs.
_NEAR = Decimal("0.75")
