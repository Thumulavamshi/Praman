"""Read-only investigation tools the dispute defender is given.

Every one returns facts already in the ledger or the decision chain. None of
them computes anything the book does not already hold, and none of them can
write. That is deliberate and it is the whole safety story for this phase: an
agent investigating a dispute has no reason to mutate state, so it is given no
way to.

The tool surface is small on purpose. A dispute has exactly four questions
behind it -- what was delegated, what did the agent propose, what did the gate
decide, and what did the money do -- and there is one tool per question. Giving
the model a general "query the ledger" tool would be more flexible and much
worse: it would let the model construct figures rather than quote them, which is
the one thing the representment packet may not do.
"""
from __future__ import annotations

from praman.ledger.money import money_str


class DisputeTools:
    """Bound to one engine and one gate. Instantiated per investigation."""

    def __init__(self, engine, gate):
        self.engine = engine
        self.gate = gate
        self.calls: list[dict] = []          # an audit trail of the investigation

    def _record(self, name: str, args: dict, ok: bool, result=None) -> None:
        """Log the call AND keep its result.

        Keeping the result matters: the citation verifier needs the exact
        records the investigation saw, and re-running the tools to rebuild them
        would (a) let the ledger change underneath and (b) append to this very
        list while something iterates it. The second one is not hypothetical --
        it was an infinite loop.
        """
        self.calls.append({"tool": name, "args": args, "found": ok,
                           "result": result})

    # -- the four questions --------------------------------------------------

    def mandate_chain(self, payment_id: str) -> dict:
        """What authority did the human delegate, and was it still valid?

        Returns the mandate behind a payment: who delegated to whom, the bounds
        they set, the delegation in their own words, the signature, and whether
        it had been revoked.

        Args:
            payment_id: the disputed payment, e.g. "pay_3f2a...".
        """
        rec = self.engine.state.payments.get(payment_id)
        if rec is None:
            out = {"error": f"no payment {payment_id} in the ledger"}
            self._record("mandate_chain", {"payment_id": payment_id}, False, out)
            return out
        m = self.engine.state.mandates.get(rec.mandate_id)
        if m is None:
            out = {"error": f"payment {payment_id} cites mandate "
                            f"{rec.mandate_id or '<none>'}, which is not in the log",
                   "mandate_id": rec.mandate_id}
            self._record("mandate_chain", {"payment_id": payment_id}, False, out)
            return out
        scope = m.get("scope", {})
        revoked = any(e.type == "mandate_revoked"
                      and e.payload.get("mandate_id") == rec.mandate_id
                      for e in self.engine.eventlog)
        out = {
            "mandate_id": m["mandate_id"],
            "source_id": m["mandate_id"],
            "principal": m.get("principal"),
            "agent": m.get("agent"),
            "delegation_in_the_humans_words": m.get("source_text"),
            "issued_at": m.get("issued_at"),
            "expires_at": m.get("expires_at"),
            "signed": bool(m.get("signature")),
            "signature": (m.get("signature") or "")[:32],
            "revoked": revoked,
            "categories_allowed": scope.get("categories_allowed"),
            "categories_denied": scope.get("categories_denied"),
            "per_transaction_cap": scope.get("per_transaction_cap"),
            "period_cap": scope.get("period_cap"),
            "requires_step_up_above": scope.get("requires_step_up_above"),
            "soft_constraints": scope.get("soft_constraints"),
        }
        self._record("mandate_chain", {"payment_id": payment_id}, True, out)
        return out

    def decision_record(self, payment_id: str) -> dict:
        """What did the authorization gate decide, and on what grounds?

        Returns the sealed decision: the verdict, the mandate clause it relied
        on, the reasons, whether a model was consulted, and any manipulation
        attempts found in the seller's listing text.

        Args:
            payment_id: the disputed payment.
        """
        rec = self.engine.state.payments.get(payment_id)
        if rec is None or not rec.decision_id:
            out = {"error": f"no gate decision recorded for {payment_id}"}
            self._record("decision_record", {"payment_id": payment_id}, False, out)
            return out
        d = self.gate.chain.get(rec.decision_id)
        if d is None:
            out = {"error": f"decision {rec.decision_id} is not in the chain"}
            self._record("decision_record", {"payment_id": payment_id}, False, out)
            return out
        out = {
            "decision_id": d.decision_id,
            "source_id": d.decision_id,
            "verdict": d.verdict,
            "decided_at": d.decided_at,
            "amount": d.amount,
            "reason": d.reason,
            "cited_clause": d.cited_clause,
            "bounds_verdict": d.bounds_verdict,
            "deterministic_checks_that_fired": [
                {"clause": f["clause"], "verdict": f["verdict"], "detail": f["detail"]}
                for f in d.bounds_findings if f.get("verdict") != "PASS"],
            "adjudicator_consulted": d.adjudicator_consulted,
            "adjudicator_model": d.adjudicator_model,
            "adjudicator_reason": d.adjudicator_reason,
            "listing_attempted_instruction": d.listing_attempted_instruction,
            "manipulation_signals": d.sanitization_signals,
            "hash_of_listing_as_the_agent_saw_it": d.proposal_hash,
        }
        self._record("decision_record", {"payment_id": payment_id}, True, out)
        return out

    def proposed_cart(self, payment_id: str) -> dict:
        """What did the agent actually propose, as it appeared at the time?

        Returns the cart the agent saw -- SKUs, seller titles and descriptions,
        prices, and the merchant's familiarity from the register. This is the
        listing as it was at decision time; a merchant editing it afterwards
        cannot change this record.

        Args:
            payment_id: the disputed payment.
        """
        rec = self.engine.state.payments.get(payment_id)
        if rec is None or not rec.proposal_id:
            out = {"error": f"no proposal recorded for {payment_id}"}
            self._record("proposed_cart", {"payment_id": payment_id}, False, out)
            return out
        p = self.engine.state.proposals.get(rec.proposal_id)
        if p is None:
            out = {"error": f"proposal {rec.proposal_id} is not in the log"}
            self._record("proposed_cart", {"payment_id": payment_id}, False, out)
            return out
        out = {
            "proposal_id": p.get("proposal_id"),
            "source_id": p.get("proposal_id"),
            "proposed_at": p.get("proposed_at"),
            "amount": p.get("amount"),
            "merchant_id": p.get("merchant_id"),
            "merchant_familiarity": p.get("merchant_familiarity"),
            "merchant_onboarded": p.get("merchant_onboarded"),
            "items": [{"sku": i["sku"], "seller_title": i["name"],
                       "seller_description": i.get("description", ""),
                       "registered_category": i["category"], "price": i["price"]}
                      for i in p.get("items", [])],
        }
        self._record("proposed_cart", {"payment_id": payment_id}, True, out)
        return out

    def money_trail(self, payment_id: str) -> dict:
        """What did the money actually do, and what did the book say at the time?

        Returns the capture, the fees, any refunds, the settlement and UTR, plus
        the PG receivable balance AS OF the capture -- not as of today.

        Args:
            payment_id: the disputed payment.
        """
        rec = self.engine.state.payments.get(payment_id)
        if rec is None:
            out = {"error": f"no payment {payment_id} in the ledger"}
            self._record("money_trail", {"payment_id": payment_id}, False, out)
            return out
        cap = next((e for e in self.engine.eventlog
                    if e.type == "payment_captured"
                    and e.payload.get("payment_id") == payment_id), None)
        snap = self.engine.snapshot_as_of(cap.event_id) if cap else self.engine.state
        settlement = self.engine.state.settlements.get(rec.settlement_id, {})
        out = {
            "payment_id": payment_id,
            "source_id": cap.event_id if cap else payment_id,
            "capture_event_id": cap.event_id if cap else None,
            "amount_captured": money_str(rec.amount),
            "captured_at": rec.captured_at,
            "instrument": rec.instrument,
            "merchant_id": rec.merchant_id,
            "status": rec.status,
            "amount_refunded": money_str(rec.refunded),
            "net_settlement": money_str(rec.net_settlement),
            "settlement_id": rec.settlement_id or None,
            "bank_utr": settlement.get("utr") or None,
            "pg_receivable_as_of_capture": money_str(snap.balance("1200")),
            "events_touching_this_payment": [
                e.event_id for e in self.engine.eventlog
                if e.payload.get("payment_id") == payment_id],
        }
        self._record("money_trail", {"payment_id": payment_id}, True, out)
        return out

    def integrity_check(self) -> dict:
        """Do the hash chains verify, and does the book satisfy its invariants?

        Returns whether the event log and decision chain are intact. A failure
        here means the records may have been altered after the fact, which
        matters more to a dispute than anything else in the packet.
        """
        chain_ok, violations, bad = self.engine.verify()
        dec_ok, bad_dec = self.gate.chain.verify()
        out = {
            "source_id": "integrity",
            "event_log_verified": chain_ok,
            "decision_chain_verified": dec_ok,
            "first_bad_event": bad,
            "first_bad_decision": bad_dec,
            "invariant_violations": [str(v) for v in violations],
            "events_in_log": len(self.engine.eventlog),
        }
        self._record("integrity_check", {}, True, out)
        return out

    def as_list(self) -> list:
        return [self.mandate_chain, self.decision_record, self.proposed_cart,
                self.money_trail, self.integrity_check]
