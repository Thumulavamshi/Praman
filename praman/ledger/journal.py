"""The journal entry, and the two ways a handler can decline to produce one.

A handler is a pure function ``(state, event) -> JournalEntry``. It may read
state -- a refund settlement needs to know the refund was initiated -- but it
may not mutate it. Applying the entry is the engine's job, and keeping those
separate is what makes replay deterministic.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .money import drop_zero_legs, legs_balance


class Rejected(Exception):
    """The event is well-formed but cannot be booked, and that is a normal outcome.

    An out-of-order refund settlement, a capture for a payment we never
    authorized, a settlement naming a payment that is not ours. In an agentic
    system these are common, not exceptional: agents retry, gateways re-deliver
    webhooks out of order, and test fixtures are messy. A rejection is recorded
    with its reason and the run continues. It never ends the run.
    """

    def __init__(self, reason: str, *, code: str = "rejected"):
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True)
class JournalEntry:
    """A balanced set of legs, plus the event that caused it."""

    event_id: str
    type: str
    ts: str
    legs: list[dict] = field(default_factory=list)
    memo: str = ""

    def normalized(self) -> "JournalEntry":
        return JournalEntry(self.event_id, self.type, self.ts,
                            drop_zero_legs(self.legs), self.memo)

    def balanced(self) -> bool:
        return legs_balance(self.legs)


EMPTY = []
