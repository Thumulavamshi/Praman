"""The payment gateway seam.

Phase 0 decided this interface before finding out whether the live Razorpay
integration would work, so that the swap would be free. It was not free in the
end -- ``api.razorpay.com`` is blocked by this build environment's egress policy
-- which is exactly why deciding the interface first was worth doing.

The mock is not a stub. It returns Razorpay's actual object shapes: amounts in
paise as integers, ``rzp_`` prefixed ids, the same status strings, the same
notes dict. Code written against the mock runs unchanged against the live
client, and the ledger cannot tell them apart. What the mock does not do is
prove the live API accepts our requests -- so ``razorpay_pg.py`` exists and is
runnable anywhere the host is reachable, and the README says plainly which one
produced any given number.

Amounts cross this boundary in **paise, as integers**, because that is what
Razorpay's API takes. Converting at the boundary rather than internally means
there is exactly one place a rupees/paise mistake can happen, and it is here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

from praman.ledger.money import dec, money, money_str


def to_paise(rupees) -> int:
    """Rupees (Decimal or string) -> paise (int). The only conversion point.

    Rounds first, so 1799.999 becomes 180000 rather than 179999 -- and cannot
    silently truncate a half-paisa the way int(x * 100) does on a float.
    """
    return int(money(rupees) * 100)


def to_rupees(paise: int) -> Decimal:
    """Paise (int) -> rupees (Decimal). Never touches float."""
    return money(dec(paise) / 100)


@dataclass
class Order:
    id: str
    amount: int              # paise
    currency: str
    receipt: str
    status: str
    notes: dict = field(default_factory=dict)


@dataclass
class Payment:
    id: str
    order_id: str
    amount: int              # paise
    currency: str
    status: str              # created | authorized | captured | failed | refunded
    method: str              # upi | card | netbanking | wallet
    captured: bool = False
    error_code: str = ""
    error_description: str = ""
    notes: dict = field(default_factory=dict)

    @property
    def rupees(self) -> Decimal:
        return to_rupees(self.amount)


@dataclass
class Refund:
    id: str
    payment_id: str
    amount: int
    status: str              # pending | processed | failed
    notes: dict = field(default_factory=dict)


class PaymentGateway(Protocol):
    """Everything Praman needs from a gateway. Deliberately small.

    No settlement-fetch, no webhook registration, no customer objects. Each of
    those is a real Razorpay surface and none of them is on the demo path, and
    an interface with methods nobody calls is an interface nobody can swap.
    """

    name: str

    def create_order(self, amount_rupees, receipt: str,
                     notes: dict | None = None) -> Order: ...

    def capture_payment(self, payment_id: str, amount_rupees,
                        currency: str = "INR") -> Payment: ...

    def simulate_payment(self, order: Order, method: str = "card",
                         fail: bool = False) -> Payment:
        """Bring a payment into existence against an order.

        On the live gateway a payment is created by the customer on a checkout
        page, so there is nothing for the server to call. In test mode Razorpay
        offers no server-side way to conjure one either, which is why this
        method exists on the interface at all: the demo needs a payment to
        capture, and where it comes from is the one genuine difference between
        the two implementations.
        """
        ...

    def refund(self, payment_id: str, amount_rupees,
               notes: dict | None = None) -> Refund: ...

    def fetch_payment(self, payment_id: str) -> Payment: ...
