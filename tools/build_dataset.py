"""Materialize the evaluation set to disk."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from praman.data.buckets import generate
from praman.data.heldout import HELDOUT_CASES

OUT = Path(__file__).resolve().parents[1] / "datasets"


def write(path, cases):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for c in cases:
            fh.write(json.dumps(c.to_dict(), sort_keys=True) + "\n")
    print(f"  {path.relative_to(OUT.parent)}  {len(cases)} cases")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    print("building evaluation set")
    write(OUT / "generated.jsonl", generate(n))
    write(OUT / "heldout.jsonl", HELDOUT_CASES)

    from collections import Counter
    allc = generate(n) + HELDOUT_CASES
    print(f"\n  total {len(allc)}")
    print(f"  buckets  {dict(Counter(c.bucket for c in allc))}")
    print(f"  labels   {dict(Counter(c.expected for c in allc))}")
    print(f"  authored {sum(1 for c in allc if c.source == 'authored')} "
          f"(held out, reported separately)")


if __name__ == "__main__":
    main()
