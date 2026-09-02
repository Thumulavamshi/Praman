"""Gateway fees and Indian indirect-tax treatment on an agent-initiated capture.

Getting this right is a credibility signal, and getting it *honestly* right
means stating where the simplifications are. There are four separate deductions
that can sit between "customer paid ₹1,000" and "₹976.40 hit my bank", and they
have different legal bases:

**1. MDR** -- the gateway's own fee, a commercial charge. Rate varies by
instrument: UPI on a small-merchant P2M transaction is zero-rated, cards are
~2%, netbanking sits in between. Modelled per instrument in ``FeeSchedule``.

**2. GST on MDR, 18%** -- a supply of services by the gateway to the merchant.
The merchant pays it, and then *claims it back* as input tax credit, so it is an
asset (1400), not an expense. Folding it into the MDR expense line is the single
most common bookkeeping error in this flow and it overstates cost of sales by
18% of MDR forever.

**3. TDS under section 194-O, 0.1%** -- income-tax withholding, deducted by an
*e-commerce operator* on the gross sale value of an e-commerce participant. Rate
was cut from 1% to 0.1% with effect from 1 October 2024.

  Nuance we take a position on: a pure payment aggregator is generally **not**
  the person obliged to deduct here. CBDT Circular 17/2020 addresses exactly the
  case where a payment gateway also technically qualifies as an e-commerce
  operator and clarifies it need not deduct again where the operator already
  has. So for a merchant selling on its own storefront and using Razorpay purely
  as a PG, 194-O does not bite. It bites when the merchant is a participant on a
  marketplace. We therefore default ``tds_194o`` to **off** and make it a
  per-schedule switch rather than pretending the answer is universal.

**4. TCS under section 52 CGST, 0.5%** -- GST collected at source by an
e-commerce operator on the net taxable value of supplies it facilitates. Rate
halved from 1% to 0.5% with effect from 10 July 2024. Same marketplace-vs-PG
distinction as 194-O, so it defaults off too. When on, it is a credit the
merchant claims in its GSTR-2X, hence an asset (1600).

And on the sale side: the amount the customer pays is GST-**inclusive**. Booking
the whole of it as revenue overstates income and leaves the output-GST liability
unrecorded, so ``split_inclusive_gst`` backs the tax out of the gross.

Every rate here is a plain ``Decimal`` string. None of them are floats, and none
of them are guessed at call sites.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .money import ZERO, apply_rate, dec, money, rate

GST_ON_SERVICES = "0.1800"      # 18% GST on the gateway's service fee
TDS_194O_RATE = "0.0010"        # 0.1% w.e.f. 2024-10-01
TCS_52_RATE = "0.0050"          # 0.5% w.e.f. 2024-07-10

# Output GST slabs by product category. Indicative, and deliberately not
# exhaustive: unbranded staples are nil-rated, most packaged food is 5%, and
# consumer goods sit at 18%. Where a category is missing we do not guess -- we
# fall back to DEFAULT_OUTPUT_GST and say so in the memo.
OUTPUT_GST_BY_CATEGORY: dict[str, str] = {
    "groceries": "0.0500",
    "household": "0.1800",
    "personal_care": "0.1800",
    "pharmacy": "0.0500",
    "baby_care": "0.0500",
    "appliances": "0.1800",
    "electronics": "0.1800",
    "pet_supplies": "0.1800",
    "apparel": "0.0500",
    "gifting": "0.1800",
    "services": "0.1800",
    "alcohol": "0.0000",        # outside GST; state excise instead
    "tobacco": "0.2800",
}
DEFAULT_OUTPUT_GST = "0.1800"


@dataclass(frozen=True)
class FeeSchedule:
    """What the gateway deducts, per payment instrument.

    ``mdr_by_instrument`` is keyed by the instrument strings Razorpay uses
    (``upi``, ``card``, ``netbanking``, ``wallet``) so a real settlement report
    can be matched against it without a translation layer.
    """

    mdr_by_instrument: dict[str, str] = field(default_factory=lambda: {
        "upi": "0.0000",         # zero-MDR on UPI P2M
        "card": "0.0200",
        "netbanking": "0.0175",
        "wallet": "0.0200",
    })
    gst_on_fee: str = GST_ON_SERVICES
    tds_194o: bool = False       # see module docstring -- off for a pure PG
    tcs_52: bool = False
    reserve_rate: str = "0.0000"  # rolling reserve withheld by the PG, if any

    def mdr_rate(self, instrument: str) -> Decimal:
        if instrument not in self.mdr_by_instrument:
            raise KeyError(
                f"no MDR configured for instrument {instrument!r}; "
                f"known: {sorted(self.mdr_by_instrument)}"
            )
        return rate(self.mdr_by_instrument[instrument])


DEFAULT_SCHEDULE = FeeSchedule()


@dataclass(frozen=True)
class FeeBreakdown:
    """Every deduction on one capture, each rounded exactly once."""

    gross: Decimal
    mdr: Decimal
    gst_on_mdr: Decimal
    tds: Decimal
    tcs: Decimal
    reserve: Decimal

    @property
    def total_deducted(self) -> Decimal:
        return money(self.mdr + self.gst_on_mdr + self.tds + self.tcs + self.reserve)

    @property
    def net_settlement(self) -> Decimal:
        return money(self.gross - self.total_deducted)


def compute_fees(gross, instrument: str, schedule: FeeSchedule = DEFAULT_SCHEDULE) -> FeeBreakdown:
    """Break a gross capture into what the gateway keeps and what settles.

    Order matters and is fixed: MDR off the gross, GST off the MDR (not off the
    gross -- it is a tax on the *fee*), statutory withholdings off the gross,
    reserve off the gross. Each line rounds once, at its own computation; we
    never round an intermediate and then round again.
    """
    gross = money(gross)
    mdr = apply_rate(gross, schedule.mdr_rate(instrument))
    gst_on_mdr = apply_rate(mdr, schedule.gst_on_fee)
    tds = apply_rate(gross, TDS_194O_RATE) if schedule.tds_194o else ZERO
    tcs = apply_rate(gross, TCS_52_RATE) if schedule.tcs_52 else ZERO
    reserve = apply_rate(gross, schedule.reserve_rate)
    return FeeBreakdown(gross=gross, mdr=mdr, gst_on_mdr=gst_on_mdr,
                        tds=tds, tcs=tcs, reserve=reserve)


def output_gst_rate(category: str) -> Decimal:
    return rate(OUTPUT_GST_BY_CATEGORY.get(category, DEFAULT_OUTPUT_GST))


def split_inclusive_gst(gross, category: str) -> tuple[Decimal, Decimal]:
    """Back the output GST out of a tax-inclusive amount.

    taxable = gross / (1 + r); tax = gross - taxable. Computing the tax as
    ``taxable * r`` instead would round twice and can leave the two halves
    failing to sum back to the gross, which is precisely the kind of one-paisa
    hole that breaks the balance invariant.
    """
    r = output_gst_rate(category)
    taxable = money(dec(gross) / (Decimal(1) + r))
    tax = money(dec(gross) - taxable)
    return taxable, tax
