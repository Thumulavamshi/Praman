"""Reconciliation: the matcher, the corruptions, and the verifier that says no."""
import json
from pathlib import Path

import pytest

from praman.ledger.engine import Engine
from praman.ledger.generator import generate_stream, resolve_settlement_amounts
from praman.ledger.invariants import check_all
from praman.ledger.money import money
from praman.recon.matcher import _expected_payout, reconcile
from praman.recon.models import BankLine, Exception_, MatchReport, Resolution
from praman.recon.sources import (CORRUPTIONS, bank_statement, corrupt,
                                  settlement_file)
from praman.recon.verify import verify

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def book():
    cat = json.loads((ROOT / "data" / "catalog.json").read_text())["products"]
    e = Engine()
    e.apply_many(resolve_settlement_amounts(generate_stream(
        seed=7, n_payments=240, settle_batch=10, catalog=cat)))
    e.drain_quarantine()
    return e


@pytest.fixture(scope="module")
def sources(book):
    rows = settlement_file(book)
    return rows, bank_statement(book, rows)


# --- the control ------------------------------------------------------------

def test_a_correct_pair_reconciles_to_nothing(book, sources):
    """Without this, no corruption result means anything."""
    rows, lines = sources
    r = reconcile(book, rows, lines)
    assert r.exceptions == []
    assert r.unmatched_bank == [] and r.unmatched_settlements == []
    assert r.auto_match_rate(len(book.state.settlements)) == 1.0


# --- each break fails in its own way ----------------------------------------

@pytest.mark.parametrize("kind,expect_exception", [
    ("fee_drift", True),
    ("split_payout", True),
    ("missing_credit", True),
    ("orphan_credit", True),
    ("duplicate_credit", True),
    # These two the deterministic matcher recovers on its own -- a mangled
    # description still credits the right rupees, and an id match does not care
    # what date the money landed. That is the matcher earning its keep.
    ("mangled_utr", False),
    ("date_shift", False),
])
def test_each_corruption_produces_the_exception_it_should(book, sources, kind,
                                                          expect_exception):
    rows, lines = sources
    cr, cl, truth = corrupt(rows, lines, seed=3, kinds=(kind,), rate=1.0)
    assert truth, f"{kind} injected nothing"
    r = reconcile(book, cr, cl)
    assert bool(r.exceptions) is expect_exception


def test_a_fee_drift_is_reported_as_a_disagreement_with_the_gateway(book, sources):
    rows, lines = sources
    cr, cl, _ = corrupt(rows, lines, seed=3, kinds=("fee_drift",), rate=1.0)
    r = reconcile(book, cr, cl)
    assert all(e.kind == "fee_mismatch" for e in r.exceptions)
    assert "our book says" in r.exceptions[0].detail


def test_the_matcher_never_guesses_between_two_equal_amounts(book, sources):
    """An ambiguous amount is left for a human, not resolved by coin flip."""
    rows, lines = sources
    first = lines[0]
    twin = BankLine("bank_twin", first.value_date, first.amount, "NEFT CR MANGLED")
    mangled = [BankLine(l.line_id, l.value_date, l.amount, "NEFT CR MANGLED")
               if l is first else l for l in lines] + [twin]
    r = reconcile(book, rows, mangled)
    assert r.unmatched_settlements, "an ambiguous pair should not be auto-matched"


def test_reconciliation_reads_the_payout_from_our_book_not_the_gateways_file(book,
                                                                            sources):
    """Otherwise a fee error reconciles perfectly against itself."""
    rows, lines = sources
    sid = rows[0].settlement_id
    srows = [r for r in rows if r.settlement_id == sid]
    assert _expected_payout(book, sid, srows) == money(
        book.state.settlements[sid]["amount"])


# --- the verifier -----------------------------------------------------------

@pytest.fixture
def rep():
    return MatchReport(exceptions=[
        Exception_("s1", "unmatched_settlement", "", money("300.00"))])


@pytest.fixture
def two_lines():
    return [BankLine("b1", "2026-09-05", money("100.00"), "x"),
            BankLine("b2", "2026-09-05", money("200.00"), "y")]


def test_a_split_that_sums_exactly_is_accepted(book, rep, two_lines):
    v = verify(book, rep, Resolution("split_match", "", settlement_id="s1",
                                     bank_line_ids=["b1", "b2"]),
               two_lines, expected_payout=money("300.00"))
    assert v.accepted


def test_a_split_that_is_off_by_a_paisa_is_refused(book, rep, two_lines):
    """No tolerance. A tolerance hides a systematic fee error inside itself."""
    v = verify(book, rep, Resolution("split_match", "", settlement_id="s1",
                                     bank_line_ids=["b1", "b2"]),
               two_lines, expected_payout=money("300.01"))
    assert not v.accepted and "out by" in v.reason


def test_claiming_a_credit_twice_is_refused(book, two_lines):
    """The error that makes a reconciliation worse than not doing one."""
    r = MatchReport(matched_settlements={"s0": ["b1"]},
                    exceptions=[Exception_("s1", "x", "", money("100.00"))])
    v = verify(book, r, Resolution("match", "", settlement_id="s1",
                                   bank_line_ids=["b1"]),
               two_lines, expected_payout=money("100.00"))
    assert not v.accepted and "already matched" in v.reason


def test_an_adjustment_may_resolve_a_real_difference_but_not_invent_one(book, rep,
                                                                       two_lines):
    good = Resolution("adjusting_entry", "", debit_account="5000",
                      credit_account="1200", amount="300.00")
    assert verify(book, rep, good, two_lines).accepted

    invented = Resolution("adjusting_entry", "", debit_account="5000",
                          credit_account="1200", amount="4200.00")
    v = verify(book, rep, invented, two_lines)
    assert not v.accepted and "does not correspond" in v.reason


def test_an_adjustment_to_an_account_outside_the_chart_is_refused(book, rep,
                                                                 two_lines):
    v = verify(book, rep, Resolution("adjusting_entry", "", debit_account="9999",
                                     credit_account="1200", amount="300.00"),
               two_lines)
    assert not v.accepted and "not in the chart" in v.reason


def test_escalation_is_always_accepted(book, rep, two_lines):
    """Deferring to a human is a correct answer, not a failure to close."""
    assert verify(book, rep, Resolution("escalate", "cannot tell"),
                  two_lines).accepted


def test_an_accepted_adjustment_lands_in_the_book_and_leaves_it_clean():
    e = Engine()
    r = MatchReport(exceptions=[Exception_("p1", "fee_mismatch", "",
                                           money("0.02"))])
    v = verify(e, r, Resolution("adjusting_entry", "", debit_account="5000",
                                credit_account="1200", amount="0.02",
                                memo="fee drift"),
               [], apply=True, event_id="ev_adj_1")
    assert v.accepted and v.applied_event
    assert e.state.balance("5000") == money("0.02")
    assert check_all(e.state) == []


def test_the_ledger_gets_the_last_word_on_an_adjustment():
    """The engine's invariant suite runs on it like any other entry."""
    e = Engine()
    r = MatchReport(exceptions=[Exception_("p1", "x", "", money("5.00"))])
    v = verify(e, r, Resolution("adjusting_entry", "", debit_account="5000",
                                credit_account="5000", amount="5.00"),
               [], apply=True, event_id="ev_adj_degenerate")
    assert not v.accepted and "ledger refused it" in v.reason
    assert e.state.nonzero_balances() == {}
