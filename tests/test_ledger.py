"""The spine: idempotency, the guards, invariants, as-of replay, chaos."""
from decimal import Decimal

import pytest

from praman.ledger.engine import Engine
from praman.ledger.generator import (generate_stream, inject_chaos,
                                     resolve_settlement_amounts)
from praman.ledger.invariants import check_all
from praman.ledger.journal import Rejected

TS = "2026-09-02T10:00:00+05:30"


def ev(eid, etype, payload, ts=TS):
    return {"event_id": eid, "type": etype, "ts": ts, "payload": payload}


def capture(eid="e1", pid="pay_1", amount="1000.00", instrument="card",
            category="groceries", **extra):
    return ev(eid, "payment_captured",
              {"payment_id": pid, "amount": amount, "instrument": instrument,
               "category": category, **extra})


# --- the seen-gate ----------------------------------------------------------

def test_a_redelivered_event_is_absorbed_not_double_booked():
    e = Engine()
    e.apply_event(capture())
    before = e.state.balance("1200")
    r = e.apply_event(capture())
    assert r.status == "duplicate"
    assert e.state.balance("1200") == before


def test_the_seen_gate_keys_on_the_producer_id_not_the_payload():
    """A retry that changes the payload is still the same event, and loses."""
    e = Engine()
    e.apply_event(capture(amount="1000.00"))
    e.apply_event(capture(amount="9999.00"))
    assert e.state.payments["pay_1"].amount == Decimal("1000.00")


# --- guard 1: rejection is a normal outcome ---------------------------------

def test_an_effect_arriving_before_its_cause_is_rejected_then_applies_on_retry():
    e = Engine()
    r = e.apply_event(ev("e_fee", "fee_debited", {"payment_id": "pay_1"}))
    assert r.status == "rejected" and "unknown payment" in r.detail
    e.apply_event(capture())
    r2 = e.apply_event(ev("e_fee", "fee_debited", {"payment_id": "pay_1"}))
    assert r2.status == "applied"


def test_a_rejection_leaves_no_trace_in_the_book():
    e = Engine()
    e.apply_event(ev("bad", "refund_settled", {"refund_id": "nope"}))
    assert e.state.nonzero_balances() == {}
    assert len(e.eventlog) == 0          # refused events never enter the log
    assert e.quarantine[0]["code"] == "orphan_refund"


def test_over_refunding_is_refused():
    e = Engine()
    e.apply_event(capture())
    e.apply_event(ev("r1", "refund_initiated",
                     {"refund_id": "rf1", "payment_id": "pay_1", "amount": "600.00"}))
    r = e.apply_event(ev("r2", "refund_initiated",
                         {"refund_id": "rf2", "payment_id": "pay_1", "amount": "600.00"}))
    assert r.status == "rejected" and "exceeds unrefunded balance" in r.detail


def test_the_ledger_is_allowed_to_disagree_with_the_gateway_about_a_fee():
    """A gateway that charges more than the schedule says is a rejection, not a fact."""
    e = Engine()
    e.apply_event(capture())
    r = e.apply_event(ev("f", "fee_debited", {"payment_id": "pay_1", "mdr": "45.00"}))
    assert r.status == "rejected" and "fee_mismatch" == e.quarantine[0]["code"]


# --- guard 2: a handler crash costs one event -------------------------------

def test_a_crashing_handler_costs_exactly_one_event_and_the_run_continues():
    def explodes(state, event):
        raise ZeroDivisionError("handler bug")

    e = Engine()
    e.handlers["payment_captured"] = explodes
    r = e.apply_event(capture())
    assert r.status == "error" and "ZeroDivisionError" in r.detail
    e.handlers["payment_captured"] = Engine().handlers["payment_captured"]
    assert e.apply_event(capture(eid="e2", pid="pay_2")).status == "applied"
    assert len(e.errors) == 1


# --- guard 3: the verifier outranks the handler -----------------------------

def test_an_entry_that_breaks_an_invariant_is_rolled_back_not_logged():
    """The verifier can reject a handler, and the book returns to its last good state."""
    from praman.ledger.journal import JournalEntry
    from praman.ledger.money import leg

    e = Engine()
    sound = e.handlers["payment_captured"]

    def unbalanced_for_pay_2(state, event):
        # Only pay_2 is broken. The engine rolls back by re-folding the log, so
        # a handler that misbehaved for every event would also corrupt the
        # rollback -- and the property under test is the rollback, not that.
        if event.payload["payment_id"] != "pay_2":
            return sound(state, event)
        return JournalEntry(event.event_id, event.type, event.ts,
                            [leg("1100", debit="100.00")])

    e.apply_event(capture())
    good = dict(e.state.nonzero_balances())
    e.handlers["payment_captured"] = unbalanced_for_pay_2
    r = e.apply_event(capture(eid="e2", pid="pay_2"))
    assert r.status == "refused"
    assert e.state.nonzero_balances() == good
    assert e.verify()[1] == []


def test_a_capture_with_a_mandate_but_no_gate_decision_is_refused():
    """No money moves without a decision behind it. This is the trust property."""
    e = Engine()
    r = e.apply_event(capture(mandate_id="mnd_1", decision_id="dec_missing"))
    assert r.status == "refused"
    assert "undecided_capture" in r.detail
    assert e.state.nonzero_balances() == {}


def test_a_capture_against_a_block_verdict_is_refused():
    e = Engine()
    e.apply_event(ev("d", "gate_decided",
                     {"decision": {"decision_id": "dec_1", "verdict": "BLOCK",
                                   "proposal_id": "prop_1"}}))
    r = e.apply_event(capture(mandate_id="mnd_1", decision_id="dec_1"))
    assert r.status == "refused" and "captured_against_verdict" in r.detail


def test_an_allowed_decision_lets_the_capture_through():
    e = Engine()
    e.apply_event(ev("d", "gate_decided",
                     {"decision": {"decision_id": "dec_1", "verdict": "ALLOW",
                                   "proposal_id": "prop_1"}}))
    assert e.apply_event(capture(mandate_id="mnd_1", decision_id="dec_1")).status == "applied"


# --- the accounting ---------------------------------------------------------

def test_the_full_lifecycle_books_correctly_and_leaves_the_receivable_flat():
    e = Engine()
    e.apply_many([
        capture(),
        ev("f", "fee_debited", {"payment_id": "pay_1", "mdr": "20.00"}),
        ev("r1", "refund_initiated",
           {"refund_id": "rf1", "payment_id": "pay_1", "amount": "200.00"}),
        ev("r2", "refund_settled", {"refund_id": "rf1"}),
        ev("s", "settlement_credited",
           {"settlement_id": "setl_1", "payment_ids": ["pay_1"], "amount": "776.40"}),
    ])
    st = e.state
    assert st.balance("1100") == Decimal("776.40")     # bank
    assert st.balance("1200") == Decimal("0.00")       # receivable clears
    assert st.balance("1400") == Decimal("3.60")       # GST input credit, an asset
    assert st.balance("5000") == Decimal("20.00")      # MDR expense, not 23.60
    assert st.balance("4000") == Decimal("761.90")     # 800 net of 5% GST
    assert st.balance("2200") == Decimal("38.10")      # output GST payable
    assert check_all(st) == []


def test_gst_on_mdr_is_an_asset_and_never_lands_in_the_expense_line():
    e = Engine()
    e.apply_event(capture())
    e.apply_event(ev("f", "fee_debited", {"payment_id": "pay_1"}))
    assert e.state.balance("5000") == Decimal("20.00")
    assert e.state.balance("1400") == Decimal("3.60")


def test_a_defended_chargeback_reverses_the_provisioned_loss():
    e = Engine()
    e.apply_event(capture())
    e.apply_event(ev("cb", "chargeback_raised",
                     {"chargeback_id": "cb_1", "payment_id": "pay_1", "amount": "1000.00"}))
    assert e.state.balance("5100") == Decimal("1000.00")
    e.apply_event(ev("cbw", "chargeback_won", {"chargeback_id": "cb_1"}))
    assert e.state.balance("5100") == Decimal("0.00")
    assert e.state.balance("2300") == Decimal("0.00")
    assert check_all(e.state) == []


def test_a_settlement_that_disagrees_with_the_batch_is_rejected():
    e = Engine()
    e.apply_event(capture())
    e.apply_event(ev("f", "fee_debited", {"payment_id": "pay_1"}))
    r = e.apply_event(ev("s", "settlement_credited",
                         {"settlement_id": "s1", "payment_ids": ["pay_1"],
                          "amount": "999.00"}))
    assert r.status == "rejected" and "nets to" in r.detail


# --- as-of replay -----------------------------------------------------------

def test_as_of_replay_reconstructs_the_book_at_the_disputed_moment(catalog):
    stream = resolve_settlement_amounts(
        generate_stream(seed=3, n_payments=30, catalog=catalog))
    e = Engine()
    e.apply_many(stream)
    e.drain_quarantine()

    mid = e.eventlog.events()[len(e.eventlog) // 2]
    snap = e.snapshot_as_of(mid.event_id)
    assert len(snap.entries) <= len(e.state.entries)
    assert check_all(snap) == []
    # and it is stable: asking twice gives the same answer
    assert snap.trial_balance() == e.snapshot_as_of(mid.event_id).trial_balance()


def test_the_hash_chain_names_the_first_tampered_record(catalog):
    e = Engine()
    e.apply_many(resolve_settlement_amounts(
        generate_stream(seed=4, n_payments=10, catalog=catalog)))
    assert e.eventlog.verify_chain() == (True, None)

    victim = e.eventlog.events()[3]
    victim.payload["amount"] = "99999.00"        # a merchant rewriting history
    ok, bad = e.eventlog.verify_chain()
    assert ok is False and bad == victim.event_id


# --- chaos ------------------------------------------------------------------

def test_the_book_is_byte_identical_under_duplicates_reorders_and_rewinds(catalog):
    clean = resolve_settlement_amounts(
        generate_stream(seed=7, n_payments=120, catalog=catalog))

    a = Engine()
    a.apply_many(clean)
    a.drain_quarantine()

    b = Engine()
    b.apply_many(inject_chaos(clean, seed=99))
    b.drain_quarantine()

    assert b.state.trial_balance() == a.state.trial_balance()
    assert len(b.state.entries) == len(a.state.entries)
    assert check_all(b.state) == []
    assert b.quarantine == []


def test_a_clean_stream_produces_no_invariant_violations(catalog):
    e = Engine()
    e.apply_many(resolve_settlement_amounts(
        generate_stream(seed=11, n_payments=200, catalog=catalog)))
    e.drain_quarantine()
    ok, violations, bad = e.verify()
    assert ok and violations == [] and bad is None
