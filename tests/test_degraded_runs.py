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
