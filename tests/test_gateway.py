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
