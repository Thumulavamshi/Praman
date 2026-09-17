"""The gateway seam: Razorpay's shapes, and its refusals."""
from decimal import Decimal

import pytest

from praman.pg.interface import to_paise, to_rupees
from praman.pg.mock import GatewayError, MockRazorpay


@pytest.fixture
def gw():
    return MockRazorpay(seed=1)


def test_rupees_and_paise_round_trip_without_touching_float():
    for r in ["1799.00", "0.01", "2000.00", "28.00", "1899.99"]:
        assert to_rupees(to_paise(r)) == Decimal(r)


def test_paise_conversion_rounds_rather_than_truncating():
    # int(17.999 * 100) is 1799 on a float. Ours is 1800.
    assert to_paise("17.999") == 1800
    assert to_paise("0.005") == 1


def test_an_order_carries_the_mandate_and_decision_into_the_gateway():
    gw = MockRazorpay(seed=1)
    o = gw.create_order("1799.00", "rcpt_1",
                        {"mandate_id": "mnd_1", "decision_id": "dec_1"})
    assert o.amount == 179900 and o.currency == "INR" and o.status == "created"
    assert o.id.startswith("order_")
    assert o.notes["decision_id"] == "dec_1"


def test_a_second_capture_is_refused_exactly_as_the_live_gateway_refuses_it(gw):
    """Verified against real Razorpay: it answers BAD_REQUEST_ERROR.

    This test previously asserted the opposite -- that re-capture returns the
    same payment idempotently -- because the mock was written on that
    assumption. Running the live check disproved it. The safety property is
    unchanged and if anything stronger: a refusal cannot double-charge either.
    What it means is that re-capture is not a retry strategy, so Praman must not
    use it as one, and Praman.purchase() does not.
    """
    o = gw.create_order("500.00", "r")
    p = gw.simulate_payment(o)
    a = gw.capture_payment(p.id, "500.00")
    assert a.status == "captured"

    with pytest.raises(GatewayError, match="already been captured"):
        gw.capture_payment(p.id, "500.00")


def test_a_failed_payment_cannot_be_captured(gw):
    o = gw.create_order("500.00", "r")
    p = gw.simulate_payment(o, fail=True)
    with pytest.raises(GatewayError):
        gw.capture_payment(p.id, "500.00")


def test_capturing_a_different_amount_than_authorized_is_refused(gw):
    o = gw.create_order("500.00", "r")
    p = gw.simulate_payment(o)
    with pytest.raises(GatewayError, match="does not match"):
        gw.capture_payment(p.id, "600.00")


def test_over_refunding_is_refused(gw):
    o = gw.create_order("500.00", "r")
    p = gw.capture_payment(gw.simulate_payment(o).id, "500.00")
    gw.refund(p.id, "300.00")
    with pytest.raises(GatewayError, match="exceeds the refundable balance"):
        gw.refund(p.id, "300.00")


def test_a_live_key_is_refused_outright(monkeypatch):
    """Praman books test data. A live key here is a way to move real money."""
    from praman.pg.razorpay_pg import RazorpayGateway, RazorpayNotConfigured
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_something")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    with pytest.raises(RazorpayNotConfigured, match="non-test key"):
        RazorpayGateway()


def test_the_gateway_defaults_to_the_mock_so_a_demo_never_needs_network(monkeypatch):
    from praman.pg.razorpay_pg import get_gateway
    monkeypatch.delenv("PRAMAN_PG", raising=False)
    assert get_gateway().name == "mock"


def test_the_refundable_balance_is_readable_off_the_payment(gw):
    """Both implementations must answer "how much is left?" the same way.

    Neither used to. The refund counters came back on every live payment object
    and we dropped them, so the only way to find out whether a payment was
    already refunded was to refund it and read the 400 -- and Razorpay's 400
    here is the generic "invalid request sent", which names no field and is
    indistinguishable from a malformed request body.
    """
    o = gw.create_order("500.00", "r")
    p = gw.capture_payment(gw.simulate_payment(o).id, "500.00")
    assert (p.amount_refunded, p.refund_status, p.refundable) == (0, "", 50000)

    gw.refund(p.id, "300.00")
    p = gw.fetch_payment(p.id)
    assert p.amount_refunded == 30000
    assert p.refund_status == "partial"
    assert p.refundable == 20000
    assert p.status == "captured"      # partial refunds do not move the status

    gw.refund(p.id, "200.00")
    p = gw.fetch_payment(p.id)
    assert p.refund_status == "full"
    assert p.refundable == 0
    assert p.status == "refunded"


def test_a_refund_sends_no_parameter_it_was_not_asked_for(monkeypatch):
    """``speed`` is opt-in, because an unrequested parameter can cost a refund.

    A live run answered BAD_REQUEST_ERROR "invalid request sent" on an ordinary
    partial refund. ``speed="normal"`` was the only optional field we sent
    unconditionally, and "normal" is the API's own default -- so it could never
    do anything except fail on an account that does not accept the field.
    """
    from praman.pg.razorpay_pg import RazorpayGateway
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghij")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    live = RazorpayGateway()

    sent = {}

    class FakePaymentResource:
        def refund(self, payment_id, body):
            sent.clear()
            sent.update(body)
            return {"id": "rfnd_1", "payment_id": payment_id,
                    "amount": body["amount"], "status": "processed", "notes": {}}

    live._client.payment = FakePaymentResource()

    live.refund("pay_1", "178.50", notes={"reason": "praman_check"})
    assert sent == {"amount": 17850, "notes": {"reason": "praman_check"}}

    live.refund("pay_1", "178.50", speed="optimum")
    assert sent["speed"] == "optimum"


def test_the_live_client_reads_the_refund_counters_off_the_api_shape(monkeypatch):
    from praman.pg.razorpay_pg import RazorpayGateway
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghij")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    p = RazorpayGateway()._payment({
        "id": "pay_1", "order_id": "order_1", "amount": 35700, "currency": "INR",
        "status": "captured", "method": "card", "captured": True,
        "amount_refunded": 17850, "refund_status": "partial", "notes": {},
    })
    assert p.amount_refunded == 17850 and p.refund_status == "partial"
    assert p.refundable == 17850

    # A payment that has never been refunded carries JSON null, not 0.
    q = RazorpayGateway()._payment({
        "id": "pay_2", "order_id": "", "amount": 35700, "currency": "INR",
        "status": "captured", "method": "card",
        "amount_refunded": None, "refund_status": None,
    })
    assert q.amount_refunded == 0 and q.refund_status == "" and q.refundable == 35700


def test_a_failed_call_reports_the_whole_error_body_not_just_its_description(monkeypatch):
    """The SDK raises BadRequestError(description) and drops the rest.

    That is how a rejected parameter reaches you as the bare string "invalid
    request sent" -- a symptom with no field name, which sends you bisecting
    request bodies by hand against a live payments API.
    """
    from praman.pg.razorpay_pg import RazorpayGateway
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abcdefghij")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "secret")
    live = RazorpayGateway()
    assert live.last_error() == ""

    class Resp:
        status_code = 400
        text = ""

        def json(self):
            return {"error": {"code": "BAD_REQUEST_ERROR",
                              "description": "invalid request sent",
                              "field": "speed", "source": "business",
                              "step": "payment_refund", "reason": "NA"}}

    # The hook the client installs is what captures this in a real call.
    assert live._remember in live._client.session.hooks["response"]
    live._remember(Resp())

    detail = live.last_error()
    assert "HTTP 400" in detail
    assert "field=speed" in detail
    assert "step=payment_refund" in detail
    assert "reason=NA" not in detail      # Razorpay's filler, not information

    class NotJson:
        status_code = 502
        text = "<html>bad gateway</html>"

        def json(self):
            raise ValueError("no json here")

    live._remember(NotJson())
    assert "HTTP 502" in live.last_error()


def test_a_fetched_payment_is_a_snapshot_not_the_live_object(gw):
    """CORRECTED 2026-09-17. The mock used to hand back the stored payment.

    The live client builds a fresh Payment out of each API response, so a value
    read from one is fixed until you fetch again. The mock returning its own
    mutable object meant a caller who read a balance, acted on it, and then
    looked at the value they had read was -- against the mock only -- looking
    at the value as it is NOW.

    That is the worst shape a seam bug can take: the code is correct against
    one implementation and wrong against the other, with no error either way.
    The live check's own over-refund probe tripped on exactly this, read a
    balance of zero, asked for one rupee, and reported that the gateway had
    accepted an over-refund.
    """
    o = gw.create_order("500.00", "r")
    p = gw.capture_payment(gw.simulate_payment(o).id, "500.00")

    snap = gw.fetch_payment(p.id)
    assert snap is not gw.payments[p.id]
    assert snap.refundable == 50000

    gw.refund(p.id, "200.00")
    assert snap.refundable == 50000                  # what we read stays read
    assert gw.fetch_payment(p.id).refundable == 30000  # a new fetch moves on

    snap.notes["tampered"] = "x"
    assert "tampered" not in gw.fetch_payment(p.id).notes


def test_a_refunded_payment_cannot_be_captured_again(gw):
    """CORRECTED 2026-09-17. This used to succeed and reset the status.

    The guard only looked for "captured", so a payment that had been captured
    and then fully refunded fell through to the amount check, passed it, and
    was marked captured again -- while amount_refunded still read the full
    amount. That is a state the live gateway cannot produce and the ledger
    cannot reconcile: money recorded as taken and returned and taken again,
    from one authorization.
    """
    o = gw.create_order("500.00", "r")
    p = gw.capture_payment(gw.simulate_payment(o).id, "500.00")
    gw.refund(p.id, "500.00")
    assert gw.fetch_payment(p.id).status == "refunded"

    with pytest.raises(GatewayError, match="already been captured"):
        gw.capture_payment(p.id, "500.00")

    after = gw.fetch_payment(p.id)
    assert after.status == "refunded" and after.amount_refunded == 50000
