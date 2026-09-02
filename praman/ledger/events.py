"""Event types and the canonical envelope.

The book is a pure fold over an append-only log. That means the event is the
only durable thing here: balances, decision outcomes and evidence bundles are
all derived, all reproducible, and all disposable. If a number in a dispute
packet cannot be traced back to an event id in this log, it does not go in the
packet.

Sixteen types are defined. Six of them -- the thin spine -- carry the money:

    payment_captured, fee_debited, refund_settled,
    settlement_credited, chargeback_raised, chargeback_lost

The other ten are lifecycle and evidence. They post no journal entry, which does
not make them optional: ``mandate_created`` and ``agent_purchase_proposed`` are
what make a capture *defensible*, and a capture with no proposal behind it is
exactly the transaction a merchant cannot represent.
"""
from __future__ import annotations

# --- Mandate lifecycle (no journal impact; evidence only) -------------------
MANDATE_CREATED = "mandate_created"
MANDATE_REVOKED = "mandate_revoked"
AGENT_PURCHASE_PROPOSED = "agent_purchase_proposed"
GATE_DECIDED = "gate_decided"

# --- Payments ---------------------------------------------------------------
PAYMENT_AUTHORIZED = "payment_authorized"
PAYMENT_CAPTURED = "payment_captured"
PAYMENT_FAILED = "payment_failed"
FEE_DEBITED = "fee_debited"

# --- Reversals --------------------------------------------------------------
REFUND_INITIATED = "refund_initiated"
REFUND_SETTLED = "refund_settled"

# --- Disputes ---------------------------------------------------------------
CHARGEBACK_RAISED = "chargeback_raised"
EVIDENCE_SUBMITTED = "evidence_submitted"
CHARGEBACK_WON = "chargeback_won"
CHARGEBACK_LOST = "chargeback_lost"

# --- Settlement -------------------------------------------------------------
SETTLEMENT_CREDITED = "settlement_credited"
RESERVE_HELD = "reserve_held"
RESERVE_RELEASED = "reserve_released"

ALL_EVENT_TYPES = frozenset({
    MANDATE_CREATED, MANDATE_REVOKED, AGENT_PURCHASE_PROPOSED, GATE_DECIDED,
    PAYMENT_AUTHORIZED, PAYMENT_CAPTURED, PAYMENT_FAILED, FEE_DEBITED,
    REFUND_INITIATED, REFUND_SETTLED,
    CHARGEBACK_RAISED, EVIDENCE_SUBMITTED, CHARGEBACK_WON, CHARGEBACK_LOST,
    SETTLEMENT_CREDITED, RESERVE_HELD, RESERVE_RELEASED,
})

# The six that move money. Kept as its own set so the thin-spine test can assert
# it is exactly what the handlers cover.
THIN_SPINE = frozenset({
    PAYMENT_CAPTURED, FEE_DEBITED, REFUND_SETTLED,
    SETTLEMENT_CREDITED, CHARGEBACK_RAISED, CHARGEBACK_LOST,
})

# Types that legitimately post nothing to the book. Listed explicitly so that a
# handler returning no legs is a *decision*, never an oversight -- the engine
# checks membership here before accepting an empty entry.
NO_JOURNAL_IMPACT = frozenset({
    MANDATE_CREATED, MANDATE_REVOKED, AGENT_PURCHASE_PROPOSED, GATE_DECIDED,
    PAYMENT_AUTHORIZED, PAYMENT_FAILED, EVIDENCE_SUBMITTED,
})
