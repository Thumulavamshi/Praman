"""Assembling the evidence bundle. Every number quoted, never generated.

This is what a merchant sends an issuer when the cardholder says *"I didn't
authorize that, my agent did."* The claim is not a lie -- the consumer really
did delegate, and really does retain chargeback rights -- so the defence cannot
be "they authorized it". The defence has to be:

    Here is the delegation, signed. Here is the cart the agent proposed. Here is
    the decision, what it enforced, and what it cited. Here is the capture. Here
    is a hash chain showing none of it was written after the dispute arrived.

The bundle carries no computed figure that is not lifted from the ledger, and
every claim names the event id it came from. That constraint is the whole point:
a dispute packet with a number in it that nobody can trace is worse than no
packet, because it invites the issuer to check.

The as-of snapshot is what makes it evidence rather than assertion. It answers
"what did your book say at the moment of the disputed transaction", not "what
does it say now, after four refunds and a settlement".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from praman.gate.decision import DecisionRecord
from praman.ledger.money import ZERO, money_str


@dataclass
class Citation:
    """One factual claim and the record it is lifted from."""
    claim: str
    value: str
    source_kind: str          # event | decision | mandate | ledger
    source_id: str

    def __str__(self) -> str:
        return f"{self.claim}: {self.value}  [{self.source_kind} {self.source_id}]"


@dataclass
class EvidenceBundle:
    payment_id: str
    chargeback_id: str = ""
    citations: list[Citation] = field(default_factory=list)
    decision: DecisionRecord | None = None
    mandate: dict = field(default_factory=dict)
    event_ids: list[str] = field(default_factory=list)
    log_head_hash: str = ""
    decision_chain_head: str = ""
    chain_verified: bool = False
    gaps: list[str] = field(default_factory=list)

    def cite(self, claim, value, kind, source_id) -> None:
        self.citations.append(Citation(claim, str(value), kind, source_id))

    @property
    def completeness(self) -> float:
        """Fraction of the things a representment needs that we actually have.

        Reported as a number rather than a badge because an incomplete bundle is
        a normal outcome -- a payment made before the mandate system existed has
        no decision record, and saying so is better than filing a packet that
        implies one.
        """
        required = 6
        have = required - len(self.gaps)
        return max(0.0, have / required)

    def render(self) -> str:
        lines = [
            "REPRESENTMENT EVIDENCE PACKET",
            "=" * 74,
            f"payment            {self.payment_id}",
            f"chargeback         {self.chargeback_id or '(none)'}",
            f"evidence complete  {self.completeness * 100:.0f}%",
            f"chain verified     {self.chain_verified}",
            "",
            "FACTS, EACH QUOTED FROM A RECORD",
            "-" * 74,
        ]
        lines += [f"  {c}" for c in self.citations]
        if self.decision:
            lines += ["", "THE AUTHORIZATION DECISION", "-" * 74]
            lines += ["  " + l for l in self.decision.human_summary().splitlines()]
        lines += ["", "INTEGRITY", "-" * 74,
                  f"  event log head      {self.log_head_hash[:32]}...",
                  f"  decision chain head {self.decision_chain_head[:32]}...",
                  f"  events cited        {len(self.event_ids)}"]
        if self.gaps:
            lines += ["", "GAPS — stated rather than papered over", "-" * 74]
            lines += [f"  - {g}" for g in self.gaps]
        return "\n".join(lines)


def build_bundle(engine, gate, payment_id: str,
                 chargeback_id: str = "") -> EvidenceBundle:
    """Reconstruct the full authorization trail for one payment.

    Reads only from the ledger and the decision chain. Nothing here computes a
    figure from scratch; where a number is needed it is read from the record
    that already holds it, and where a record is missing that is a gap, not a
    number to estimate.
    """
    b = EvidenceBundle(payment_id=payment_id, chargeback_id=chargeback_id)
    state = engine.state
    rec = state.payments.get(payment_id)
    if rec is None:
        b.gaps.append(f"no capture recorded for {payment_id}")
        return b

    capture_event = next(
        (e for e in engine.eventlog
         if e.type == "payment_captured"
         and e.payload.get("payment_id") == payment_id), None)

    # The book as it stood at the disputed capture, not as it stands today.
    snapshot = (engine.snapshot_as_of(capture_event.event_id)
                if capture_event else state)

    if capture_event:
        b.event_ids.append(capture_event.event_id)
        b.cite("amount captured", f"INR {money_str(rec.amount)}",
               "event", capture_event.event_id)
        b.cite("captured at", rec.captured_at, "event", capture_event.event_id)
        b.cite("instrument", rec.instrument, "event", capture_event.event_id)
        b.cite("merchant", rec.merchant_id, "event", capture_event.event_id)
    else:
        b.gaps.append("no payment_captured event found")

    # --- the mandate -------------------------------------------------------
    mandate = state.mandates.get(rec.mandate_id)
    if mandate:
        b.mandate = mandate
        scope = mandate.get("scope", {})
        b.cite("mandate", rec.mandate_id, "mandate", rec.mandate_id)
        b.cite("delegated by", mandate.get("principal", "?"),
               "mandate", rec.mandate_id)
        b.cite("delegated to", mandate.get("agent", "?"),
               "mandate", rec.mandate_id)
        b.cite("mandate valid from", mandate.get("issued_at", "?"),
               "mandate", rec.mandate_id)
        b.cite("mandate valid until", mandate.get("expires_at", "?"),
               "mandate", rec.mandate_id)
        if scope.get("per_transaction_cap"):
            b.cite("per-transaction cap the human set",
                   f"INR {scope['per_transaction_cap']}", "mandate",
                   rec.mandate_id)
        if mandate.get("source_text"):
            b.cite("the delegation in the human's own words",
                   f"\"{mandate['source_text']}\"", "mandate", rec.mandate_id)
        if mandate.get("signature"):
            b.cite("mandate signature", mandate["signature"][:28] + "...",
                   "mandate", rec.mandate_id)
        else:
            b.gaps.append("mandate carries no signature")
    else:
        b.gaps.append(f"mandate {rec.mandate_id or '<none>'} is not in the log")

    # --- the proposal ------------------------------------------------------
    proposal = state.proposals.get(rec.proposal_id)
    if proposal:
        b.cite("agent proposed", rec.proposal_id, "event", rec.proposal_id)
        b.cite("items the agent saw",
               ", ".join(f"{i['sku']} \"{i['name']}\""
                         for i in proposal.get("items", [])),
               "event", rec.proposal_id)
    else:
        b.gaps.append("no agent_purchase_proposed event for this payment")

    # --- the decision ------------------------------------------------------
    decision = gate.chain.get(rec.decision_id) if rec.decision_id else None
    if decision:
        b.decision = decision
        b.cite("gate verdict", decision.verdict, "decision", decision.decision_id)
        b.cite("clause relied on", decision.cited_clause, "decision",
               decision.decision_id)
        b.cite("decided at", decision.decided_at, "decision", decision.decision_id)
        b.cite("hash of the listing as the agent saw it",
               decision.proposal_hash[:24] + "...", "decision",
               decision.decision_id)
        if decision.adjudicator_consulted:
            b.cite("intent adjudicator", decision.adjudicator_model, "decision",
                   decision.decision_id)
        if decision.sanitization_signals:
            b.cite("manipulation attempts in the seller's listing, resisted",
                   ", ".join(decision.sanitization_signals), "decision",
                   decision.decision_id)
    else:
        b.gaps.append("no gate decision record for this payment")

    # --- the money, as of the capture --------------------------------------
    b.cite("PG settlement receivable at the moment of capture",
           f"INR {money_str(snapshot.balance('1200'))}", "ledger",
           capture_event.event_id if capture_event else "n/a")
    if rec.settlement_id:
        s = state.settlements.get(rec.settlement_id, {})
        b.cite("settled in payout", rec.settlement_id, "ledger", rec.settlement_id)
        if s.get("utr"):
            b.cite("bank UTR", s["utr"], "ledger", rec.settlement_id)
    if rec.refunded > ZERO:
        b.cite("refunded since", f"INR {money_str(rec.refunded)}", "ledger",
               payment_id)

    for e in engine.eventlog:
        if e.payload.get("payment_id") == payment_id and e.event_id not in b.event_ids:
            b.event_ids.append(e.event_id)

    chain_ok, _ = engine.eventlog.verify_chain()
    dec_ok, _ = gate.chain.verify()
    b.chain_verified = chain_ok and dec_ok
    b.log_head_hash = engine.eventlog.head_hash
    b.decision_chain_head = gate.chain.head_hash
    if not b.chain_verified:
        b.gaps.append("hash chain does not verify — records may have been altered")
    return b
