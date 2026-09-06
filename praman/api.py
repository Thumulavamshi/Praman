"""HTTP surface. Thin on purpose.

Every endpoint is a direct call into ``Praman``; there is no logic here that is
not also reachable from Python, because logic that exists only behind HTTP is
logic the test suite reaches only by spinning up a server.

The interesting endpoint is ``/authorize``. It is deliberately *not* a
"validate" endpoint that returns a boolean for the caller to act on: it decides,
captures, and books in one call, because a design where the caller can obtain an
ALLOW and then choose to capture something else is a design where the decision
record does not describe the transaction. The gate must be in the path, not
beside it.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .data.mandates import ISSUER
from .gate.adjudicator import LLMAdjudicator, StaticAdjudicator
from .gate.gate import Gate
from .ledger.accounts import CHART, account_name
from .ledger.engine import Engine
from .ledger.money import money_str
from .mandate.schema import Mandate, Proposal
from .orchestrator import Praman
from .pg.razorpay_pg import get_gateway


class StepUpApproval(BaseModel):
    """A human resuming a deferred purchase.

    Defined at module level, not inside build_app. FastAPI resolves a handler's
    annotations against the module namespace, so a request model nested in a
    factory is invisible to it and the field silently degrades to a query
    parameter -- a 422 that looks like the client sent the wrong thing.
    """
    decision_id: str
    approver: str
    proposal: Proposal


def build_app(praman: Praman | None = None) -> FastAPI:
    if praman is None:
        offline = os.getenv("PRAMAN_OFFLINE", "").lower() in ("1", "true", "yes")
        adjudicator = StaticAdjudicator() if offline else LLMAdjudicator()
        praman = Praman(Gate(adjudicator=adjudicator, issuer=ISSUER),
                        Engine(), get_gateway())

    app = FastAPI(
        title="Praman",
        description="A verification-native trust layer that makes a merchant "
                    "safely transactable by an AI buyer.",
        version="0.1.0")
    app.state.praman = praman

    # -- mandates ------------------------------------------------------------

    @app.post("/mandates")
    def create_mandate(mandate: Mandate) -> dict:
        """Register a mandate. Signs it if it arrives unsigned."""
        p: Praman = app.state.praman
        if not mandate.signature:
            mandate = ISSUER.issue(mandate)
        r = p.register_mandate(mandate)
        return {"mandate_id": mandate.mandate_id, "signature": mandate.signature,
                "ledger": r.status}

    @app.post("/mandates/{mandate_id}/revoke")
    def revoke(mandate_id: str) -> dict:
        p: Praman = app.state.praman
        if mandate_id not in p._mandates:
            raise HTTPException(404, f"no mandate {mandate_id}")
        return {"mandate_id": mandate_id,
                "ledger": p.revoke_mandate(mandate_id).status}

    @app.get("/mandates/{mandate_id}")
    def get_mandate(mandate_id: str) -> dict:
        p: Praman = app.state.praman
        m = p._mandates.get(mandate_id)
        if m is None:
            raise HTTPException(404, f"no mandate {mandate_id}")
        return {"mandate": m.model_dump(exclude_none=True),
                "revoked": mandate_id in p._revoked,
                "signature_verifies": ISSUER.verify(m)}

    # -- the authorization path ---------------------------------------------

    @app.post("/authorize")
    def authorize(proposal: Proposal) -> dict:
        """Decide, and on ALLOW capture and book. One call, one record."""
        p: Praman = app.state.praman
        out = p.purchase(proposal)
        return {
            "verdict": out.verdict, "decision_id": out.decision_id,
            "reason": out.reason, "cited_clause": out.cited_clause,
            "payment_id": out.payment_id, "order_id": out.order_id,
            "captured_amount": out.captured_amount,
            "gateway_error": out.gateway_error,
        }

    @app.post("/step-up/approve")
    def approve(body: StepUpApproval) -> dict:
        p: Praman = app.state.praman
        out = p.approve_step_up(body.proposal, body.decision_id, body.approver)
        return {"verdict": out.verdict, "decision_id": out.decision_id,
                "reason": out.reason, "payment_id": out.payment_id}

    # -- the book ------------------------------------------------------------

    @app.get("/ledger/balances")
    def balances() -> dict:
        p: Praman = app.state.praman
        st = p.engine.state
        return {"accounts": [
            {"code": c, "name": account_name(c), "balance": money_str(st.balance(c))}
            for c in CHART], "events": len(p.engine.eventlog)}

    @app.get("/ledger/verify")
    def verify() -> dict:
        p: Praman = app.state.praman
        chain_ok, violations, bad = p.engine.verify()
        dec_ok, bad_dec = p.gate.chain.verify()
        return {"event_chain_ok": chain_ok, "first_bad_event": bad,
                "decision_chain_ok": dec_ok, "first_bad_decision": bad_dec,
                "invariant_violations": [str(v) for v in violations],
                "quarantined": len(p.engine.quarantine)}

    @app.get("/ledger/snapshot/{event_id}")
    def snapshot(event_id: str) -> dict:
        """The book as it stood at one event. The as-of query, over HTTP."""
        p: Praman = app.state.praman
        try:
            st = p.engine.snapshot_as_of(event_id)
        except KeyError:
            raise HTTPException(404, f"no event {event_id} in the log")
        return {"as_of": event_id,
                "balances": {c: money_str(b) for c, b in st.nonzero_balances().items()}}

    # -- decisions and evidence ---------------------------------------------

    @app.get("/decisions/{decision_id}")
    def decision(decision_id: str) -> dict:
        p: Praman = app.state.praman
        d = p.gate.chain.get(decision_id)
        if d is None:
            raise HTTPException(404, f"no decision {decision_id}")
        return {"decision": d.to_dict(), "summary": d.human_summary()}

    @app.get("/evidence/{payment_id}")
    def evidence(payment_id: str, chargeback_id: str = "") -> dict:
        """The representment packet. Every number quoted from a record."""
        p: Praman = app.state.praman
        b = p.defend(payment_id, chargeback_id)
        return {
            "payment_id": payment_id,
            "completeness": b.completeness,
            "chain_verified": b.chain_verified,
            "citations": [{"claim": c.claim, "value": c.value,
                           "source_kind": c.source_kind, "source_id": c.source_id}
                          for c in b.citations],
            "gaps": b.gaps,
            "rendered": b.render(),
        }

    @app.get("/ui", response_class=HTMLResponse)
    def ui() -> str:
        """The audit trail, rendered from a real run.

        The same page tools/build_ui.py writes to disk, so the demo works with
        the network off and the live path shows the identical thing. A demo that
        needs a server running is a demo that fails on stage.
        """
        from .ui.build import build
        return build()

    @app.get("/health")
    def health() -> dict:
        p: Praman = app.state.praman
        return {"ok": True, "gateway": p.gateway.name,
                "adjudicator": type(p.gate.adjudicator).__name__,
                "events": len(p.engine.eventlog),
                "decisions": len(p.gate.chain)}

    return app


app = build_app()
