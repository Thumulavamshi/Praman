"""A run where the adjudicator was unreachable is not a measurement.

This is the failure mode that nearly got a meaningless number published. It is
worth a test of its own because of the SHAPE of it: the safety property working
perfectly is exactly what disguises the outage. Every failed adjudication fails
closed to STEP_UP, a step-up is a legitimate verdict, so the metrics count it
like any other and the report comes out looking conservative and tidy -- zero
false allows, zero false blocks, and an accuracy figure over answers the model
never gave.
"""
from praman.eval.harness import CaseResult
from praman.eval.metrics import degraded


def case(cid, expected, actual, *, consulted=True, error=""):
    return CaseResult(case_id=cid, bucket="C", expected=expected, actual=actual,
                      amount="100.00", latency_ms=1.0,
                      adjudicator_latency_ms=0.0, consulted=consulted,
                      error=error, heldout=True, source="authored")


def test_a_clean_run_is_not_flagged():
    results = [case(f"c{i}", "ALLOW", "ALLOW") for i in range(10)]
    assert degraded(results) is None


def test_an_outage_is_caught_even_though_every_verdict_is_safe():
    """The giveaway is not a wrong answer. There are no wrong answers of the
    dangerous kind -- that is the point. It is that the model never replied."""
    good = [case(f"ok{i}", "ALLOW", "ALLOW") for i in range(20)]
    out = [case(f"bad{i}", "ALLOW", "STEP_UP",
                error="ClientError: 429 RESOURCE_EXHAUSTED") for i in range(30)]
    bounds = [case(f"det{i}", "BLOCK", "BLOCK", consulted=False) for i in range(50)]

    d = degraded(good + out + bounds)
    assert d is not None
    assert d["n_errored"] == 30
    assert d["n_total"] == 100
    assert d["n_consulted"] == 50
    assert d["share_of_all"] == 0.30
    assert d["share_of_consulted"] == 0.60
    assert d["distinct"][0] == ("ClientError: 429 RESOURCE_EXHAUSTED", 30)


def test_the_distinct_causes_are_ranked_so_the_real_one_is_first():
    results = ([case(f"a{i}", "ALLOW", "STEP_UP", error="quota exhausted")
                for i in range(9)]
               + [case("b", "ALLOW", "STEP_UP", error="connection reset")])
    d = degraded(results)
    assert [n for _, n in d["distinct"]] == [9, 1]
    assert d["distinct"][0][0] == "quota exhausted"


def test_the_committed_degraded_run_is_still_recognised_as_one():
    """out/generated_degraded_credit_exhausted.json is kept deliberately, as
    evidence that the fail-closed path was exercised rather than asserted. The
    check must keep recognising it, or the tool has stopped protecting the one
    file that documents why it exists."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    raw = json.loads(
        (root / "out" / "generated_degraded_credit_exhausted.json").read_text())
    results = [case(r["case_id"], r["expected"], r["actual"],
                    consulted=r.get("consulted", False), error=r.get("error", ""))
               for r in raw["results"]]
    d = degraded(results)
    assert d["n_errored"] == 302
    assert d["share_of_consulted"] == 1.0
    # One cause, not 302 distinct ones: the messages carry a per-request id, so
    # they must be truncated before they are counted or the summary degenerates
    # into a list as long as the outage.
    assert len(d["distinct"]) == 1
    assert "credit balance is too low" in d["distinct"][0][0]


def test_a_dead_adjudicator_aborts_the_run_instead_of_burning_the_quota():
    """Two full held-out runs were spent discovering the same dead adjudicator.

    Each took twenty minutes, spent a day's free-tier quota, and produced a
    report that looked plausible -- because every failed adjudication becomes a
    fail-closed STEP_UP and the metrics count it like any other verdict. Five
    calls is enough to learn what the whole run would have said.
    """
    import pytest
    from praman.data.heldout import HELDOUT_CASES
    from praman.eval.harness import AdjudicatorDown, run
    from praman.gate.adjudicator import AdjudicationResult
    from praman.gate.gate import Gate
    from praman.data.mandates import ISSUER

    class DeadAdjudicator:
        """Fails exactly the way a rejected request fails: every time, alike."""

        def adjudicate(self, mandate, proposal, bounds):
            return AdjudicationResult(
                verdict="STEP_UP", reason="adjudicator unavailable (ClientError)",
                cited_clause="(adjudicator error)", confidence="low",
                listing_attempted_instruction=False,
                error="ClientError: 400 INVALID_ARGUMENT")

    cases = [c for c in HELDOUT_CASES if c.bucket == "C"][:40]
    with pytest.raises(AdjudicatorDown) as caught:
        run(cases, lambda: Gate(adjudicator=DeadAdjudicator(), issuer=ISSUER,
                                always_consult=True),
            workers=1, measure_clean_twins=False)
    assert "ClientError: 400 INVALID_ARGUMENT" in str(caught.value)
    assert caught.value.n == 5


def test_fail_fast_can_be_switched_off_to_capture_a_degraded_run():
    """out/generated_degraded_credit_exhausted.json only exists because a run
    was allowed to finish while failing. That evidence is worth keeping, so the
    abort has to be defeatable on purpose."""
    from praman.data.heldout import HELDOUT_CASES
    from praman.eval.harness import run
    from praman.gate.adjudicator import AdjudicationResult
    from praman.gate.gate import Gate
    from praman.data.mandates import ISSUER

    class DeadAdjudicator:
        def adjudicate(self, mandate, proposal, bounds):
            return AdjudicationResult(
                verdict="STEP_UP", reason="down", cited_clause="",
                confidence="low", listing_attempted_instruction=False,
                error="ClientError: 400")

    cases = [c for c in HELDOUT_CASES if c.bucket == "C"][:10]
    results = run(cases, lambda: Gate(adjudicator=DeadAdjudicator(),
                                      issuer=ISSUER, always_consult=True),
                  workers=1, measure_clean_twins=False, fail_fast=0)
    assert len(results) == 10
    assert all(r.error for r in results if r.consulted)
