"""Decimal arithmetic, the single rounding convention, and journal-leg construction.

Praman books real Indian payment flows: a capture, an MDR fee, 18% GST on that
fee, a TDS withholding under section 194-O, a refund, a settlement payout. Every
one of those is a percentage applied to a rupee amount, which means every one of
them lands on a half-paisa boundary eventually. If two places in the codebase
round differently, the book acquires a residual that never clears and the
invariant suite starts failing for reasons nobody can trace.

So there is exactly one money rounding helper here, and handlers are not allowed
to call ``quantize`` themselves. A second rounding path is a second convention.

Two rules this module owns:

  1. Every amount is rounded to the paisa, half away from zero.
  2. Binary float never touches a monetary value. Not once, not "just for the
     percentage". ``dec()`` refuses to see one silently.

Rule 2 matters more than it looks. ``0.1 + 0.2 != 0.3`` is a party trick until
it is a settlement that is eight paise short and a reconciler agent that cannot
explain why.
"""
from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP, getcontext

log = logging.getLogger(__name__)

# 28 significant digits is the library default, but pin it explicitly so
# behaviour never depends on whatever ambient context we happen to be imported
# into. A host process that narrowed the context would otherwise silently
# change our arithmetic.
getcontext().prec = 28

D = Decimal

PAISA = D("0.01")
BASIS = D("0.0001")            # rate quantum: MDR of 2% is stored as 0.0200

ZERO = D("0.00")


def dec(value) -> Decimal:
    """Parse a payload value into a Decimal without ever touching binary float.

    JSON is parsed with ``parse_float=Decimal`` (see ``eventlog.py``) so a float
    should never reach us in the first place. If one does, the parsing
    discipline has a hole in it: we convert via ``str`` -- the shortest
    round-tripping repr, so the least wrong option available -- and log loudly
    rather than quietly baking the error into the book.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        log.warning("float reached dec(): %r -- JSON parsing discipline broken", value)
        return D(str(value))
    if isinstance(value, int):
        return D(value)
    if isinstance(value, str):
        return D(value.strip())
    raise TypeError(f"cannot parse {value!r} ({type(value).__name__}) as Decimal")


def money(x) -> Decimal:
    """Round to the paisa, half AWAY FROM ZERO.

    Not the built-in ``round()``, which is half-to-even (banker's rounding).
    Indian tax and gateway statements round half up, so half-to-even would make
    our book disagree with the settlement file on roughly half the ties -- small
    amounts, but they accumulate and every one of them becomes a reconciliation
    exception.

    Python's ``ROUND_HALF_UP`` is a misleading name for the right behaviour: it
    rounds away from zero on a tie, so -1.005 -> -1.01, not -1.00. That matters
    for refunds and chargebacks, which carry negative-signed amounts.
    """
    return dec(x).quantize(PAISA, rounding=ROUND_HALF_UP)


def rate(x) -> Decimal:
    """Normalise a percentage rate to four decimal places.

    MDR is quoted as "2%" and stored as ``0.0200``; GST as "18%" -> ``0.1800``;
    TDS 194-O as "0.1%" -> ``0.0010``. Rates are quantized so that two gateway
    configs that mean the same thing compare equal.
    """
    return dec(x).quantize(BASIS, rounding=ROUND_HALF_UP)


def apply_rate(amount, r) -> Decimal:
    """Apply a rate to an amount and round the result once, at the end.

    The single most common way to introduce a residual is to round an
    intermediate. ``apply_rate(1000, 0.02)`` rounds exactly one time, here.
    """
    return money(dec(amount) * rate(r))


def money_str(x) -> str:
    """Serialize an amount as a fixed 2dp string: '0.00', never '0' or '0E-2'."""
    return format(money(x), "f")


def leg(account: str, debit=ZERO, credit=ZERO, memo: str = "") -> dict:
    """One journal leg.

    Both columns are always present; the unused one is '0.00'. ``account`` is a
    chart-of-accounts code from ``accounts.py``; ``memo`` is free text that ends
    up in the dispute evidence bundle, so it is worth writing properly.
    """
    return {
        "account": account,
        "debit": money_str(debit),
        "credit": money_str(credit),
        "memo": memo,
    }


def drop_zero_legs(legs: list[dict]) -> list[dict]:
    """Remove legs whose debit AND credit are both zero.

    A zero leg carries no information and cannot unbalance an entry, but it does
    make two structurally identical entries serialize differently -- which
    breaks the byte-identical-replay claim the chaos test makes. Applied
    centrally after every handler so no handler has to remember.
    """
    return [l for l in legs
            if not (dec(l["debit"]) == ZERO and dec(l["credit"]) == ZERO)]


def legs_balance(legs: list[dict]) -> bool:
    """Debits equal credits. True for an empty list."""
    debits = sum((dec(l["debit"]) for l in legs), ZERO)
    credits = sum((dec(l["credit"]) for l in legs), ZERO)
    return money(debits) == money(credits)


def invert_legs(legs: list[dict]) -> list[dict]:
    """Swap the debit and credit columns of every leg.

    Used to reverse an entry exactly. The amounts are COPIED, never recomputed:
    re-running the fee chain could land a half-paisa differently and leave a
    residual that never clears. A reversal must be the arithmetic inverse of
    what was actually booked, not of what we would book today.
    """
    return [
        {
            "account": l["account"],
            "debit": l["credit"],
            "credit": l["debit"],
            "memo": l.get("memo", ""),
        }
        for l in legs
    ]
