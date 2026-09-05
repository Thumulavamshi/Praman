"""Settlement file and bank statement, generated from a real book then broken.

Same discipline as the ledger generator: build a **correct** pair first, then
apply corruptions as a separate transform. That separation is what lets the test
suite assert something meaningful — the clean pair must reconcile to zero
exceptions, and each corruption must produce exactly the exception it names.

The corruptions are not random noise. Every one is a break that happens to real
merchants, and each fails in its own way:

  ``fee_drift``       the gateway charged a paisa or two more than the schedule.
                      Rounding, or a rate change nobody told the merchant about.
                      Small, endless, and the single most common reconciliation
                      break there is.
  ``split_payout``    one settlement arrives as two bank credits. Nothing is
                      wrong; the deterministic matcher just cannot see it,
                      because neither credit equals the payout.
  ``mangled_utr``     the bank truncated or reformatted the UTR in the
                      description. The money is right, the thread back is gone.
  ``missing_credit``  the gateway says it paid; the bank has no such credit.
                      This is the one that is actually alarming.
  ``orphan_credit``   money arrived that no settlement explains.
  ``date_shift``      the credit landed a day late, so a date-window matcher
                      misses it while an id matcher does not.
  ``duplicate_credit`` the same payout credited twice. Rare, and expensive to
                      get wrong in either direction.
"""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta

from praman.ledger.money import ZERO, D, money, money_str
from .models import BankLine, SettlementRow

CORRUPTIONS = ("fee_drift", "split_payout", "mangled_utr", "missing_credit",
               "orphan_credit", "date_shift", "duplicate_credit")


def settlement_file(engine) -> list[SettlementRow]:
    """What the gateway would report, derived from what we booked.

    Deriving it from our own book means a clean run reconciles perfectly by
    construction, which is the only baseline against which a corruption means
    anything.
    """
    rows: list[SettlementRow] = []
    for sid, s in engine.state.settlements.items():
        for pid in s["payment_ids"]:
            rec = engine.state.payments.get(pid)
            if rec is None:
                continue
            deductions = money(rec.amount - rec.net_settlement)
            # The gateway reports fee and tax separately; GST is 18% of the fee,
            # so the fee is deductions / 1.18 and the tax is the remainder.
            fee = money(deductions / D("1.18"))
            rows.append(SettlementRow(
                payment_id=pid, settlement_id=sid, utr=s.get("utr", ""),
                gross=rec.amount, fee=fee, tax=money(deductions - fee),
                net=rec.net_settlement, settled_at=s.get("ts", "")))
    return rows


def bank_statement(engine, rows: list[SettlementRow]) -> list[BankLine]:
    """One credit per payout, net of any refunds the gateway took back."""
    lines: list[BankLine] = []
    for i, (sid, s) in enumerate(engine.state.settlements.items()):
        amount = money(s["amount"])
        if amount <= ZERO:
            continue
        utr = s.get("utr", f"UTR{i:07d}")
        lines.append(BankLine(
            line_id=f"bank_{i:04d}",
            value_date=(s.get("ts", "") or "")[:10],
            amount=amount,
            description=f"NEFT CR {utr} RAZORPAY SOFTWARE PVT LTD PAYOUT"))
    return lines


def corrupt(rows: list[SettlementRow], lines: list[BankLine], *,
            seed: int = 5, kinds=CORRUPTIONS,
            rate: float = 0.35) -> tuple[list[SettlementRow], list[BankLine], list[dict]]:
    """Break a correct pair in named ways. Returns the ground truth alongside.

    The truth list is what makes the AI measurable: for every break we know
    exactly which settlement it touched and what the right resolution is, so an
    agent proposal can be scored rather than admired.
    """
    rng = random.Random(seed)
    rows = list(rows)
    lines = list(lines)
    truth: list[dict] = []

    by_settlement: dict[str, BankLine] = {}
    for l in lines:
        for r in rows:
            if r.utr and r.utr in l.description:
                by_settlement.setdefault(r.settlement_id, l)

    settlements = sorted({r.settlement_id for r in rows})
    for sid in settlements:
        if rng.random() > rate:
            continue
        kind = rng.choice(list(kinds))
        line = by_settlement.get(sid)

        if kind == "fee_drift":
            # A paisa or two on one payment. The payout no longer equals the
            # sum of its rows, and nothing else looks wrong.
            idx = next((i for i, r in enumerate(rows)
                        if r.settlement_id == sid), None)
            if idx is None:
                continue
            drift = money(D(rng.choice(["0.01", "0.02", "0.05", "-0.01"])))
            r = rows[idx]
            rows[idx] = replace(r, fee=money(r.fee + drift),
                                net=money(r.net - drift))
            truth.append({"settlement_id": sid, "kind": kind,
                          "amount": money_str(abs(drift)),
                          "expected": "adjusting_entry"})

        elif kind == "split_payout" and line is not None:
            half = money(line.amount / D("2"))
            rest = money(line.amount - half)
            lines.remove(line)
            lines += [
                replace(line, line_id=line.line_id + "a", amount=half),
                replace(line, line_id=line.line_id + "b", amount=rest),
            ]
            truth.append({"settlement_id": sid, "kind": kind,
                          "amount": money_str(line.amount),
                          "expected": "split_match"})

        elif kind == "mangled_utr" and line is not None:
            i = lines.index(line)
            lines[i] = replace(line, description="NEFT CR RAZORPAY PAYOUT REF "
                                                 + line.line_id.upper())
            truth.append({"settlement_id": sid, "kind": kind,
                          "amount": money_str(line.amount),
                          "expected": "match"})

        elif kind == "missing_credit" and line is not None:
            lines.remove(line)
            truth.append({"settlement_id": sid, "kind": kind,
                          "amount": money_str(line.amount),
                          "expected": "escalate"})

        elif kind == "orphan_credit":
            amt = money(D(rng.randrange(50000, 900000)) / D("100"))
            lines.append(BankLine(
                line_id=f"bank_orphan_{sid}", value_date="2026-09-05",
                amount=amt,
                description="NEFT CR UTRUNKNOWN99 UNIDENTIFIED REMITTER"))
            truth.append({"settlement_id": "", "kind": kind,
                          "amount": money_str(amt), "expected": "escalate"})

        elif kind == "date_shift" and line is not None:
            i = lines.index(line)
            try:
                d = datetime.fromisoformat(line.value_date) + timedelta(days=1)
                lines[i] = replace(line, value_date=d.date().isoformat())
            except ValueError:
                continue
            truth.append({"settlement_id": sid, "kind": kind,
                          "amount": money_str(line.amount),
                          "expected": "match"})

        elif kind == "duplicate_credit" and line is not None:
            lines.append(replace(line, line_id=line.line_id + "_dup"))
            truth.append({"settlement_id": sid, "kind": kind,
                          "amount": money_str(line.amount),
                          "expected": "escalate"})

    return rows, lines, truth
