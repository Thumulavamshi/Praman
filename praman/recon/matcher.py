"""The deterministic three-way matcher. It runs first and should carry ~90%.

Same architecture as the authorization gate, one layer down: what can be decided
mechanically is decided mechanically, and the model only sees what is genuinely
open. A reconciler that sent every line to an LLM would be slower, costlier, and
— because an LLM will always produce *an* answer — would quietly convert
arithmetic into opinion.

Three passes, cheapest and most certain first:

  1. **Ledger vs settlement, per payment.** Does the gateway agree with our book
     about what it kept? An exact match on the deductions, to the paisa. This is
     the pass that catches fee drift, which is the most common break there is.
  2. **Settlement vs bank, by UTR.** The description is free text a bank
     mangles, so a UTR appearing in it is evidence, not proof — the amount must
     agree too.
  3. **Aggregate.** The payout must equal the sum of its rows, net of refunds
     the gateway took back.

Anything surviving all three is the residual. That is the agent's input, and
keeping it small is most of the value here.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from praman.ledger.money import ZERO, money, money_str
from .models import BankLine, Exception_, MatchReport, SettlementRow

# What counts as "the same amount". Zero: a reconciliation that tolerates a
# paisa is a reconciliation that hides a systematic fee error behind noise. If
# the numbers differ at all, that is a finding, not a rounding artefact.
TOLERANCE = ZERO


def reconcile(engine, rows: list[SettlementRow],
              lines: list[BankLine]) -> MatchReport:
    report = MatchReport()
    by_settlement: dict[str, list[SettlementRow]] = defaultdict(list)
    for r in rows:
        by_settlement[r.settlement_id].append(r)

    # --- pass 1: does the gateway agree with our book, per payment? ---------
    for r in rows:
        rec = engine.state.payments.get(r.payment_id)
        if rec is None:
            report.exceptions.append(Exception_(
                r.payment_id, "unknown_payment",
                "the settlement file names a payment the ledger never booked",
                r.net))
            continue
        ours = money(rec.amount - rec.net_settlement)
        theirs = r.deductions
        if abs(ours - theirs) > TOLERANCE:
            report.exceptions.append(Exception_(
                r.payment_id, "fee_mismatch",
                f"gateway kept INR {money_str(theirs)}, our book says "
                f"INR {money_str(ours)} (out by {money_str(theirs - ours)})",
                money(abs(theirs - ours))))
        else:
            report.matched_payments.add(r.payment_id)

    # --- pass 2: settlement to bank, by UTR AND amount ----------------------
    unclaimed = list(lines)
    for sid, srows in sorted(by_settlement.items()):
        expected = _expected_payout(engine, sid, srows)
        utr = next((r.utr for r in srows if r.utr), "")
        hit = None
        for l in unclaimed:
            if utr and utr in l.description and l.amount == expected:
                hit = l
                break
        if hit is not None:
            unclaimed.remove(hit)
            report.matched_settlements[sid] = [hit.line_id]
        else:
            report.unmatched_settlements.append(sid)

    # --- pass 3: an exact-amount second look, for a mangled description -----
    # A bank that truncated the UTR still credited the right rupees. Matching on
    # a unique amount is safe; matching on an ambiguous one is not, so a value
    # that appears twice is left for the agent rather than guessed at.
    still: list[str] = []
    for sid in report.unmatched_settlements:
        expected = _expected_payout(engine, sid, by_settlement[sid])
        same = [l for l in unclaimed if l.amount == expected]
        if len(same) == 1:
            unclaimed.remove(same[0])
            report.matched_settlements[sid] = [same[0].line_id]
        else:
            still.append(sid)
    report.unmatched_settlements = still
    report.unmatched_bank = unclaimed

    for sid in report.unmatched_settlements:
        expected = _expected_payout(engine, sid, by_settlement[sid])
        report.exceptions.append(Exception_(
            sid, "unmatched_settlement",
            f"gateway reports a payout of INR {money_str(expected)} with no "
            f"single bank credit to match it", expected))
    for l in report.unmatched_bank:
        report.exceptions.append(Exception_(
            l.line_id, "unmatched_credit",
            f"INR {money_str(l.amount)} credited on {l.value_date} that no "
            f"settlement explains: \"{l.description[:52]}\"", l.amount))
    return report


def _expected_payout(engine, sid: str, srows: list[SettlementRow]) -> Decimal:
    """What the bank should have credited for this settlement.

    Read from the ledger's own settlement record where we have one, because that
    is the figure our book committed to. Falling back to the sum of the
    gateway's rows would quietly let the gateway define the answer, and then a
    fee error would reconcile perfectly against itself.
    """
    s = engine.state.settlements.get(sid)
    if s is not None:
        return money(s["amount"])
    return money(sum((r.net for r in srows), ZERO))
