"""The book, as a pure fold over the log.

``LedgerState`` is derived data. Every field in it can be thrown away and
rebuilt by replaying the log from event zero, and the test suite does exactly
that on every run. Two consequences worth being explicit about:

  * **As-of replay is free.** Fold the first N events instead of all of them and
    you have the book as it stood at event N. That is the dispute evidence, and
    it needs no snapshot table, no temporal columns, and no "who edited this".
  * **There is nowhere to hide a correction.** You cannot quietly adjust a
    balance, because the balance is not stored -- it is computed. A correction
    has to be an event, which means it is in the chain, timestamped, and
    visible.

The state carries more than balances. It carries the mandate that authorised
each payment, the proposal the agent made, and the gate decision that let it
through -- because a balance alone cannot answer the only question that matters
in a dispute, which is *why*.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from .accounts import CHART, is_debit_natured
from .money import ZERO, dec, money


@dataclass
class PaymentRecord:
    payment_id: str
    order_id: str = ""
    amount: Decimal = ZERO
    currency: str = "INR"
    instrument: str = "card"
    merchant_id: str = ""
    category: str = ""
    mandate_id: str = ""
    proposal_id: str = ""
    decision_id: str = ""
    captured_at: str = ""
    status: str = "captured"
    fees_booked: bool = False
    net_settlement: Decimal = ZERO
    refunded: Decimal = ZERO
    settlement_id: str = ""
    chargeback_id: str = ""


@dataclass
class RefundRecord:
    refund_id: str
    payment_id: str
    amount: Decimal = ZERO
    status: str = "initiated"
    netted_in: str = ""      # settlement id this refund was deducted from


@dataclass
class ChargebackRecord:
    chargeback_id: str
    payment_id: str
    amount: Decimal = ZERO
    reason_code: str = ""
    status: str = "raised"
    evidence_event_id: str = ""


@dataclass
class LedgerState:
    balances: dict[str, Decimal] = field(
        default_factory=lambda: defaultdict(lambda: ZERO))
    payments: dict[str, PaymentRecord] = field(default_factory=dict)
    refunds: dict[str, RefundRecord] = field(default_factory=dict)
    chargebacks: dict[str, ChargebackRecord] = field(default_factory=dict)
    mandates: dict[str, dict] = field(default_factory=dict)
    proposals: dict[str, dict] = field(default_factory=dict)
    decisions: dict[str, dict] = field(default_factory=dict)
    settlements: dict[str, dict] = field(default_factory=dict)
    entries: list = field(default_factory=list)
    applied: set = field(default_factory=set)
    rejections: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    # -- balances ------------------------------------------------------------

    def post(self, legs: list[dict]) -> None:
        """Apply one entry's legs to the balances.

        Balances are held in debit-positive form internally -- a single sign
        convention across every account type, so the trial-balance invariant is
        a plain sum to zero rather than a type-aware walk. ``balance()``
        converts to natural sign on the way out, because a liability of 500
        printing as -500 is how reports get misread.
        """
        for l in legs:
            self.balances[l["account"]] += dec(l["debit"]) - dec(l["credit"])

    def raw(self, account: str) -> Decimal:
        return money(self.balances.get(account, ZERO))

    def balance(self, account: str) -> Decimal:
        """Balance in the account's natural sign (assets/expenses debit-positive)."""
        v = self.raw(account)
        return v if is_debit_natured(account) else -v

    def trial_balance(self) -> dict[str, Decimal]:
        return {code: self.balance(code) for code in CHART}

    def nonzero_balances(self) -> dict[str, Decimal]:
        return {c: b for c, b in self.trial_balance().items() if b != ZERO}


def fold(events, handlers, *, on_reject=None) -> LedgerState:
    """Replay events into a fresh state. The only way a LedgerState is built.

    ``handlers`` maps event type to ``(state, event) -> JournalEntry``. Unknown
    types are skipped rather than raising: the log is append-only and forward
    compatible, so an old build reading a stream that contains a newer event
    type should degrade to ignoring it, not refuse to open the book.
    """
    import traceback

    from .journal import Rejected

    state = LedgerState()
    for ev in events:
        if ev.event_id in state.applied:
            continue
        handler = handlers.get(ev.type)
        if handler is None:
            continue
        try:
            entry = handler(state, ev).normalized()
        except Rejected as r:
            state.rejections.append({"event_id": ev.event_id, "type": ev.type,
                                     "code": r.code, "reason": r.reason})
            if on_reject is not None:
                on_reject(ev, r)
            continue
        except Exception as exc:            # noqa: BLE001 -- fold must be total
            # A fold that can raise is a fold you cannot use to roll back, and
            # rollback is exactly what the engine needs it for. One bad event
            # costs one event, here as in the engine.
            state.errors.append({"event_id": ev.event_id, "type": ev.type,
                                 "error": f"{exc.__class__.__name__}: {exc}",
                                 "traceback": traceback.format_exc()})
            continue
        state.post(entry.legs)
        state.entries.append(entry)
        state.applied.add(ev.event_id)
    return state
