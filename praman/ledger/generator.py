"""Seeded event-stream generator, with deliberate chaos.

We generate our own ledger events rather than pulling them from anywhere. Three
reasons, in order of how much they matter:

  1. **We own the ground truth.** It is the only reason we can measure anything.
     A stream we did not author is a stream whose correct book we do not know.
  2. **Reproducibility.** A seed produces a byte-identical stream, so the demo
     runs the same way on stage as it did in rehearsal, and a regression is a
     regression rather than a different sample.
  3. **Deliberate chaos.** Real webhook delivery duplicates, reorders, and
     replays. A generator that only emits clean streams tests the happy path and
     tells you nothing about the property we actually claim -- that the book is
     byte-identical under redelivery.

``chaos`` is applied as a *transform on an already-correct stream*, never woven
into generation. That separation is what makes the replay test meaningful: the
clean stream and the chaotic stream must fold to the same book, and if chaos
were part of generation there would be no clean stream to compare against.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import events as E
from .fees import compute_fees
from .money import D, money, money_str

IST = timezone(timedelta(hours=5, minutes=30))

INSTRUMENTS = ["upi", "card", "netbanking", "wallet"]
INSTRUMENT_WEIGHTS = [0.45, 0.35, 0.15, 0.05]


def _ts(base: datetime, minutes: float) -> str:
    return (base + timedelta(minutes=minutes)).isoformat()


def generate_stream(
    *,
    seed: int = 7,
    n_payments: int = 120,
    refund_rate: float = 0.08,
    chargeback_rate: float = 0.04,
    chargeback_win_rate: float = 0.5,
    settle_batch: int = 20,
    start: datetime | None = None,
    catalog: list[dict] | None = None,
) -> list[dict]:
    """A well-formed stream: captures, fees, refunds, settlements, disputes.

    Ordering is causal by construction -- fees follow their capture, settlements
    follow their fees, refunds net into the payout that follows them. Every
    amount that appears in a ``fee_debited`` is computed from the same schedule
    the handler will use, so a mismatch in the test means a real disagreement,
    not a fixture that drifted.
    """
    rng = random.Random(seed)
    base = start or datetime(2026, 8, 1, 9, 0, tzinfo=IST)
    catalog = catalog or _fallback_catalog()

    out: list[dict] = []
    t = 0.0
    unsettled: list[tuple[str, str]] = []      # (payment_id, refund_id or "")
    refunds_pending: dict[str, str] = {}       # payment_id -> refund_id
    batch_no = 0

    for i in range(n_payments):
        t += rng.uniform(3, 40)
        item = rng.choice(catalog)
        pid = f"pay_{seed}_{i:04d}"
        oid = f"order_{seed}_{i:04d}"
        instrument = rng.choices(INSTRUMENTS, INSTRUMENT_WEIGHTS)[0]
        amount = money(item["price"])

        out.append({
            "event_id": f"ev_cap_{pid}", "type": E.PAYMENT_CAPTURED, "ts": _ts(base, t),
            "payload": {
                "payment_id": pid, "order_id": oid, "amount": money_str(amount),
                "currency": "INR", "instrument": instrument,
                "merchant_id": item["merchant_id"], "category": item["category"],
                "sku": item["sku"],
            },
        })
        fees = compute_fees(amount, instrument)
        out.append({
            "event_id": f"ev_fee_{pid}", "type": E.FEE_DEBITED, "ts": _ts(base, t + 0.1),
            "payload": {"payment_id": pid, "mdr": money_str(fees.mdr)},
        })

        if rng.random() < refund_rate:
            rid = f"rfnd_{seed}_{i:04d}"
            # Half of a rupee amount is a rounding event like any other: it goes
            # through money(), and the multiplier is a Decimal, never 0.5 the float.
            part = amount if rng.random() < 0.6 else money(amount * D("0.5"))
            out.append({
                "event_id": f"ev_rfi_{rid}", "type": E.REFUND_INITIATED,
                "ts": _ts(base, t + 120),
                "payload": {"refund_id": rid, "payment_id": pid,
                            "amount": money_str(part)},
            })
            out.append({
                "event_id": f"ev_rfs_{rid}", "type": E.REFUND_SETTLED,
                "ts": _ts(base, t + 180),
                "payload": {"refund_id": rid},
            })
            refunds_pending[pid] = money_str(part)

        unsettled.append((pid, instrument))

        if len(unsettled) >= settle_batch:
            out.append(_settlement(seed, batch_no, unsettled, refunds_pending,
                                   base, t + 240))
            batch_no += 1
            unsettled = []
            refunds_pending = {}

    if unsettled:
        out.append(_settlement(seed, batch_no, unsettled, refunds_pending,
                               base, t + 240))

    # Disputes land well after settlement, which is exactly what makes them hard:
    # the money is already in the bank and the book has moved on.
    captures = [e for e in out if e["type"] == E.PAYMENT_CAPTURED]
    for e in captures:
        if rng.random() >= chargeback_rate:
            continue
        pid = e["payload"]["payment_id"]
        cid = f"cb_{pid}"
        t += rng.uniform(60, 400)
        out.append({
            "event_id": f"ev_cbr_{cid}", "type": E.CHARGEBACK_RAISED,
            "ts": _ts(base, t + 3000),
            "payload": {"chargeback_id": cid, "payment_id": pid,
                        "amount": e["payload"]["amount"],
                        "reason_code": rng.choice([
                            "10.4_other_fraud_card_absent",
                            "13.1_merchandise_not_received",
                            "13.7_cancelled_recurring",
                            "agent_not_authorized",
                        ])},
        })
        won = rng.random() < chargeback_win_rate
        out.append({
            "event_id": f"ev_cbo_{cid}",
            "type": E.CHARGEBACK_WON if won else E.CHARGEBACK_LOST,
            "ts": _ts(base, t + 8000),
            "payload": {"chargeback_id": cid},
        })

    return out


def _settlement(seed, batch_no, unsettled, refunds_pending, base, t):
    from .fees import compute_fees as _cf
    sid = f"setl_{seed}_{batch_no:03d}"
    total = money(0)
    ids = []
    for pid, instrument in unsettled:
        ids.append(pid)
    # The generator recomputes the payout the same way the handler will. Any
    # disagreement is a genuine defect in one of them, which is the point.
    return {
        "event_id": f"ev_set_{sid}", "type": E.SETTLEMENT_CREDITED,
        "ts": _ts(base, t),
        "payload": {"settlement_id": sid, "payment_ids": ids,
                    "amount": "__COMPUTED__", "utr": f"UTR{seed}{batch_no:05d}",
                    "refunds_netted": refunds_pending},
    }


def resolve_settlement_amounts(stream: list[dict]) -> list[dict]:
    """Fill in the ``__COMPUTED__`` payout amounts by replaying the stream.

    The generator cannot know a batch's payout until it knows every fee and
    refund in it, so it emits a placeholder and this pass resolves it. Doing it
    as a second pass rather than with lookahead keeps the generator's ordering
    honestly causal.
    """
    from .engine import Engine

    probe = Engine()
    resolved: list[dict] = []
    for e in stream:
        if e["type"] == E.SETTLEMENT_CREDITED and e["payload"]["amount"] == "__COMPUTED__":
            expected = money(0)
            for pid in e["payload"]["payment_ids"]:
                rec = probe.state.payments.get(pid)
                if rec is not None:
                    expected = money(expected + rec.net_settlement)
            netted = money(0)
            for r in probe.state.refunds.values():
                if (r.status == "settled" and not r.netted_in
                        and r.payment_id in set(e["payload"]["payment_ids"])):
                    netted = money(netted + r.amount)
            e = {**e, "payload": {**e["payload"],
                                  "amount": money_str(money(expected - netted))}}
            e["payload"].pop("refunds_netted", None)
        resolved.append(e)
        probe.apply_event(e)
    return resolved


def inject_chaos(stream: list[dict], *, seed: int = 99,
                 duplicate_rate: float = 0.12,
                 reorder_window: int = 4,
                 rewind_rate: float = 0.03) -> list[dict]:
    """Redeliver, reorder, and rewind an already-correct stream.

    - **duplicate**: the same event delivered twice. The seen-gate must absorb it.
    - **reorder**: local shuffling within a window, so effects can precede causes.
      The affected handlers reject, and the redelivery applies.
    - **rewind**: a slice of already-delivered events replayed from the start,
      which is what a gateway does after an outage.

    None of this changes the correct answer. That is the claim the chaos test
    makes: same book, byte for byte.
    """
    rng = random.Random(seed)
    out: list[dict] = []
    for i, e in enumerate(stream):
        out.append(e)
        if rng.random() < duplicate_rate:
            out.append(dict(e))
        if rewind_rate and rng.random() < rewind_rate and i > 12:
            out.extend(dict(x) for x in stream[max(0, i - 12):i - 6])
    # local reordering
    i = 0
    while i < len(out) - reorder_window:
        if rng.random() < 0.25:
            window = out[i:i + reorder_window]
            rng.shuffle(window)
            out[i:i + reorder_window] = window
        i += reorder_window
    return out


def _fallback_catalog() -> list[dict]:
    return [
        {"sku": "GRO-0001", "price": "285.00", "category": "groceries",
         "merchant_id": "mch_dailymart"},
        {"sku": "HOU-0001", "price": "199.00", "category": "household",
         "merchant_id": "mch_homeneeds"},
        {"sku": "GRO-0002", "price": "649.00", "category": "groceries",
         "merchant_id": "mch_dailymart"},
    ]
