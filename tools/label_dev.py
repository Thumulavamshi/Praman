"""Review the development slice and record YOUR verdict on each case.

The cases in ``praman/data/devset.py`` ship with labels a model proposed. Those
are a starting point for review, not ground truth: tuning a model against
labels the model wrote is a closed loop that will happily report progress while
learning nothing. This script is how the labels become yours.

    python tools/label_dev.py              # review every unreviewed case
    python tools/label_dev.py --all        # review everything again
    python tools/label_dev.py --show       # print the current state, change nothing

For each case you see the delegation in the person's own words, the item, the
merchant and its familiarity, the amount, and the proposal's reasoning. Then:

    a / b / s   ALLOW / BLOCK / STEP_UP
    enter       accept the proposed label as it stands
    n           note -- record your own reasoning for the verdict you just gave
    ?           re-read the delegation's full compiled scope
    q           stop and save

Answers land in ``data/dev_labels.json`` and override the file. Stop whenever;
your place is kept.

**STEP_UP is a real answer here.** If you read the delegation and the item and
genuinely cannot decide, that IS the label -- not a cop-out you should resolve
by picking a side. The whole point of measuring step-up recall is that a gate
which guesses on undecidable cases is worse than one that asks.
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from praman.data.devset import DEV_CASES, LABELS_PATH
from praman.data.mandates import variant

VERDICTS = {"a": "ALLOW", "b": "BLOCK", "s": "STEP_UP"}
W = 74


def load() -> dict:
    if LABELS_PATH.exists():
        return json.loads(LABELS_PATH.read_text(encoding="utf-8"))
    return {}


def save(labels: dict) -> None:
    LABELS_PATH.parent.mkdir(parents=True, exist_ok=True)
    LABELS_PATH.write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8")


def describe(case) -> None:
    m = variant(case.mandate_variant)
    p = case.proposal()
    print("\n" + "=" * W)
    print(f"{case.case_id}   [{case.mandate_variant}]")
    print("=" * W)
    print("\n  The person said:")
    print(f"    \"{m.source_text}\"")
    print("\n  The agent proposes:")
    for it in p.items:
        print(f"    {it.sku}  {it.name}")
        print(f"        registered category: {it.category}    INR {it.price}")
    print(f"    merchant: {p.merchant_name} [{p.merchant_familiarity}]")
    print(f"    total:    INR {p.amount}")
    print(f"\n  proposed label: {case.expected}")
    for line in _wrap(case.rationale, W - 6):
        print(f"      {line}")


def _wrap(text, width):
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def show_scope(case) -> None:
    s = variant(case.mandate_variant).scope
    print("\n  compiled scope:")
    print(f"    allowed        {', '.join(s.categories_allowed)}")
    print(f"    denied         {', '.join(s.categories_denied)}")
    print(f"    per-txn cap    INR {s.per_transaction_cap}")
    print(f"    ask me above   INR {s.requires_step_up_above}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="review every case again, including ones already done")
    ap.add_argument("--show", action="store_true",
                    help="print current labels and exit")
    args = ap.parse_args()

    labels = load()

    if args.show:
        print(f"\n{len(labels)} of {len(DEV_CASES)} dev cases carry a human label\n")
        for case in DEV_CASES:
            rec = labels.get(case.case_id)
            mark = "  " if rec else "* "
            shown = rec["expected"] if rec else f"{case.expected} (proposed)"
            print(f"{mark}{case.case_id:28} {shown}")
        if len(labels) < len(DEV_CASES):
            print("\n* = not yet reviewed by a human")
        return 0

    todo = [c for c in DEV_CASES
            if args.all or c.case_id not in labels]
    if not todo:
        print(f"\nAll {len(DEV_CASES)} dev cases already reviewed. "
              f"Use --all to go through them again.\n")
        return 0

    print(f"\n{len(todo)} case(s) to review. "
          f"a=ALLOW  b=BLOCK  s=STEP_UP  enter=accept  n=note  ?=scope  q=quit")

    for i, case in enumerate(todo, 1):
        describe(case)
        while True:
            try:
                ans = input(f"\n  [{i}/{len(todo)}] your verdict > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n\nstopped. saving what you did.")
                save(labels)
                return 0
            if ans == "?":
                show_scope(case)
                continue
            if ans == "q":
                save(labels)
                print(f"\nsaved {len(labels)} labels to "
                      f"{LABELS_PATH.relative_to(ROOT)}\n")
                return 0
            if ans == "":
                labels[case.case_id] = {"expected": case.expected,
                                        "rationale": case.rationale,
                                        "accepted_proposal": True}
                break
            if ans in VERDICTS:
                labels[case.case_id] = {"expected": VERDICTS[ans],
                                        "rationale": case.rationale,
                                        "accepted_proposal":
                                            VERDICTS[ans] == case.expected}
                break
            if ans == "n":
                note = input("    your reasoning > ").strip()
                if case.case_id in labels and note:
                    labels[case.case_id]["rationale"] = note
                    break
                print("    give a verdict first, then n to annotate it.")
                continue
            print("    a / b / s / enter / n / ? / q")
        save(labels)

    changed = sum(1 for v in labels.values() if not v.get("accepted_proposal"))
    print(f"\n{'=' * W}")
    print(f"saved {len(labels)} labels to {LABELS_PATH.relative_to(ROOT)}")
    print(f"you disagreed with the proposal on {changed} of them")
    print(f"\nnow measure against them:")
    print(f"    python tools/run_eval.py --dev --provider gemini")
    print(f"{'=' * W}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
