"""Properties that must hold after every event. The verifier that can reject the AI.

This file is the reason a probabilistic system is allowed near the authorization
path at all. Nothing lands in the book -- not a handler's entry, not a
reconciliation agent's proposed adjustment, not a step-up approval -- without
passing ``check_all``. When an AI-proposed action fails a check, the proposal is
refused and escalated. The verifier outranks the model, always, and a live
demonstration of the verifier rejecting the model is worth more than a clean run.

Checks are cheap and total. They run over the whole state after every event in
the test suite, and after every AI proposal in production. There is no sampling.
"""
from __future__ import annotations

from dataclasses import dataclass

from .accounts import CHART
from .money import ZERO, dec, money, money_str
from .state import LedgerState


@dataclass(frozen=True)
class Violation:
    check: str
    detail: str
    context: dict

    def __str__(self) -> str:
        return f"[{self.check}] {self.detail}"


def trial_balance_sums_to_zero(state: LedgerState) -> list[Violation]:
    """Debits equal credits across the whole book.

    Balances are held debit-positive, so the sum over every account is exactly
    zero when the book is sound. This is the check that catches a handler
    emitting an unbalanced entry, and it is the reason the ledger can be trusted
    to contradict a settlement report.
    """
    total = sum(state.balances.values(), ZERO)
    if money(total) != ZERO:
        return [Violation("trial_balance", f"book is out by {money_str(total)}",
                          {"total": money_str(total)})]
    return []


def every_entry_balances(state: LedgerState) -> list[Violation]:
    out = []
    for e in state.entries:
        if not e.balanced():
            out.append(Violation("entry_balance",
                                 f"entry for {e.event_id} ({e.type}) does not balance",
                                 {"event_id": e.event_id, "legs": e.legs}))
    return out


def accounts_are_known(state: LedgerState) -> list[Violation]:
    out = []
    for code in state.balances:
        if code not in CHART:
            out.append(Violation("unknown_account",
                                 f"posting to account {code} which is not in the chart",
                                 {"account": code}))
    return out


def refunds_never_exceed_capture(state: LedgerState) -> list[Violation]:
    out = []
    for pid, rec in state.payments.items():
        if rec.refunded > rec.amount:
            out.append(Violation(
                "over_refund",
                f"{pid} refunded {money_str(rec.refunded)} against a capture of "
                f"{money_str(rec.amount)}",
                {"payment_id": pid}))
    return out


def reserve_never_negative(state: LedgerState) -> list[Violation]:
    v = state.balance("1300")
    if v < ZERO:
        return [Violation("negative_reserve", f"reserve held is {money_str(v)}",
                          {"balance": money_str(v)})]
    return []


def refunds_payable_never_negative(state: LedgerState) -> list[Violation]:
    v = state.balance("2100")
    if v < ZERO:
        return [Violation("negative_refunds_payable",
                          f"refunds payable is {money_str(v)}",
                          {"balance": money_str(v)})]
    return []


def chargeback_provision_never_negative(state: LedgerState) -> list[Violation]:
    v = state.balance("2300")
    if v < ZERO:
        return [Violation("negative_chargeback_provision",
                          f"chargeback provision is {money_str(v)}",
                          {"balance": money_str(v)})]
    return []


def settled_payments_have_a_settlement(state: LedgerState) -> list[Violation]:
    out = []
    for pid, rec in state.payments.items():
        if rec.status == "settled" and not rec.settlement_id:
            out.append(Violation("orphan_settled_payment",
                                 f"{pid} is marked settled with no settlement id",
                                 {"payment_id": pid}))
    return out


def every_capture_cites_a_decision(state: LedgerState) -> list[Violation]:
    """No money moves without a gate decision behind it.

    This is the invariant that makes the whole thing a *trust layer* rather than
    a ledger with an LLM bolted on. A capture whose ``decision_id`` names no
    decision in the log is a payment the merchant cannot defend, and it is
    flagged here rather than discovered during a dispute.

    Captures that carry no mandate at all -- an ordinary human checkout -- are
    out of scope and skipped: Praman only claims to govern agent-initiated flow.
    """
    out = []
    for pid, rec in state.payments.items():
        if not rec.mandate_id:
            continue
        if not rec.decision_id or rec.decision_id not in state.decisions:
            out.append(Violation(
                "undecided_capture",
                f"agent-initiated capture {pid} cites decision "
                f"{rec.decision_id or '<none>'} which is not in the log",
                {"payment_id": pid, "decision_id": rec.decision_id}))
            continue
        d = state.decisions[rec.decision_id]
        if d.get("verdict") not in ("ALLOW", "STEP_UP_APPROVED"):
            out.append(Violation(
                "captured_against_verdict",
                f"{pid} was captured but the gate returned {d.get('verdict')}",
                {"payment_id": pid, "verdict": d.get("verdict")}))
    return out


ALL_CHECKS = (
    trial_balance_sums_to_zero,
    every_entry_balances,
    accounts_are_known,
    refunds_never_exceed_capture,
    reserve_never_negative,
    refunds_payable_never_negative,
    chargeback_provision_never_negative,
    settled_payments_have_a_settlement,
    every_capture_cites_a_decision,
)


def check_all(state: LedgerState, checks=ALL_CHECKS) -> list[Violation]:
    out: list[Violation] = []
    for check in checks:
        out.extend(check(state))
    return out
