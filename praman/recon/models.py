"""The three records a reconciliation compares, and what a resolution may say.

Reconciliation is a three-way problem and collapsing it to two loses the thing
that matters:

  **the ledger**      what Praman booked -- what we believe happened
  **the settlement**  what the gateway says it paid out, per payment
  **the bank**        what actually landed, as one credit per payout

The gap between the first two is a disagreement about fees. The gap between the
second and third is a disagreement about money movement. They fail differently
and a merchant needs to know which one broke, so they stay separate here.

A resolution is a **typed** proposal, never free text. An agent that can only
answer in this vocabulary cannot invent a third kind of fix, and every one of
these four shapes is checkable before it lands.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from praman.ledger.money import ZERO, money, money_str


@dataclass(frozen=True)
class SettlementRow:
    """One payment as the gateway reports it in a settlement file."""
    payment_id: str
    settlement_id: str
    utr: str
    gross: Decimal
    fee: Decimal
    tax: Decimal
    net: Decimal
    settled_at: str

    @property
    def deductions(self) -> Decimal:
        return money(self.fee + self.tax)


@dataclass(frozen=True)
class BankLine:
    """One credit on the merchant's bank statement.

    A payout arrives as a single credit covering many payments, so the UTR in
    the description is usually the only thread back to the settlement.
    """
    line_id: str
    value_date: str
    amount: Decimal
    description: str

    def utr_hint(self) -> str:
        """Pull a UTR out of free-text description, if one is legible.

        Banks mangle these -- truncated, case-shifted, run together with the
        payer name. A hint is a hint; the matcher never treats it as proof on
        its own.
        """
        import re
        m = re.search(r"\b(UTR[A-Za-z0-9]{4,})\b", self.description)
        return m.group(1) if m else ""


Kind = Literal["match", "split_match", "adjusting_entry", "escalate"]


@dataclass
class Resolution:
    """A typed proposal for one unmatched item. The agent may say nothing else."""
    kind: Kind
    reason: str
    settlement_id: str = ""
    bank_line_ids: list[str] = field(default_factory=list)
    # An adjusting entry names BOTH legs and one amount, so an unbalanced entry
    # cannot be expressed. Making the invalid state unrepresentable is better
    # than catching it downstream -- what the verifier is left to check is the
    # part that actually needs judgment: whether the accounts exist, whether the
    # amount is the discrepancy we really found, and whether the book still
    # satisfies its invariants afterwards.
    debit_account: str = ""
    credit_account: str = ""
    amount: str = "0.00"
    memo: str = ""
    confidence: Literal["low", "medium", "high"] = "low"

    def __str__(self) -> str:
        if self.kind == "match":
            return f"match {self.settlement_id} <- {', '.join(self.bank_line_ids)}"
        if self.kind == "split_match":
            return (f"split_match {self.settlement_id} <- "
                    f"{len(self.bank_line_ids)} credits")
        if self.kind == "adjusting_entry":
            return (f"adjusting_entry Dr {self.debit_account} "
                    f"Cr {self.credit_account} {self.amount}")
        return f"escalate: {self.reason[:60]}"


@dataclass
class Exception_:
    """Something that did not reconcile, and why. The honest output."""
    ref: str
    kind: str
    detail: str
    amount: Decimal = ZERO

    def __str__(self) -> str:
        return f"[{self.kind}] {self.ref}: {self.detail}"


@dataclass
class MatchReport:
    matched_settlements: dict[str, list[str]] = field(default_factory=dict)
    matched_payments: set = field(default_factory=set)
    unmatched_bank: list[BankLine] = field(default_factory=list)
    unmatched_settlements: list[str] = field(default_factory=list)
    exceptions: list[Exception_] = field(default_factory=list)
    resolved_by_agent: list[tuple] = field(default_factory=list)
    rejected_proposals: list[tuple] = field(default_factory=list)

    # -- rates ---------------------------------------------------------------

    def auto_match_rate(self, total_settlements: int) -> float:
        return (len(self.matched_settlements) / total_settlements
                if total_settlements else 1.0)

    def unresolved_value(self) -> Decimal:
        return money(sum((e.amount for e in self.exceptions), ZERO))

    def summary(self) -> str:
        return (f"{len(self.matched_settlements)} settlements matched, "
                f"{len(self.unmatched_settlements)} unmatched, "
                f"{len(self.unmatched_bank)} bank lines unmatched, "
                f"{len(self.exceptions)} exceptions "
                f"worth INR {money_str(self.unresolved_value())}")
