"""One handler per event type. Pure: read state, return a balanced entry.

Every handler here is written so that the *only* reason it can produce a wrong
number is that the event itself carried a wrong number. No handler computes a
fee from a rate it chose; no handler reads an amount from anywhere but the
payment object. That last point is a security property, not a style preference:
bucket D of the red-team set contains product listings claiming a display price
that differs from the charge, and the reason those attacks are structurally
impossible rather than merely resisted is this file.
"""
from __future__ import annotations

from decimal import Decimal

from . import events as E
from .accounts import (BANK, CHART, CHARGEBACK_LOSSES, CHARGEBACK_PROVISION, GST_INPUT,
                       GST_OUTPUT, MDR_EXPENSE, PG_RECEIVABLE, REFUNDS_PAYABLE,
                       RESERVE, SALES_REVENUE, TCS_CREDIT, TDS_RECEIVABLE)
from .fees import DEFAULT_SCHEDULE, FeeSchedule, compute_fees, split_inclusive_gst
from .journal import JournalEntry, Rejected
from .money import ZERO, dec, leg, money, money_str
from .state import ChargebackRecord, LedgerState, PaymentRecord, RefundRecord


def _entry(ev, legs, memo=""):
    return JournalEntry(event_id=ev.event_id, type=ev.type, ts=ev.ts,
                        legs=legs, memo=memo)


def _nothing(ev, memo=""):
    """A lifecycle event that legitimately posts nothing to the book."""
    return _entry(ev, [], memo)


# --- lifecycle: evidence, no journal impact ---------------------------------

def h_mandate_created(state: LedgerState, ev) -> JournalEntry:
    m = ev.payload["mandate"]
    state.mandates[m["mandate_id"]] = m
    return _nothing(ev, f"mandate {m['mandate_id']} issued to {m.get('agent', '?')}")


def h_mandate_revoked(state: LedgerState, ev) -> JournalEntry:
    mid = ev.payload["mandate_id"]
    m = state.mandates.get(mid)
    if m is None:
        raise Rejected(f"revocation for unknown mandate {mid}", code="unknown_mandate")
    m["revoked_at"] = ev.ts
    return _nothing(ev, f"mandate {mid} revoked")


def h_purchase_proposed(state: LedgerState, ev) -> JournalEntry:
    p = ev.payload["proposal"]
    state.proposals[p["proposal_id"]] = p
    return _nothing(ev, f"agent proposed cart {p['proposal_id']}")


def h_gate_decided(state: LedgerState, ev) -> JournalEntry:
    d = ev.payload["decision"]
    state.decisions[d["decision_id"]] = d
    return _nothing(ev, f"gate {d['verdict']} on {d.get('proposal_id', '?')}")


def h_payment_authorized(state: LedgerState, ev) -> JournalEntry:
    # Authorization moves no money on the merchant's book -- the customer's bank
    # has earmarked funds, nothing has settled. Recorded because the gap between
    # authorization and capture is where a revoked mandate has to be caught.
    return _nothing(ev, f"authorized {ev.payload.get('payment_id', '?')}")


def h_payment_failed(state: LedgerState, ev) -> JournalEntry:
    pid = ev.payload.get("payment_id", "")
    rec = state.payments.get(pid)
    if rec is not None:
        rec.status = "failed"
    return _nothing(ev, f"payment {pid} failed: {ev.payload.get('error_code', '')}")


def h_evidence_submitted(state: LedgerState, ev) -> JournalEntry:
    cid = ev.payload["chargeback_id"]
    cb = state.chargebacks.get(cid)
    if cb is None:
        raise Rejected(f"evidence for unknown chargeback {cid}", code="unknown_chargeback")
    cb.status = "evidence_submitted"
    cb.evidence_event_id = ev.event_id
    return _nothing(ev, f"representment packet filed for {cid}")


# --- the thin spine: these six move money -----------------------------------

def h_payment_captured(state: LedgerState, ev) -> JournalEntry:
    """Customer money is now the merchant's, sitting at the gateway.

    Dr PG Settlement Receivable (gross the customer paid)
        Cr Sales Revenue          (the tax-exclusive value of the supply)
        Cr GST Output Payable     (the tax the merchant now owes the exchequer)

    The gross is booked to the receivable, not to the bank: the money is at the
    PG and does not reach the bank until ``settlement_credited``. Collapsing
    those two into one entry is the mistake that makes a book impossible to
    reconcile against a settlement report, because the report is the only place
    the T+2 gap is visible.
    """
    p = ev.payload
    pid = p["payment_id"]
    if pid in state.payments:
        raise Rejected(f"payment {pid} already captured", code="duplicate_capture")
    gross = money(p["amount"])
    if gross <= ZERO:
        raise Rejected(f"non-positive capture {money_str(gross)}", code="bad_amount")
    category = p.get("category", "")
    taxable, tax = split_inclusive_gst(gross, category)

    state.payments[pid] = PaymentRecord(
        payment_id=pid, order_id=p.get("order_id", ""), amount=gross,
        currency=p.get("currency", "INR"), instrument=p.get("instrument", "card"),
        merchant_id=p.get("merchant_id", ""), category=category,
        mandate_id=p.get("mandate_id", ""), proposal_id=p.get("proposal_id", ""),
        decision_id=p.get("decision_id", ""), captured_at=ev.ts, status="captured",
    )
    return _entry(ev, [
        leg(PG_RECEIVABLE, debit=gross, memo=f"capture {pid}"),
        leg(SALES_REVENUE, credit=taxable, memo=f"taxable value, {category or 'uncategorised'}"),
        leg(GST_OUTPUT, credit=tax, memo="output GST on sale"),
    ], memo=f"captured {money_str(gross)} on {pid}")


def h_fee_debited(state: LedgerState, ev) -> JournalEntry:
    """What the gateway keeps, split by legal character rather than lumped.

    Dr Payment Gateway Fees (MDR)   -- an expense
    Dr GST Input Credit             -- an ASSET, recoverable, not a cost
    Dr TDS Receivable (194-O)       -- an asset, if the operator withheld
    Dr GST TCS Credit (s.52)        -- an asset, if the operator collected
        Cr PG Settlement Receivable -- the gateway pays out that much less

    The fee is recomputed here from the payment's own instrument and the
    schedule, then checked against what the event says the gateway charged. A
    mismatch is a rejection, not a silent overwrite: the whole point of holding
    our own book is to be able to disagree with the gateway.
    """
    p = ev.payload
    pid = p["payment_id"]
    rec = state.payments.get(pid)
    if rec is None:
        raise Rejected(f"fee for unknown payment {pid}", code="unknown_payment")
    if rec.fees_booked:
        raise Rejected(f"fees already booked for {pid}", code="duplicate_fee")

    schedule = _schedule_from(p.get("schedule"))
    computed = compute_fees(rec.amount, rec.instrument, schedule)

    claimed = p.get("mdr")
    if claimed is not None and money(claimed) != computed.mdr:
        raise Rejected(
            f"gateway MDR {money_str(claimed)} != computed {money_str(computed.mdr)} "
            f"for {pid} ({rec.instrument})",
            code="fee_mismatch")

    rec.fees_booked = True
    rec.net_settlement = computed.net_settlement
    legs = [
        leg(MDR_EXPENSE, debit=computed.mdr, memo=f"MDR on {pid} ({rec.instrument})"),
        leg(GST_INPUT, debit=computed.gst_on_mdr, memo="18% GST on MDR, recoverable"),
        leg(TDS_RECEIVABLE, debit=computed.tds, memo="s.194-O withholding"),
        leg(TCS_CREDIT, debit=computed.tcs, memo="s.52 GST TCS"),
        leg(RESERVE, debit=computed.reserve, memo="rolling reserve held at PG"),
        leg(PG_RECEIVABLE, credit=computed.total_deducted, memo=f"deductions on {pid}"),
    ]
    return _entry(ev, legs, memo=f"fees {money_str(computed.total_deducted)} on {pid}")


def h_refund_initiated(state: LedgerState, ev) -> JournalEntry:
    """The merchant owes the customer money back.

    Dr Sales Revenue        -- reverse the taxable value
    Dr GST Output Payable   -- reverse the output tax; a refunded sale is not a supply
        Cr Refunds Payable  -- a liability until the money actually leaves

    Reversing the output GST matters: a merchant that refunds without reversing
    it pays tax on revenue it never kept.
    """
    p = ev.payload
    rid = p["refund_id"]
    pid = p["payment_id"]
    if rid in state.refunds:
        raise Rejected(f"refund {rid} already initiated", code="duplicate_refund")
    rec = state.payments.get(pid)
    if rec is None:
        raise Rejected(f"refund for unknown payment {pid}", code="unknown_payment")
    amount = money(p["amount"])
    if amount <= ZERO:
        raise Rejected("non-positive refund", code="bad_amount")
    if rec.refunded + amount > rec.amount:
        raise Rejected(
            f"refund {money_str(amount)} exceeds unrefunded balance of {pid} "
            f"({money_str(rec.amount - rec.refunded)})",
            code="over_refund")

    taxable, tax = split_inclusive_gst(amount, rec.category)
    rec.refunded = money(rec.refunded + amount)
    rec.status = "refunded" if rec.refunded == rec.amount else "partially_refunded"
    state.refunds[rid] = RefundRecord(refund_id=rid, payment_id=pid, amount=amount)
    return _entry(ev, [
        leg(SALES_REVENUE, debit=taxable, memo=f"refund {rid} on {pid}"),
        leg(GST_OUTPUT, debit=tax, memo="reverse output GST"),
        leg(REFUNDS_PAYABLE, credit=amount, memo=f"owed back to customer"),
    ], memo=f"refund {money_str(amount)} initiated on {pid}")


def h_refund_settled(state: LedgerState, ev) -> JournalEntry:
    """The gateway has actually taken the refund out of the merchant's balance.

    Dr Refunds Payable
        Cr PG Settlement Receivable
    """
    rid = ev.payload["refund_id"]
    ref = state.refunds.get(rid)
    if ref is None:
        raise Rejected(f"settlement for uninitiated refund {rid}", code="orphan_refund")
    if ref.status == "settled":
        raise Rejected(f"refund {rid} already settled", code="duplicate_refund_settle")
    ref.status = "settled"
    return _entry(ev, [
        leg(REFUNDS_PAYABLE, debit=ref.amount, memo=f"refund {rid} paid out"),
        leg(PG_RECEIVABLE, credit=ref.amount, memo=f"debited by PG for {rid}"),
    ], memo=f"refund {money_str(ref.amount)} settled")


def h_settlement_credited(state: LedgerState, ev) -> JournalEntry:
    """A bulk PG payout lands in the bank.

    Dr Bank Account
        Cr PG Settlement Receivable

    The event names the payments in the batch, and the credited amount must
    equal the sum of their net settlements. If it does not, the entry is
    rejected and lands on the exception list -- which is the honest outcome, and
    the input the reconciliation agent works from.
    """
    p = ev.payload
    sid = p["settlement_id"]
    if sid in state.settlements:
        raise Rejected(f"settlement {sid} already credited", code="duplicate_settlement")
    payment_ids = list(p.get("payment_ids", []))
    expected = ZERO
    for pid in payment_ids:
        rec = state.payments.get(pid)
        if rec is None:
            raise Rejected(f"settlement {sid} names unknown payment {pid}",
                           code="unknown_payment")
        if not rec.fees_booked:
            raise Rejected(f"settlement {sid} includes {pid} before its fees were booked",
                           code="premature_settlement")
        if rec.settlement_id:
            raise Rejected(f"{pid} already settled in {rec.settlement_id}",
                           code="double_settlement")
        expected = money(expected + rec.net_settlement)

    # A payout is net of everything the gateway has already taken back. Refunds
    # that have settled against these payments and have not yet been netted into
    # an earlier payout come out here. Omitting this is why so many merchant
    # books show a permanently negative receivable that nobody can explain.
    netted_refunds = [r for r in state.refunds.values()
                      if r.status == "settled" and not r.netted_in
                      and r.payment_id in set(payment_ids)]
    deduction = money(sum((r.amount for r in netted_refunds), ZERO))
    expected = money(expected - deduction)

    credited = money(p["amount"])
    if credited != expected:
        raise Rejected(
            f"settlement {sid} credited {money_str(credited)} but the batch nets to "
            f"{money_str(expected)}",
            code="settlement_mismatch")

    for pid in payment_ids:
        state.payments[pid].settlement_id = sid
        state.payments[pid].status = "settled"
    for r in netted_refunds:
        r.netted_in = sid
    state.settlements[sid] = {"settlement_id": sid, "amount": credited,
                              "payment_ids": payment_ids, "ts": ev.ts,
                              "utr": p.get("utr", ""),
                              "refunds_netted": money_str(deduction)}
    return _entry(ev, [
        leg(BANK, debit=credited, memo=f"payout {sid} utr={p.get('utr', '')}"),
        leg(PG_RECEIVABLE, credit=credited, memo=f"{len(payment_ids)} payments settled"),
    ], memo=f"settled {money_str(credited)} across {len(payment_ids)} payments")


def h_chargeback_raised(state: LedgerState, ev) -> JournalEntry:
    """A dispute arrives. Recognise the loss now; reverse it only if we win.

    Dr Chargeback Losses
        Cr Chargeback Provision

    Provisioning on the raise rather than the loss is the conservative
    treatment, and it is the one that matches what an agent-initiated dispute
    actually is: the money is already gone from the merchant's control the
    moment the claim is filed.
    """
    p = ev.payload
    cid = p["chargeback_id"]
    pid = p["payment_id"]
    if cid in state.chargebacks:
        raise Rejected(f"chargeback {cid} already raised", code="duplicate_chargeback")
    rec = state.payments.get(pid)
    if rec is None:
        raise Rejected(f"chargeback on unknown payment {pid}", code="unknown_payment")
    amount = money(p.get("amount", rec.amount))
    if amount > rec.amount:
        raise Rejected(f"chargeback {money_str(amount)} exceeds capture "
                       f"{money_str(rec.amount)}", code="bad_amount")
    rec.chargeback_id = cid
    rec.status = "disputed"
    state.chargebacks[cid] = ChargebackRecord(
        chargeback_id=cid, payment_id=pid, amount=amount,
        reason_code=p.get("reason_code", ""))
    return _entry(ev, [
        leg(CHARGEBACK_LOSSES, debit=amount, memo=f"dispute {cid} on {pid}: "
                                                  f"{p.get('reason_code', '')}"),
        leg(CHARGEBACK_PROVISION, credit=amount, memo="provision pending outcome"),
    ], memo=f"chargeback {money_str(amount)} raised on {pid}")


def h_chargeback_lost(state: LedgerState, ev) -> JournalEntry:
    """The issuer sided with the cardholder. The provision becomes a real outflow.

    Dr Chargeback Provision
        Cr PG Settlement Receivable
    """
    cid = ev.payload["chargeback_id"]
    cb = state.chargebacks.get(cid)
    if cb is None:
        raise Rejected(f"outcome for unknown chargeback {cid}", code="unknown_chargeback")
    if cb.status in ("won", "lost"):
        raise Rejected(f"chargeback {cid} already resolved as {cb.status}",
                       code="duplicate_outcome")
    cb.status = "lost"
    state.payments[cb.payment_id].status = "charged_back"
    return _entry(ev, [
        leg(CHARGEBACK_PROVISION, debit=cb.amount, memo=f"{cid} lost"),
        leg(PG_RECEIVABLE, credit=cb.amount, memo="debited by PG"),
    ], memo=f"chargeback {cid} lost, {money_str(cb.amount)} recovered by issuer")


def h_chargeback_won(state: LedgerState, ev) -> JournalEntry:
    """Representment succeeded. Release the provision and reverse the loss.

    Dr Chargeback Provision
        Cr Chargeback Losses
    """
    cid = ev.payload["chargeback_id"]
    cb = state.chargebacks.get(cid)
    if cb is None:
        raise Rejected(f"outcome for unknown chargeback {cid}", code="unknown_chargeback")
    if cb.status in ("won", "lost"):
        raise Rejected(f"chargeback {cid} already resolved as {cb.status}",
                       code="duplicate_outcome")
    cb.status = "won"
    state.payments[cb.payment_id].status = "captured"
    return _entry(ev, [
        leg(CHARGEBACK_PROVISION, debit=cb.amount, memo=f"{cid} won, provision released"),
        leg(CHARGEBACK_LOSSES, credit=cb.amount, memo="reverse provisioned loss"),
    ], memo=f"chargeback {cid} defended successfully")


def h_reserve_held(state: LedgerState, ev) -> JournalEntry:
    amount = money(ev.payload["amount"])
    return _entry(ev, [
        leg(RESERVE, debit=amount, memo="rolling reserve"),
        leg(PG_RECEIVABLE, credit=amount, memo="withheld by PG"),
    ], memo=f"reserve {money_str(amount)} held")


def h_reserve_released(state: LedgerState, ev) -> JournalEntry:
    amount = money(ev.payload["amount"])
    if state.raw("1300") < amount:
        raise Rejected("release exceeds reserve held", code="bad_amount")
    return _entry(ev, [
        leg(PG_RECEIVABLE, debit=amount, memo="reserve released"),
        leg(RESERVE, credit=amount, memo="rolling reserve"),
    ], memo=f"reserve {money_str(amount)} released")


def h_recon_adjustment(state: LedgerState, ev) -> JournalEntry:
    """A reconciliation adjustment. Balanced by construction, checked anyway.

    Both legs and one amount come in, so the entry cannot be unbalanced. What
    this handler enforces is that the accounts are real and the amount is
    positive -- and then the engine's invariant suite gets the last word, which
    is the whole point: an adjustment proposed by an AI lands only if the book
    still holds afterwards.
    """
    p = ev.payload
    amount = money(p["amount"])
    if amount <= ZERO:
        raise Rejected("adjustment must be positive", code="bad_amount")
    for acct in (p["debit_account"], p["credit_account"]):
        if acct not in CHART:
            raise Rejected(f"account {acct} is not in the chart of accounts",
                           code="unknown_account")
    if p["debit_account"] == p["credit_account"]:
        raise Rejected("an adjustment to and from the same account is a no-op",
                       code="degenerate_entry")
    memo = p.get("memo", "reconciliation adjustment")
    return _entry(ev, [
        leg(p["debit_account"], debit=amount, memo=memo),
        leg(p["credit_account"], credit=amount, memo=memo),
    ], memo=f"recon adjustment {money_str(amount)}: {memo}")


def _schedule_from(spec) -> FeeSchedule:
    if not spec:
        return DEFAULT_SCHEDULE
    return FeeSchedule(
        mdr_by_instrument=dict(spec.get("mdr_by_instrument",
                                        DEFAULT_SCHEDULE.mdr_by_instrument)),
        gst_on_fee=str(spec.get("gst_on_fee", DEFAULT_SCHEDULE.gst_on_fee)),
        tds_194o=bool(spec.get("tds_194o", DEFAULT_SCHEDULE.tds_194o)),
        tcs_52=bool(spec.get("tcs_52", DEFAULT_SCHEDULE.tcs_52)),
        reserve_rate=str(spec.get("reserve_rate", DEFAULT_SCHEDULE.reserve_rate)),
    )


HANDLERS = {
    E.MANDATE_CREATED: h_mandate_created,
    E.MANDATE_REVOKED: h_mandate_revoked,
    E.AGENT_PURCHASE_PROPOSED: h_purchase_proposed,
    E.GATE_DECIDED: h_gate_decided,
    E.PAYMENT_AUTHORIZED: h_payment_authorized,
    E.PAYMENT_CAPTURED: h_payment_captured,
    E.PAYMENT_FAILED: h_payment_failed,
    E.FEE_DEBITED: h_fee_debited,
    E.REFUND_INITIATED: h_refund_initiated,
    E.REFUND_SETTLED: h_refund_settled,
    E.CHARGEBACK_RAISED: h_chargeback_raised,
    E.EVIDENCE_SUBMITTED: h_evidence_submitted,
    E.CHARGEBACK_WON: h_chargeback_won,
    E.CHARGEBACK_LOST: h_chargeback_lost,
    E.SETTLEMENT_CREDITED: h_settlement_credited,
    E.RESERVE_HELD: h_reserve_held,
    E.RESERVE_RELEASED: h_reserve_released,
    E.RECON_ADJUSTMENT: h_recon_adjustment,
}
