"""The path an agent-initiated purchase actually takes.

    propose -> adjudicate -> (capture) -> book -> seal

Every step emits an event, including the ones that move no money. The ordering
is the safety property and it is enforced structurally, not by convention:

  * The proposal is logged **before** the decision, so a decision always has the
    cart it was made about.
  * The decision is logged **before** the capture, so the ledger's
    ``every_capture_cites_a_decision`` invariant can refuse a capture whose
    decision is missing. That invariant is why the AI is allowed near the money
    path: the verifier can refuse the agent, and it does so by construction
    rather than by trusting this file to call things in the right order.
  * The gateway is called **after** the gate and only on ALLOW. A BLOCK never
    reaches Razorpay at all, which is the difference between a gate and a
    reconciliation report.

A STEP_UP does not capture. It records the decision and returns, leaving the
purchase pending a human. ``approve_step_up`` is the resume path; it records a
second decision citing the first, so the audit trail shows a human intervened
rather than the gate quietly changing its mind.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from .evidence.chain import EvidenceBundle, build_bundle
from .gate.bounds import SpendHistory
from .gate.gate import Gate, GateOutcome
from .ledger.engine import ApplyResult, Engine
from .ledger.fees import compute_fees
from .ledger.money import money, money_str
from .mandate.schema import Mandate, Proposal
from .pg.interface import PaymentGateway


@dataclass
class PurchaseOutcome:
    verdict: str
    decision_id: str
    proposal_id: str
    payment_id: str = ""
    order_id: str = ""
    reason: str = ""
    cited_clause: str = ""
    captured_amount: str = "0.00"
    ledger_results: list = field(default_factory=list)
    gateway_error: str = ""

    @property
    def captured(self) -> bool:
        return bool(self.payment_id) and self.verdict in ("ALLOW",
                                                          "STEP_UP_APPROVED")


class Praman:
    """Gate, ledger and gateway wired into one object.

    Holds the mandates it has issued so that spend history for the period cap
    and velocity checks comes from the ledger rather than from a tally kept
    alongside it. A second source of truth about how much has been spent is a
    second answer to the only question the cap asks.
    """

    def __init__(self, gate: Gate, engine: Engine, gateway: PaymentGateway):
        self.gate = gate
        self.engine = engine
        self.gateway = gateway
        self._mandates: dict[str, Mandate] = {}
        self._revoked: set[str] = set()

    # -- mandates ------------------------------------------------------------

    def register_mandate(self, mandate: Mandate) -> ApplyResult:
        self._mandates[mandate.mandate_id] = mandate
        return self.engine.apply(
            f"ev_mnd_{mandate.mandate_id}", "mandate_created",
            mandate.issued_at, {"mandate": mandate.model_dump(exclude_none=True)})

    def revoke_mandate(self, mandate_id: str, at: str | None = None) -> ApplyResult:
        self._revoked.add(mandate_id)
        return self.engine.apply(
            f"ev_rev_{mandate_id}", "mandate_revoked",
            at or datetime.now().astimezone().isoformat(),
            {"mandate_id": mandate_id})

    def spend_history(self, mandate_id: str) -> SpendHistory:
        """Prior spend under this mandate, read from the book.

        Refunded amounts are netted out: a purchase that was refunded did not
        consume the weekly cap, and counting it would block purchases the human
        is still entitled to make.
        """
        txns = []
        for pid, rec in self.engine.state.payments.items():
            if rec.mandate_id != mandate_id or not rec.captured_at:
                continue
            net = money(rec.amount - rec.refunded)
            if net > 0:
                txns.append((datetime.fromisoformat(rec.captured_at), net))
        return SpendHistory(txns)

    # -- the purchase path ---------------------------------------------------

    def purchase(self, proposal: Proposal) -> PurchaseOutcome:
        mandate = self._mandates.get(proposal.mandate_id)
        if mandate is None:
            return PurchaseOutcome(
                verdict="BLOCK", decision_id="", proposal_id=proposal.proposal_id,
                reason=f"no mandate {proposal.mandate_id} is registered with this "
                       f"gate; a purchase with no delegation behind it is not an "
                       f"agent purchase, it is an unauthorized one",
                cited_clause="mandate_id")

        # 0. the agent-facing seen-gate. Agents retry aggressively, and a
        # retried purchase() previously minted a fresh decision, a fresh order
        # and a SECOND capture -- a real double charge, in the one place the
        # project claims retry safety loudest.
        #
        # The ledger's seen-gate cannot catch this: the second attempt arrives
        # with a new payment id, so it is a genuinely new event. Idempotency has
        # to be keyed on what the agent repeats, which is the proposal.
        #
        # Only a captured proposal short-circuits. A BLOCK or STEP_UP is
        # re-decided on purpose: a refund may have freed the period cap, or a
        # human may have approved in between, and pinning the old refusal would
        # make the gate stale.
        prior = next((r for r in self.engine.state.payments.values()
                      if r.proposal_id == proposal.proposal_id), None)
        if prior is not None:
            return PurchaseOutcome(
                verdict="ALLOW", decision_id=prior.decision_id,
                proposal_id=proposal.proposal_id, payment_id=prior.payment_id,
                order_id=prior.order_id,
                captured_amount=money_str(prior.amount),
                reason=f"proposal {proposal.proposal_id} was already captured as "
                       f"{prior.payment_id}; returning that rather than charging "
                       f"again",
                cited_clause="idempotent_retry")

        results = []

        # 1. the cart the agent proposed, logged before anything judges it
        results.append(self.engine.apply(
            f"ev_prop_{proposal.proposal_id}", "agent_purchase_proposed",
            proposal.proposed_at, {"proposal": proposal.model_dump()}))

        # 2. the decision
        outcome: GateOutcome = self.gate.decide(
            mandate, proposal, self.spend_history(mandate.mandate_id),
            revoked=mandate.mandate_id in self._revoked)
        d = outcome.decision
        results.append(self.engine.apply(
            f"ev_dec_{d.decision_id}", "gate_decided", d.decided_at,
            {"decision": d.to_dict()}))

        out = PurchaseOutcome(
            verdict=d.verdict, decision_id=d.decision_id,
            proposal_id=proposal.proposal_id, reason=d.reason,
            cited_clause=d.cited_clause, ledger_results=results)

        if d.verdict != "ALLOW":
            # A BLOCK never reaches the gateway. Neither does a STEP_UP: it is
            # waiting on a human, and a purchase waiting on a human is not a
            # purchase that has been made.
            return out

        return self._capture(proposal, d.decision_id, out)

    def approve_step_up(self, proposal: Proposal, decision_id: str,
                        approver: str) -> PurchaseOutcome:
        """A human said yes to a deferred purchase.

        Recorded as its own decision citing the original, so the trail shows a
        person intervened. Overwriting the STEP_UP would make the record say the
        gate allowed it, which is exactly the fact a dispute turns on.
        """
        original = self.gate.chain.get(decision_id)
        if original is None or original.verdict != "STEP_UP":
            return PurchaseOutcome(
                verdict="BLOCK", decision_id=decision_id,
                proposal_id=proposal.proposal_id,
                reason=f"decision {decision_id} is not an open step-up",
                cited_clause="requires_step_up_above")

        from .gate.decision import DecisionRecord
        rec = DecisionRecord(
            decision_id="dec_" + uuid.uuid4().hex[:16],
            proposal_id=proposal.proposal_id, mandate_id=proposal.mandate_id,
            agent=proposal.agent, principal=original.principal,
            verdict="STEP_UP_APPROVED",
            decided_at=datetime.now().astimezone().isoformat(),
            amount=money_str(proposal.total), merchant_id=proposal.merchant_id,
            mandate_hash=original.mandate_hash,
            proposal_hash=original.proposal_hash,
            bounds_verdict=original.bounds_verdict,
            bounds_findings=original.bounds_findings,
            reason=f"{approver} approved the step-up raised by {decision_id}: "
                   f"{original.reason}",
            cited_clause=original.cited_clause)
        self.gate.chain.append(rec)

        out = PurchaseOutcome(verdict="STEP_UP_APPROVED",
                              decision_id=rec.decision_id,
                              proposal_id=proposal.proposal_id,
                              reason=rec.reason, cited_clause=rec.cited_clause)
        out.ledger_results.append(self.engine.apply(
            f"ev_dec_{rec.decision_id}", "gate_decided", rec.decided_at,
            {"decision": rec.to_dict()}))
        return self._capture(proposal, rec.decision_id, out)

    def _capture(self, proposal: Proposal, decision_id: str,
                 out: PurchaseOutcome) -> PurchaseOutcome:
        """Take the money and book it. Only ever reached on an allowing verdict."""
        try:
            order = self.gateway.create_order(
                proposal.total, receipt=proposal.proposal_id,
                notes={"mandate_id": proposal.mandate_id,
                       "decision_id": decision_id,
                       "agent": proposal.agent})
            payment = self.gateway.simulate_payment(order, proposal.instrument)
            payment = self.gateway.capture_payment(payment.id, proposal.total)
        except Exception as exc:                       # noqa: BLE001
            # A gateway failure is not a ledger event. Booking a capture that
            # did not happen is how a book stops matching reality.
            out.gateway_error = f"{exc.__class__.__name__}: {exc}"
            out.reason = f"{out.reason} — gateway refused: {out.gateway_error}"
            return out

        out.order_id = order.id
        out.payment_id = payment.id
        out.captured_amount = money_str(payment.rupees)

        cat = proposal.items[0].category if proposal.items else ""
        out.ledger_results.append(self.engine.apply(
            f"ev_cap_{payment.id}", "payment_captured",
            datetime.now().astimezone().isoformat(),
            {"payment_id": payment.id, "order_id": order.id,
             "amount": money_str(payment.rupees), "currency": payment.currency,
             "instrument": proposal.instrument,
             "merchant_id": proposal.merchant_id, "category": cat,
             "mandate_id": proposal.mandate_id,
             "proposal_id": proposal.proposal_id, "decision_id": decision_id}))

        fees = compute_fees(payment.rupees, proposal.instrument)
        out.ledger_results.append(self.engine.apply(
            f"ev_fee_{payment.id}", "fee_debited",
            datetime.now().astimezone().isoformat(),
            {"payment_id": payment.id, "mdr": money_str(fees.mdr)}))
        return out

    # -- reversals and disputes ---------------------------------------------

    def refund(self, payment_id: str, amount, reason: str = "") -> list:
        r = self.gateway.refund(payment_id, amount, notes={"reason": reason})
        now = datetime.now().astimezone().isoformat()
        return [
            self.engine.apply(f"ev_rfi_{r.id}", "refund_initiated", now,
                              {"refund_id": r.id, "payment_id": payment_id,
                               "amount": money_str(money(amount)),
                               "reason": reason}),
            self.engine.apply(f"ev_rfs_{r.id}", "refund_settled", now,
                              {"refund_id": r.id}),
        ]

    def raise_chargeback(self, payment_id: str, reason_code: str,
                         amount=None) -> ApplyResult:
        cid = "cb_" + uuid.uuid4().hex[:12]
        rec = self.engine.state.payments.get(payment_id)
        return self.engine.apply(
            f"ev_cbr_{cid}", "chargeback_raised",
            datetime.now().astimezone().isoformat(),
            {"chargeback_id": cid, "payment_id": payment_id,
             "amount": money_str(amount if amount is not None
                                 else (rec.amount if rec else 0)),
             "reason_code": reason_code})

    def defend(self, payment_id: str, chargeback_id: str = "") -> EvidenceBundle:
        return build_bundle(self.engine, self.gate, payment_id, chargeback_id)
