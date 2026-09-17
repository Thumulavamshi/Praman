"""Running the evaluation set through the gate, and re-grading a stored run.

Two entry points, and the second is the more valuable one.

``run`` executes cases against a live gate. ``regrade`` replays a *stored* run
through the current metrics code without spending a rupee on the model. That
separation is what makes the numbers cheap to iterate on: change how a metric is
computed, re-grade every historical run, and see whether the change moves
anything. Re-running 500 adjudications to find out that a denominator was wrong
is how a measurement budget disappears.

Every run is written out in full -- per-case verdicts, reasons, latencies, token
counts -- so any number in the report can be traced back to the case that
produced it.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path

from praman.gate.adjudicator import prompt_fingerprint
from praman.data.cases import EvalCase
from praman.gate.gate import Gate
from praman.ledger.money import money
from .metrics import CaseResult


def _clean_twin(case: EvalCase) -> EvalCase:
    """The same cart with the injection removed. Ground truth for bucket D.

    Measuring resistance against the stored label would conflate two different
    failures: an attack that worked, and a case the gate gets wrong with or
    without the attack. Only the first is an injection failure, and only the
    clean twin separates them.
    """
    return EvalCase(**{**asdict(case), "attack": None,
                       "case_id": case.case_id + "__clean"})


def run_case(gate: Gate, case: EvalCase) -> CaseResult:
    out = gate.decide(case.mandate(), case.proposal(), case.spend_history(),
                      revoked=case.revoked)
    d = out.decision
    return CaseResult(
        case_id=case.case_id, bucket=case.bucket, expected=case.expected,
        actual=d.verdict, amount=money(case.proposal().amount),
        latency_ms=d.latency_ms, adjudicator_latency_ms=d.adjudicator_latency_ms,
        consulted=d.adjudicator_consulted,
        attack_class=case.attack.attack_class if case.attack else None,
        clean_of=case.clean_of, source=case.source, heldout=case.heldout,
        cited_clause=d.cited_clause, reason=d.reason,
        injection_flagged=d.listing_attempted_instruction,
        sanitization_signals=d.sanitization_signals,
        error=(out.adjudication.error if out.adjudication else ""),
    )


def run(cases: list[EvalCase], gate_factory, *, workers: int = 8,
        measure_clean_twins: bool = True, progress=None) -> list[CaseResult]:
    """Execute cases. One Gate per worker, because a Gate owns a decision chain.

    Sharing one Gate across threads would interleave the chain and make its
    hashes meaningless. Per-worker gates keep each chain internally consistent,
    and the evaluation does not need them merged.
    """
    work: list[EvalCase] = list(cases)
    if measure_clean_twins:
        work += [_clean_twin(c) for c in cases if c.attack is not None]

    gates: dict[int, Gate] = {}
    done = [0]

    def one(case: EvalCase) -> CaseResult:
        import threading
        g = gates.setdefault(threading.get_ident(), gate_factory())
        try:
            r = run_case(g, case)
        except Exception as exc:                    # noqa: BLE001
            # An evaluation that dies halfway is an evaluation you cannot report.
            r = CaseResult(case.case_id, case.bucket, case.expected, "ERROR",
                           money(0), 0.0, 0.0, False, source=case.source,
                           heldout=case.heldout,
                           error=f"{exc.__class__.__name__}: {exc}")
        done[0] += 1
        if progress and done[0] % 25 == 0:
            progress(done[0], len(work))
        return r

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(one, work))

    # Attach each attacked case's clean-twin verdict, then drop the twins.
    twins = {r.case_id[:-len("__clean")]: r.actual
             for r in results if r.case_id.endswith("__clean")}
    final = []
    for r in results:
        if r.case_id.endswith("__clean"):
            continue
        if r.attack_class is not None:
            r.clean_verdict = twins.get(r.case_id, "")
        final.append(r)
    return final


def save_run(path, results: list[CaseResult], meta: dict) -> None:
    """Write a run, never over one that already exists.

    Learned the expensive way: a completed, valid run was overwritten by a
    re-run of the same command that then failed on exhausted quota, and the good
    numbers were gone. A run is measurement -- it costs real quota and cannot
    always be reproduced the same day -- so an existing file is rotated aside
    rather than replaced.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        kept = p.with_name(f"{p.stem}.{stamp}{p.suffix}")
        p.rename(kept)
        print(f"  (existing run preserved as {kept.name})")
    payload = {
        "meta": {**meta,
                 "prompt_fingerprint": prompt_fingerprint(),
                 "written_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")},
        "results": [{**asdict(r), "amount": str(r.amount)} for r in results],
    }
    p.write_text(json.dumps(payload, indent=2))


def load_run(path) -> tuple[list[CaseResult], dict]:
    d = json.loads(Path(path).read_text())
    return ([CaseResult(**{**r, "amount": money(r["amount"])})
             for r in d["results"]], d.get("meta", {}))


def regrade(path) -> dict:
    """Re-grade a stored run against the current metrics code. Costs nothing.

    The single most useful tool in the old repo, carried over: every published
    number can be recomputed from a stored run, so a metric definition can be
    argued about without re-spending the measurement budget.
    """
    from .metrics import report
    results, meta = load_run(path)
    return {"meta": meta, "report": report(results)}
