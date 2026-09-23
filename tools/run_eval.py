"""Run the evaluation set through the gate and print the report.

Usage:
  run_eval.py                       full set, live model
  run_eval.py --heldout             the 100 hand-labelled cases only
  run_eval.py --limit 60            a sample, for a cheap smoke run
  run_eval.py --bounds-only         no model calls; the deterministic baseline
  run_eval.py --regrade out/run.json    re-grade a stored run, costs nothing
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from praman.data.cases import EvalCase
from praman.eval.harness import (AdjudicatorDown, regrade, run,
                                 save_run)
from praman.eval.metrics import degraded, exception_list, report
from praman.gate.adjudicator import (LLMAdjudicator, StaticAdjudicator,
                                     prompt_fingerprint)
from praman.gate.gate import Gate
from praman.data.mandates import ISSUER


def load(path):
    return [EvalCase.from_dict(json.loads(l))
            for l in Path(path).read_text().splitlines() if l.strip()]


def pct(x):
    return "  n/a " if x is None else f"{x * 100:6.1f}%"


def ms(x):
    return "   n/a" if x is None else f"{x:6.0f}"


def print_slice(s, indent="  "):
    print(f"{indent}n={s['n']:<5} accuracy {pct(s['accuracy'])}     "
          f"step-up rate {pct(s['step_up_rate'])}")
    for v in ("ALLOW", "BLOCK", "STEP_UP"):
        m = s["per_verdict"][v]
        if m["tp"] + m["fn"] == 0 and m["fp"] == 0:
            continue
        print(f"{indent}  {v:8} precision {pct(m['precision'])}  "
              f"recall {pct(m['recall'])}   "
              f"(tp {m['tp']:>3} fp {m['fp']:>3} fn {m['fn']:>3})")
    fb, fa = s["false_block"], s["false_allow"]
    print(f"{indent}  false blocks  {fb['n']:>3}   INR {fb['cost_inr']:>12}   "
          f"(lost sales)")
    print(f"{indent}  false allows  {fa['n']:>3}   INR {fa['cost_inr']:>12}   "
          f"(txn + INR {fa['handling_fee_assumed']} dispute handling)")
    od = s["over_deferral"]
    print(f"{indent}  over-deferred {od['n']:>3}   INR {od['amount_inr']:>12}   "
          f"(stepped up on a clear case)")
    print(f"{indent}  under-deferred {s['under_deferral']:>2}         "
          f"(guessed where the honest answer was 'ask')")
    lat = s["latency"]
    print(f"{indent}  latency p50 {ms(lat['p50_ms'])} ms   p95 "
          f"{ms(lat['p95_ms'])} ms   model consulted on "
          f"{pct(lat['consult_rate'])}")
    print(f"{indent}    deterministic path  n={lat['deterministic_only']['n']:<4} "
          f"p50 {ms(lat['deterministic_only']['p50_ms'])} ms  "
          f"p95 {ms(lat['deterministic_only']['p95_ms'])} ms")
    print(f"{indent}    model path          n={lat['model_consulted']['n']:<4} "
          f"p50 {ms(lat['model_consulted']['p50_ms'])} ms  "
          f"p95 {ms(lat['model_consulted']['p95_ms'])} ms")


def print_degraded_banner(d: dict, W: int = 78) -> None:
    print("\n" + "=" * W)
    print("!!  DEGRADED RUN — NOT A MEASUREMENT")
    print("=" * W)
    print(f"  {d['n_errored']} of {d['n_total']} cases never reached the "
          f"adjudicator — {d['share_of_all'] * 100:.1f}% of all cases, "
          f"{d['share_of_consulted'] * 100:.1f}% of the")
    print(f"  {d['n_consulted']} that were sent to the model.")
    print()
    print("  Every one of those failed CLOSED to STEP_UP. That is the safety")
    print("  property working, and it is also what disguises the outage: the")
    print("  step-up rate is inflated, the easy buckets collapse to 0%, and")
    print("  there are no false allows because the model answered nothing.")
    print("  The accuracy below is arithmetic over verdicts that were never given.")
    print()
    print("  what failed:")
    for msg, n in d["distinct"][:4]:
        print(f"    {n:>4}x  {msg}")
    print()
    print("  Do not quote any figure from this run. Fix the cause, run it again,")
    print("  and if you keep the file, keep it the way out/README.md keeps the")
    print("  other degraded runs: as evidence of the fail-closed path, not as a")
    print("  metric.")
    print("=" * W)


def print_report(rep, results=None, dev=False):
    W = 78
    bad = degraded(results) if results else None
    if bad:
        print_degraded_banner(bad, W)
    print("\n" + "=" * W)
    print("PRAMAN — AUTHORIZATION GATE EVALUATION")
    print("=" * W)

    if dev:
        # The two slices must never be confusable in the output. A dev number
        # that reads "never used to tune a prompt" is the exact claim the dev
        # slice exists to stop us making by accident.
        print("\nDEVELOPMENT SLICE — TUNE AGAINST THIS")
        print("-" * W)
        print("  Iterate here as much as you like. This is NOT the held-out "
              "number and\n  must never be published as one. The held-out "
              "slice stays unseen until\n  the change is finished.\n")
    else:
        print("\nHELD-OUT, HAND-LABELLED SLICE")
        print("-" * W)
        print("  Labels authored individually, one case at a time, never used "
              "to tune a prompt.")
        print("  This is the number to argue with.\n")
    print_slice(rep["held_out_hand_labelled"])
    for b, s in rep.get("held_out_by_bucket", {}).items():
        print(f"\n  bucket {b}:")
        print_slice(s, indent="    ")

    print("\n\nGENERATED SET")
    print("-" * W)
    print_slice(rep["generated"])

    print("\n\nPER BUCKET, WHOLE SET")
    print("-" * W)
    for b, s in rep["by_bucket"].items():
        print(f"\n  bucket {b}:")
        print_slice(s, indent="    ")

    inj = rep["injection"]
    print("\n\nINJECTION RESISTANCE")
    print("-" * W)
    print("  An attack succeeds when it changes the verdict the SAME cart got "
          "without it.")
    if inj["n"]:
        print(f"\n  overall  {pct(inj['resistance'])}  over {inj['n']} attacks")
        print(f"  the model flagged an instruction attempt on "
              f"{pct(inj['flagged_rate'])} of them\n")
        from praman.data.injections import CLASS_NAMES
        for k, v in inj["by_class"].items():
            print(f"    class {k}  {pct(v['resistance'])}  "
                  f"({v['held']}/{v['n']})  {CLASS_NAMES.get(k, '')}")
        if inj["failures"]:
            print(f"\n  {len(inj['failures'])} attacks changed a verdict:")
            for f in inj["failures"][:12]:
                print(f"    {f['case_id']:22} class {f['attack_class']}  "
                      f"{f['clean_verdict']} -> {f['attacked_verdict']}"
                      f"   flagged={f['flagged_by_model']}")
                print(f"      {f['reason'][:100]}")
        else:
            print("\n  no attack changed a verdict in this run")

    print("\n\nOVERALL (reported last, deliberately)")
    print("-" * W)
    print("  An aggregate over a set that is 40% easy cases is not a "
          "capability number.")
    print_slice(rep["overall"])

    if results:
        exc = exception_list(results)
        print("\n\nEXCEPTION LIST — every wrong answer, most expensive first")
        print("-" * W)
        if not exc:
            print("  none")
        for e in exc[:25]:
            print(f"  {e['case_id']:24} {e['bucket']}  {e['expected']:8}-> "
                  f"{e['actual']:8} INR {e['cost_inr']:>10}  {e['source']}")
            if e["reason"]:
                print(f"      {e['reason'][:104]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heldout", action="store_true")
    ap.add_argument("--generated", action="store_true")
    ap.add_argument("--dev", action="store_true",
                    help="the development slice ONLY -- the one set you may "
                         "tune against. Never mixed into a normal run.")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--bounds-only", action="store_true",
                    help="no model calls; the deterministic baseline")
    ap.add_argument("--workers", type=int, default=8,
                    help="on gemini the key ring paces calls, so more workers "
                         "than the pool RPM just queue on the limiter")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--provider", default=os.getenv("PRAMAN_PROVIDER", "gemini"),
                    choices=["anthropic", "gemini"],
                    help="which model answers the semantic question")
    ap.add_argument("--model", default="",
                    help="override the provider's default model")
    ap.add_argument("--thinking-budget", type=int, default=512,
                    help="gemini only: reasoning tokens before the verdict")
    ap.add_argument("--out", default="",
                    help="default: out/run.json, or out/dev_run.json under --dev")
    ap.add_argument("--regrade")
    ap.add_argument("--no-fail-fast", action="store_true",
                    help="run to the end even if every adjudication is failing "
                         "— for deliberately capturing a degraded run")
    args = ap.parse_args()
    if not args.out:
        args.out = "out/dev_run.json" if args.dev else "out/run.json"

    if args.regrade:
        r = regrade(args.regrade)
        print(f"re-graded {args.regrade}  (meta: {r['meta']})")
        stored = r["meta"].get("prompt_fingerprint")
        current = prompt_fingerprint()
        if stored and stored != current:
            print(f"\n  !! this run was produced by a DIFFERENT system prompt "
                  f"({stored}, now {current}).\n     Re-grading recomputes the "
                  f"metrics from stored verdicts, so the numbers below are "
                  f"still\n     what that run measured -- but they are not what "
                  f"today's code would produce.\n")
        elif not stored:
            print(f"\n  note: this run predates prompt fingerprinting, so which "
                  f"prompt produced it\n        cannot be checked from the file. "
                  f"Current prompt is {current}.\n")
        results, _ = __import__("praman.eval.harness", fromlist=["load_run"]) \
            .load_run(args.regrade)
        print_report(r["report"], results, dev=bool(args.dev))
        return 2 if degraded(results) else 0

    if args.dev:
        # Built in memory rather than read from datasets/dev.jsonl, so a label
        # recorded a minute ago is the label measured against. A materialised
        # file would need rebuilding after every labelling session, and the one
        # time someone forgot, the run would silently grade against the model's
        # own proposals and report the agreement as accuracy.
        from praman.data.devset import DEV_CASES, HUMAN_LABELLED
        cases = list(DEV_CASES)
        print(f"development slice: {len(cases)} cases, "
              f"{HUMAN_LABELLED} carrying a human label")
        if HUMAN_LABELLED < len(cases):
            print(f"\n  !! {len(cases) - HUMAN_LABELLED} case(s) still carry the "
                  f"label a model proposed.\n     Accuracy against those is a "
                  f"model agreeing with itself. Run:\n"
                  f"         python tools/label_dev.py\n")
    else:
        cases = []
        if not args.generated:
            cases += load(ROOT / "datasets" / "heldout.jsonl")
        if not args.heldout:
            cases += load(ROOT / "datasets" / "generated.jsonl")
    if args.limit:
        cases = cases[:args.limit]

    # The gate must verify against the key that actually issued these mandates.
    # A fresh issuer would fail every signature and turn the whole run into a
    # study of the signature check.
    issuer = ISSUER
    if args.bounds_only:
        def factory():
            return Gate(adjudicator=StaticAdjudicator(), issuer=issuer)
        mode = "deterministic bounds only (no model)"
    elif args.provider == "gemini":
        from praman.gate.gemini import DEFAULT_GEMINI_MODEL, GeminiAdjudicator
        from praman.keyring import GeminiKeyRing
        model = args.model or DEFAULT_GEMINI_MODEL
        # One ring shared by every worker. Per-worker rings would each pace to
        # the full pool rate and together overshoot it by the worker count,
        # which is the fastest way to burn a free-tier day.
        ring = GeminiKeyRing(rpm_per_key=float(os.getenv("PRAMAN_GEMINI_RPM", "10")),
                             model=model)
        print(ring.report())
        seen = set()

        def note(state, why):
            line = f"  [{state.masked}] {why}"
            if line not in seen:
                seen.add(line)
                print(line, flush=True)

        def factory():
            return Gate(adjudicator=GeminiAdjudicator(
                ring=ring, model=model, thinking_budget=args.thinking_budget,
                on_retry=note), issuer=issuer, always_consult=True)
        mode = (f"{model}, thinking_budget={args.thinking_budget}, "
                f"{len(ring)} keys, always_consult")
        args._ring = ring
    else:
        model = args.model or "claude-opus-5"
        def factory():
            return Gate(adjudicator=LLMAdjudicator(model=model, effort=args.effort),
                        issuer=issuer, always_consult=True)
        mode = f"{model}, effort={args.effort}, always_consult"

    print(f"running {len(cases)} cases — {mode}")

    def progress(done, total):
        print(f"  {done}/{total}", flush=True)

    try:
        results = run(cases, factory, workers=args.workers, progress=progress,
                      fail_fast=0 if args.no_fail_fast else 5)
    except AdjudicatorDown as down:
        W = 78
        print("\n" + "=" * W)
        print("!!  ABORTED — THE ADJUDICATOR IS NOT ANSWERING")
        print("=" * W)
        print(f"  {down}")
        print("  Nothing was written. Every case would have come back STEP_UP,")
        print("  fail-closed, and the run would have looked plausible and")
        print("  measured nothing.")
        print()
        print("  Read the error above before spending more quota. A 429 or")
        print("  RESOURCE_EXHAUSTED is rate limiting — add keys or lower")
        print("  PRAMAN_GEMINI_RPM. Anything else is a rejected request: the")
        print("  key, the model name, or the request shape, and retrying will")
        print("  fail identically every time.")
        print()
        print("  To capture a degraded run deliberately, as evidence of the")
        print("  fail-closed path:  --no-fail-fast")
        print("=" * W)
        return 2
    # The model that answered is recorded with the run, so a report can never
    # quietly inherit a number produced by a different one.
    save_run(ROOT / args.out, results,
             {"mode": mode, "provider": args.provider, "n_cases": len(cases),
              "effort": args.effort})
    if getattr(args, "_ring", None) is not None:
        print("\n" + args._ring.report())
    print_report(report(results), results, dev=args.dev)
    print(f"\nfull run written to {args.out} — re-grade with "
          f"--regrade {args.out}")

    # The report is long enough that the banner at the top has scrolled away by
    # now, and the last thing on screen is what gets copied into a README.
    bad = degraded(results)
    if bad:
        print_degraded_banner(bad)
        return 2


if __name__ == "__main__":
    # Exit 2 on a degraded run. A script or a CI step that re-runs the eval must
    # be able to tell "the gate scored badly" from "the adjudicator was never
    # reached", and a zero exit on the second one is how a meaningless number
    # gets picked up and published by something that was not watching.
    raise SystemExit(main())
