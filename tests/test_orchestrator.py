"""The end-to-end path, and the ordering that makes it safe."""
import pytest

from praman.data.mandates import AGENT, ISSUER, variant
from praman.gate.adjudicator import StaticAdjudicator
from praman.gate.gate import Gate
from praman.ledger.engine import Engine
from praman.ledger.invariants import check_all
from praman.mandate.schema import CartItem, Proposal
from praman.orchestrator import Praman
from praman.pg.mock import MockRazorpay

WHEN = "2026-09-03T19:20:00+05:30"


@pytest.fixture
def praman():
    return Praman(Gate(adjudicator=StaticAdjudicator(), issuer=ISSUER),
                  Engine(), MockRazorpay(seed=5))


def prop(amount="357.00", category="groceries", merchant="mch_dailymart",
         mandate_id="mnd_reference", pid="p1", at=WHEN, description=""):
    return Proposal(
        proposal_id=pid, mandate_id=mandate_id, agent=AGENT,
        merchant_id=merchant, merchant_familiarity="established",
        proposed_at=at, amount=amount, instrument="card",
        items=[CartItem(sku="S1", name="item", category=category,
                        merchant_id=merchant, price=amount,
                        description=description)])


def test_a_purchase_with_no_registered_mandate_is_not_an_agent_purchase(praman):
    out = praman.purchase(prop())
    assert out.verdict == "BLOCK" and not out.payment_id
    assert "no mandate" in out.reason


def test_an_allowed_purchase_captures_and_books(praman):
    praman.register_mandate(variant("reference"))
    out = praman.purchase(prop())
    assert out.verdict == "ALLOW" and out.captured
    assert out.captured_amount == "357.00"
    st = praman.engine.state
    assert st.payments[out.payment_id].amount == praman.engine.state.payments[
        out.payment_id].amount
    assert check_all(st) == []


def test_a_blocked_purchase_never_reaches_the_gateway(praman):
    praman.register_mandate(variant("reference"))
    out = praman.purchase(prop("4800.00", "alcohol", "mch_spiritsco"))
    assert out.verdict == "BLOCK" and not out.payment_id
    assert praman.gateway.orders == {} and praman.gateway.payments == {}


def test_a_step_up_does_not_capture_until_a_human_approves(praman):
    praman.register_mandate(variant("reference"))
    p = prop("1800.00")
    out = praman.purchase(p)
    assert out.verdict == "STEP_UP" and not out.payment_id

    approved = praman.approve_step_up(p, out.decision_id, "user_9931")
    assert approved.verdict == "STEP_UP_APPROVED" and approved.captured
    assert "user_9931 approved" in approved.reason


def test_the_step_up_approval_is_a_second_record_not_an_overwrite(praman):
    """A dispute turns on whether a human intervened. The trail must show it."""
    praman.register_mandate(variant("reference"))
    p = prop("1800.00")
    first = praman.purchase(p)
    praman.approve_step_up(p, first.decision_id, "user_9931")
    original = praman.gate.chain.get(first.decision_id)
    assert original.verdict == "STEP_UP"           # unchanged
    assert len(praman.gate.chain) == 2
    assert praman.gate.chain.verify()[0]


def test_approving_something_that_was_not_stepped_up_is_refused(praman):
    praman.register_mandate(variant("reference"))
    p = prop()
    allowed = praman.purchase(p)
    out = praman.approve_step_up(p, allowed.decision_id, "user_9931")
    assert out.verdict == "BLOCK" and "not an open step-up" in out.reason


def test_a_revoked_mandate_stops_capturing(praman):
    praman.register_mandate(variant("reference"))
    assert praman.purchase(prop(pid="p1")).captured
    praman.revoke_mandate("mnd_reference")
    out = praman.purchase(prop(pid="p2"))
    assert out.verdict == "BLOCK" and "revoked" in out.reason


def test_the_period_cap_reads_spend_from_the_book_not_a_side_tally(praman):
    praman.register_mandate(variant("reference"))
    for i in range(5):
        assert praman.purchase(prop("1400.00", pid=f"p{i}")).captured
    # 7,000 spent against an 8,000 weekly cap; 1,400 more would exceed it.
    out = praman.purchase(prop("1400.00", pid="p_over"))
    assert out.verdict == "BLOCK" and "period_cap" in out.cited_clause


def test_a_refund_frees_up_the_period_cap_it_consumed(praman):
    praman.register_mandate(variant("reference"))
    outs = [praman.purchase(prop("1400.00", pid=f"p{i}")) for i in range(5)]
    assert praman.purchase(prop("1400.00", pid="p_a")).verdict == "BLOCK"
    praman.refund(outs[0].payment_id, "1400.00", "customer changed their mind")
    assert praman.purchase(prop("1400.00", pid="p_b")).captured


def test_a_gateway_failure_does_not_book_a_capture_that_did_not_happen(praman):
    praman.register_mandate(variant("reference"))

    class Broken(MockRazorpay):
        def capture_payment(self, *a, **k):
            from praman.pg.mock import GatewayError
            raise GatewayError("GATEWAY_ERROR", "upstream bank timeout")

    praman.gateway = Broken(seed=5)
    out = praman.purchase(prop())
    assert not out.captured and "upstream bank timeout" in out.gateway_error
    assert praman.engine.state.payments == {}
    assert check_all(praman.engine.state) == []


def test_the_whole_path_leaves_an_invariant_clean_book(praman):
    praman.register_mandate(variant("reference"))
    a = praman.purchase(prop("357.00", pid="p1"))
    praman.purchase(prop("4800.00", "alcohol", "mch_spiritsco", pid="p2"))
    praman.refund(a.payment_id, "100.00", "damaged")
    praman.raise_chargeback(a.payment_id, "agent_not_authorized", "257.00")
    ok, violations, bad = praman.engine.verify()
    assert ok and violations == [] and bad is None
