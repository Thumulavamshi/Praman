"""A Razorpay-shaped gateway that runs in-process.

Object shapes, id prefixes, status strings and paise-denominated integer amounts
all match the live API, so nothing downstream can tell the difference. Two
things it deliberately keeps that a lazier mock would drop:

  * **Re-capture is refused, exactly as the real gateway refuses it.** Verified
    live: Razorpay answers "This payment has already been captured". An earlier
    version of this mock returned the payment instead, and that divergence was
    only caught by running the real thing -- which is the whole argument for
    keeping the live check script around.
  * **Real refusals.** Capturing a failed payment, over-refunding, and capturing
    an amount that disagrees with the authorization all raise. A gateway that
    never says no makes every error path in the caller untested.

Deterministic under a seed, so the demo runs identically every time.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field, replace

from praman.ledger.money import money, money_str
from .interface import Order, Payment, Refund, to_paise, to_rupees


class GatewayError(Exception):
    def __init__(self, code: str, description: str):
        super().__init__(f"{code}: {description}")
        self.code = code
        self.description = description


@dataclass
class MockRazorpay:
    """In-process. No network. The default, because the live host is blocked."""

    name: str = "mock"
    seed: int = 20260903
    _rng: random.Random = field(init=False)
    orders: dict = field(default_factory=dict)
    payments: dict = field(default_factory=dict)
    refunds: dict = field(default_factory=dict)
    _n: int = 0

    def __post_init__(self):
        self._rng = random.Random(self.seed)

    def _id(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_{self._rng.getrandbits(56):014x}"

    # -- orders --------------------------------------------------------------

    def create_order(self, amount_rupees, receipt: str,
                     notes: dict | None = None) -> Order:
        amount = to_paise(amount_rupees)
        if amount < 100:
            raise GatewayError("BAD_REQUEST_ERROR",
                               "amount must be at least INR 1.00")
        o = Order(id=self._id("order"), amount=amount, currency="INR",
                  receipt=receipt, status="created", notes=notes or {})
        self.orders[o.id] = o
        return o

    # -- payments ------------------------------------------------------------

    def simulate_payment(self, order: Order, method: str = "card",
                         fail: bool = False) -> Payment:
        p = Payment(
            id=self._id("pay"), order_id=order.id, amount=order.amount,
            currency=order.currency,
            status="failed" if fail else "authorized",
            method=method, notes=dict(order.notes),
            error_code="BAD_REQUEST_ERROR" if fail else "",
            error_description="payment failed on the customer's bank page"
                              if fail else "")
        self.payments[p.id] = p
        if not fail:
            order.status = "attempted"
        return p

    def capture_payment(self, payment_id: str, amount_rupees,
                        currency: str = "INR") -> Payment:
        p = self.payments.get(payment_id)
        if p is None:
            raise GatewayError("BAD_REQUEST_ERROR",
                               f"payment {payment_id} does not exist")
        if p.status == "failed":
            raise GatewayError("BAD_REQUEST_ERROR",
                               "this payment failed and cannot be captured")
        amount = to_paise(amount_rupees)
        if p.status in ("captured", "refunded"):
            # CORRECTED against the live API, 2026-09-03. This mock previously
            # returned the payment, on the assumption that Razorpay treats
            # re-capture idempotently. It does not -- it answers
            #   BAD_REQUEST_ERROR: This payment has already been captured
            # and a mock that is kinder than the gateway is worse than no mock,
            # because it lets code pass here and fail in production.
            #
            # The safety property still holds, and more strongly: a refusal
            # cannot double-charge either. What changes is that the CALLER must
            # not treat re-capture as a retry strategy. Praman does not -- see
            # Praman.purchase(), which returns the prior outcome rather than
            # reaching the gateway a second time.
            raise GatewayError(
                "BAD_REQUEST_ERROR", "This payment has already been captured")
        if amount != p.amount:
            raise GatewayError(
                "BAD_REQUEST_ERROR",
                f"capture amount {amount} does not match the authorized "
                f"amount {p.amount}")
        p.status = "captured"
        p.captured = True
        self.orders[p.order_id].status = "paid"
        return p

    def _live(self, payment_id: str) -> Payment:
        """The stored payment itself. Internal -- mutations belong in here."""
        p = self.payments.get(payment_id)
        if p is None:
            raise GatewayError("BAD_REQUEST_ERROR", f"no such payment {payment_id}")
        return p

    def fetch_payment(self, payment_id: str) -> Payment:
        """A snapshot, not the stored object.

        CORRECTED 2026-09-17. This used to return the stored payment itself, so
        a caller holding the result watched it change under them whenever
        anything else touched the payment. The live client cannot do that: it
        builds a fresh Payment out of each API response, and a value read from
        one is fixed until you fetch again.

        The divergence is invisible until it is expensive. Read a refundable
        balance, refund against it, then check the balance you read -- against
        the live gateway you are holding the number you read, against this mock
        you were silently holding the number as it is NOW. The live check's own
        over-refund test was reading a balance of zero for exactly this reason
        and concluding the gateway had accepted an over-refund.
        """
        p = self._live(payment_id)
        return replace(p, notes=dict(p.notes))

    # -- refunds -------------------------------------------------------------

    def refund(self, payment_id: str, amount_rupees,
               notes: dict | None = None) -> Refund:
        p = self._live(payment_id)
        if p.status not in ("captured", "refunded"):
            raise GatewayError("BAD_REQUEST_ERROR",
                               "only a captured payment can be refunded")
        amount = to_paise(amount_rupees)
        already = sum(r.amount for r in self.refunds.values()
                      if r.payment_id == payment_id and r.status != "failed")
        if already + amount > p.amount:
            raise GatewayError(
                "BAD_REQUEST_ERROR",
                f"refund of {amount} exceeds the refundable balance of "
                f"{p.amount - already} paise")
        r = Refund(id=self._id("rfnd"), payment_id=payment_id, amount=amount,
                   status="processed", notes=notes or {})
        self.refunds[r.id] = r
        # Keep the payment's own refund counters in step, because the live API
        # does. A caller that asks "how much is left?" must get the same answer
        # from both implementations or the seam is a lie in the one direction
        # that matters -- the direction where money has already moved.
        p.amount_refunded = already + amount
        p.refund_status = "full" if p.amount_refunded == p.amount else "partial"
        if already + amount == p.amount:
            p.status = "refunded"
        return r
