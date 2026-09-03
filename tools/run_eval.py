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
from praman.eval.harness import regrade, run, save_run
from praman.eval.metrics import exception_list, report
from praman.gate.adjudicator import LLMAdjudicator, StaticAdjudicator
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


def print_report(rep, results=None):
    W = 78
    print("\n" + "=" * W)
    print("PRAMAN — AUTHORIZATION GATE EVALUATION")
    print("=" * W)

    print("\nHELD-OUT, HAND-LABELLED SLICE")
    print("-" * W)
    print("  Labels authored individually, one case at a time, never used to "
          "tune a prompt.")
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
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--bounds-only", action="store_true",
                    help="no model calls; the deterministic baseline")
    ap.add_argument("--workers", type=int, default=8,
                    help="on gemini the key ring paces calls, so more workers "
                         "than the pool RPM just queue on the limiter")
    ap.add_argument("--effort", default="high")
    ap.add_argument("--provider", default=os.getenv("PRAMAN_PROVIDER", "anthropic"),
                    choices=["anthropic", "gemini"],
                    help="which model answers the semantic question")
    ap.add_argument("--model", default="",
                    help="override the provider's default model")
    ap.add_argument("--thinking-budget", type=int, default=512,
                    help="gemini only: reasoning tokens before the verdict")
    ap.add_argument("--out", default="out/run.json")
    ap.add_argument("--regrade")
    args = ap.parse_args()

    if args.regrade:
        r = regrade(args.regrade)
        print(f"re-graded {args.regrade}  (meta: {r['meta']})")
        results, _ = __import__("praman.eval.harness", fromlist=["load_run"]) \
            .load_run(args.regrade)
        print_report(r["report"], results)
        return

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

    results = run(cases, factory, workers=args.workers, progress=progress)
    # The model that answered is recorded with the run, so a report can never
    # quietly inherit a number produced by a different one.
    save_run(ROOT / args.out, results,
             {"mode": mode, "provider": args.provider, "n_cases": len(cases),
              "effort": args.effort})
    if getattr(args, "_ring", None) is not None:
        print("\n" + args._ring.report())
    print_report(report(results), results)
    print(f"\nfull run written to {args.out} — re-grade with "
          f"--regrade {args.out}")


if __name__ == "__main__":
    main()
