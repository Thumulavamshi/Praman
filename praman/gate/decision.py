"""The decision record: what the gate saw, what it decided, and why.

This object is the answer to *"I didn't authorize that, my agent did."* It is
also the thing that has to survive being read six months later by an issuer's
dispute analyst who has no idea what an LLM is.

So it records the inputs, not just the outcome:

  * the mandate hash, so we can prove which version of the delegation applied
  * the proposal hash, including the seller's text **as it was at decision
    time** -- a merchant who edits a listing after the fact cannot rewrite what
    the agent was looking at
  * every deterministic finding, with the clause it enforced
  * the adjudicator's verdict, its cited clause, its reason, and its confidence
  * every sanitization signal, so an attack that was resisted is on the record

Each record carries the previous record's hash. The chain is the same
construction as the event log's, for the same reason: a decision trail that can
be back-dated is not evidence.
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from ..ledger.eventlog import GENESIS, _canonical
from ..mandate.schema import Mandate, Proposal
from .adjudicator import AdjudicationResult
from .bounds import BoundsResult


def _hash(obj) -> str:
    return hashlib.sha256(_canonical(obj).encode("utf-8")).hexdigest()


def mandate_hash(mandate: Mandate) -> str:
    return _hash(mandate.model_dump(exclude_none=True))


def proposal_hash(proposal: Proposal) -> str:
    """Hashes the proposal *including* the seller's raw text.

    Deliberately the raw text, not the sanitized version. The evidentiary claim
    is "this is what the listing said when our agent read it", and normalizing
    before hashing would destroy exactly the detail a dispute turns on.
    """
    return _hash(proposal.model_dump())


@dataclass
class DecisionRecord:
    decision_id: str
    proposal_id: str
    mandate_id: str
    agent: str
    principal: str
    verdict: str                      # ALLOW | BLOCK | STEP_UP
    decided_at: str
    amount: str
    merchant_id: str

    mandate_hash: str = ""
    proposal_hash: str = ""

    bounds_verdict: str = "PASS"
    bounds_findings: list[dict] = field(default_factory=list)

    adjudicator_consulted: bool = False
    adjudicator_verdict: str = ""
    adjudicator_reason: str = ""
    adjudicator_cited_clause: str = ""
    adjudicator_confidence: str = ""
    adjudicator_model: str = ""
    listing_attempted_instruction: bool = False
    sanitization_signals: list[str] = field(default_factory=list)

    reason: str = ""
    cited_clause: str = ""
    latency_ms: float = 0.0
    adjudicator_latency_ms: float = 0.0

    prev_hash: str = GENESIS
    hash: str = ""

    def compute_hash(self) -> str:
        body = {k: v for k, v in asdict(self).items() if k != "hash"}
        return _hash(body)

    def sealed(self) -> "DecisionRecord":
        self.hash = self.compute_hash()
        return self

    def to_dict(self) -> dict:
        return asdict(self)

    def human_summary(self) -> str:
        """What a dispute analyst reads. No model jargon, no hashes."""
        lines = [
            f"{self.verdict} — INR {self.amount} at {self.merchant_id} "
            f"on {self.decided_at}",
            f"Mandate {self.mandate_id}, delegated by {self.principal} "
            f"to {self.agent}.",
            f"Reason: {self.reason}",
        ]
        if self.cited_clause:
            lines.append(f"Clause relied on: {self.cited_clause}")
        blocking = [f for f in self.bounds_findings if f.get("verdict") != "PASS"]
        if blocking:
            lines.append("Deterministic checks that fired:")
            lines += [f"  - {f['clause']}: {f['detail']}" for f in blocking]
        if self.listing_attempted_instruction or self.sanitization_signals:
            lines.append("The seller's listing text attempted to influence this "
                         "decision. It was disregarded; signals recorded: "
                         + ", ".join(self.sanitization_signals or ["(model-detected)"]))
        return "\n".join(lines)


class DecisionChain:
    """An append-only chain of decisions. One per gate."""

    def __init__(self):
        self._records: list[DecisionRecord] = []
        self._by_id: dict[str, DecisionRecord] = {}

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self):
        return iter(self._records)

    @property
    def head_hash(self) -> str:
        return self._records[-1].hash if self._records else GENESIS

    def append(self, rec: DecisionRecord) -> DecisionRecord:
        rec.prev_hash = self.head_hash
        rec.sealed()
        self._records.append(rec)
        self._by_id[rec.decision_id] = rec
        return rec

    def get(self, decision_id: str) -> DecisionRecord | None:
        return self._by_id.get(decision_id)

    def for_proposal(self, proposal_id: str) -> DecisionRecord | None:
        for r in reversed(self._records):
            if r.proposal_id == proposal_id:
                return r
        return None

    def verify(self) -> tuple[bool, str | None]:
        prev = GENESIS
        for r in self._records:
            if r.prev_hash != prev or r.compute_hash() != r.hash:
                return False, r.decision_id
            prev = r.hash
        return True, None
