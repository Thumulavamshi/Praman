"""Checking a proposed resolution before it lands. The verifier outranks the AI.

This is the same principle as the ledger's invariant suite and the dispute
packet's citation check, applied a third time — and by now that repetition is
the argument, not an accident. Wherever a model's output touches the book, a
deterministic check stands between the two and can say no.

What is checked, by kind:

  ``match`` / ``split_match``
      The named credits must exist, must not already be claimed, and must sum
      **exactly** to the payout the ledger expects. Not approximately. A matcher
      that tolerates a paisa is a matcher that hides a systematic fee error
      inside its own tolerance, and reconciliation exists to find exactly that.

  ``adjusting_entry``
      Both accounts must be real, and the amount must equal a discrepancy the
      deterministic pass actually found — the agent may not invent a figure, it
      may only resolve one already on the exception list. Then the entry is
      applied through the ordinary engine, which means the full invariant suite
      runs and rolls it back if the book would not hold. **That rollback is the
      rejection**, and it is the same code path that guards every other entry.

  ``escalate``
      Always accepted. Deferring to a human is a correct answer, and a verifier
      that penalised it would push the agent to guess on precisely the cases
      where guessing is most expensive.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from praman.ledger.accounts import CHART
from praman.ledger.money import ZERO, money, money_str
from .models import BankLine, MatchReport, Resolution


@dataclass
class Verdict:
    accepted: bool
    reason: str
    applied_event: str = ""

    def __str__(self) -> str:
        return ("accepted" if self.accepted else "REJECTED") + f": {self.reason}"


def verify(engine, report: MatchReport, res: Resolution,
           lines: list[BankLine], *, expected_payout=None,
           apply: bool = False, event_id: str = "") -> Verdict:
    by_id = {l.line_id: l for l in lines}
    claimed = {lid for ids in report.matched_settlements.values() for lid in ids}

    if res.kind == "escalate":
        return Verdict(True, "escalated to a human, which is a correct answer")

    if res.kind in ("match", "split_match"):
        if not res.settlement_id:
            return Verdict(False, "names no settlement to match against")
        if not res.bank_line_ids:
            return Verdict(False, "names no bank credits")
        if res.kind == "split_match" and len(res.bank_line_ids) < 2:
            return Verdict(False, "a split match needs more than one credit")

        total = ZERO
        for lid in res.bank_line_ids:
            l = by_id.get(lid)
            if l is None:
                return Verdict(False, f"bank line {lid} does not exist")
            if lid in claimed:
                return Verdict(
                    False,
                    f"bank line {lid} is already matched to another settlement; "
                    f"claiming it twice would credit the same money twice")
            total = money(total + l.amount)

        if expected_payout is None:
            return Verdict(False, f"no expected payout known for "
                                  f"{res.settlement_id}")
        if total != money(expected_payout):
            return Verdict(
                False,
                f"the credits sum to INR {money_str(total)} but the payout is "
                f"INR {money_str(expected_payout)} — out by "
                f"{money_str(total - money(expected_payout))}")

        if apply:
            report.matched_settlements[res.settlement_id] = list(res.bank_line_ids)
            report.unmatched_settlements = [
                s for s in report.unmatched_settlements if s != res.settlement_id]
            report.unmatched_bank = [
                l for l in report.unmatched_bank if l.line_id not in res.bank_line_ids]
        return Verdict(True, f"credits sum exactly to INR {money_str(total)}")

    if res.kind == "adjusting_entry":
        amount = money(res.amount)
        if amount <= ZERO:
            return Verdict(False, "an adjustment must be a positive amount")
        for acct in (res.debit_account, res.credit_account):
            if acct not in CHART:
                return Verdict(False, f"account {acct} is not in the chart")

        # The agent may only resolve a discrepancy the deterministic pass
        # actually found. Without this it could book any figure it liked and the
        # entry would still balance, because a balanced entry says nothing about
        # whether it is TRUE.
        found = {money(e.amount) for e in report.exceptions}
        if amount not in found:
            return Verdict(
                False,
                f"INR {money_str(amount)} does not correspond to any "
                f"discrepancy the matcher found; an adjustment may resolve a "
                f"real difference, not invent one")

        if apply:
            r = engine.apply(
                event_id or f"ev_recon_{len(engine.eventlog)}",
                "recon_adjustment",
                engine.eventlog.events()[-1].ts if len(engine.eventlog) else
                "2026-09-05T10:00:00+05:30",
                {"debit_account": res.debit_account,
                 "credit_account": res.credit_account,
                 "amount": money_str(amount),
                 "memo": res.memo or res.reason[:80]})
            if r.status != "applied":
                # The engine's own invariant suite refused it and rolled the
                # book back. Nothing to undo here; that is the guarantee.
                return Verdict(False, f"the ledger refused it — {r.detail}")
            return Verdict(True, f"booked INR {money_str(amount)}", r.event_id)

        return Verdict(True, f"would book INR {money_str(amount)}")

    return Verdict(False, f"unknown resolution kind {res.kind!r}")
