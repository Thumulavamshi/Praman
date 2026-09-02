"""The live Razorpay test-mode client, behind the same interface.

Not reachable from the build environment this was written in --
``api.razorpay.com`` is denied by the egress policy, which Phase 0 established
before a line of it was written. It is here, complete and runnable, because the
interface was fixed first and because anywhere the host IS reachable this is a
one-environment-variable swap:

    PRAMAN_PG=razorpay

The one place the two implementations genuinely differ is
``simulate_payment``. On the live gateway a payment comes into existence when a
customer completes checkout; there is no server-side call that conjures one, in
test mode or otherwise. So this raises rather than pretending, and the caller
is told to drive checkout instead. A mock that quietly fabricated a payment id
here would make the demo look end-to-end when it was not.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .interface import Order, Payment, Refund, to_paise


class RazorpayNotConfigured(RuntimeError):
    pass


@dataclass
class RazorpayGateway:
    name: str = "razorpay"
    key_id: str = field(default_factory=lambda: os.getenv("RAZORPAY_KEY_ID", ""))
    key_secret: str = field(
        default_factory=lambda: os.getenv("RAZORPAY_KEY_SECRET", ""))
    _client: object = None

    def __post_init__(self):
        if not self.key_id or not self.key_secret:
            raise RazorpayNotConfigured(
                "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set; "
                "copy .env.example to .env")
        if not self.key_id.startswith("rzp_test"):
            # A live key in a system that books test data is a way to move real
            # money by accident. Refuse rather than warn.
            raise RazorpayNotConfigured(
                f"refusing to run against a non-test key ({self.key_id[:12]}...); "
                "Praman books test data and must never touch live credentials")
        import razorpay
        self._client = razorpay.Client(auth=(self.key_id, self.key_secret))

    def create_order(self, amount_rupees, receipt: str,
                     notes: dict | None = None) -> Order:
        d = self._client.order.create({
            "amount": to_paise(amount_rupees), "currency": "INR",
            "receipt": receipt, "notes": notes or {},
        })
        return Order(id=d["id"], amount=d["amount"], currency=d["currency"],
                     receipt=d.get("receipt", ""), status=d["status"],
                     notes=d.get("notes") or {})

    def simulate_payment(self, order: Order, method: str = "card",
                         fail: bool = False) -> Payment:
        raise NotImplementedError(
            "a live Razorpay payment is created by the customer completing "
            "checkout; there is no server-side call that creates one. Drive "
            "the checkout flow with order id "
            f"{order.id}, then pass the resulting payment id to "
            "capture_payment(). Use PRAMAN_PG=mock for an unattended demo.")

    def capture_payment(self, payment_id: str, amount_rupees,
                        currency: str = "INR") -> Payment:
        d = self._client.payment.capture(payment_id, to_paise(amount_rupees),
                                         {"currency": currency})
        return self._payment(d)

    def fetch_payment(self, payment_id: str) -> Payment:
        return self._payment(self._client.payment.fetch(payment_id))

    def refund(self, payment_id: str, amount_rupees,
               notes: dict | None = None) -> Refund:
        d = self._client.payment.refund(payment_id, {
            "amount": to_paise(amount_rupees), "notes": notes or {},
            "speed": "normal",
        })
        return Refund(id=d["id"], payment_id=d["payment_id"], amount=d["amount"],
                      status=d["status"], notes=d.get("notes") or {})

    @staticmethod
    def _payment(d: dict) -> Payment:
        return Payment(
            id=d["id"], order_id=d.get("order_id", ""), amount=d["amount"],
            currency=d["currency"], status=d["status"],
            method=d.get("method", ""), captured=bool(d.get("captured")),
            error_code=d.get("error_code") or "",
            error_description=d.get("error_description") or "",
            notes=d.get("notes") or {})


def get_gateway(kind: str | None = None):
    """Pick the gateway from PRAMAN_PG. Defaults to the mock.

    Defaults to the mock deliberately: a demo that silently needs network is a
    demo that fails on stage. The live client is opt-in and says so in its name.
    """
    kind = (kind or os.getenv("PRAMAN_PG", "mock")).lower()
    if kind == "razorpay":
        return RazorpayGateway()
    if kind == "mock":
        from .mock import MockRazorpay
        return MockRazorpay()
    raise ValueError(f"unknown PRAMAN_PG={kind!r}; use 'mock' or 'razorpay'")
