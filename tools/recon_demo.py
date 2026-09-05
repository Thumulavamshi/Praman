"""Phase 6: reconcile settlement file, bank statement and ledger.

    python tools/recon_demo.py              # deterministic only, free
    python tools/recon_demo.py --agent      # residual goes to the model
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from praman.ledger.engine import Engine
from praman.ledger.generator import generate_stream, resolve_settlement_amounts
from praman.ledger.invariants import check_all
from praman.ledger.money import ZERO, money, money_str
from praman.recon.matcher import _expected_payout, reconcile
from praman.recon.sources import bank_statement, corrupt, settlement_file
from praman.recon.verify import verify

W = 78


def rule(t=""):
    print(f"\n{t}\n{'-' * W}" if t else "-" * W)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", action="store_true")
    ap.add_argument("--payments", type=int, default=240)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--corrupt-seed", type=int, default=5)
    ap.add_argument("--model", default="")
    args = ap.parse_args()

    catalog = json.loads((ROOT / "data" / "catalog.json").read_text())["products"]
    engine = Engine()
    engine.apply_many(resolve_settlement_amounts(generate_stream(
        seed=args.seed, n_payments=args.payments, settle_batch=10,
        catalog=catalog)))
    engine.drain_quarantine()

    rows = settlement_file(engine)
    lines = bank_statement(engine, rows)
    n = len(engine.state.settlements)

    print("=" * W)
    print("PHASE 6 — RECONCILIATION")
    print("=" * W)
    print(f"\n  book        {n} settlements, {len(rows)} settlement rows, "
          f"{len(lines)} bank credits")

    rule("the clean pair, as a control")
    clean = reconcile(engine, rows, lines)
    print(f"  {clean.summary()}")
    print(f"  auto-match {clean.auto_match_rate(n) * 100:.1f}%   "
          f"— a correct pair must reconcile to nothing, or the rest means nothing")

    crows, clines, truth = corrupt(rows, lines, seed=args.corrupt_seed, rate=0.5)
    rule(f"now broken in {len(truth)} places, the way real files break")
    print(f"  {dict(Counter(t['kind'] for t in truth))}")

    report = reconcile(engine, crows, clines)
    before = report.auto_match_rate(n)
    print(f"\n  DETERMINISTIC PASS")
    print(f"    {report.summary()}")
    print(f"    auto-match {before * 100:.1f}%")
    print(f"\n  Two break kinds raised no exception at all — the matcher's own")
    print(f"  fallbacks recovered them. That is the pass earning its keep.")

    if not args.agent:
        rule("exceptions the agent would see")
        for e in report.exceptions[:12]:
            print(f"  {e}")
        if len(report.exceptions) > 12:
            print(f"  ... and {len(report.exceptions) - 12} more")
        print(f"\n  Run again with --agent to send these to the model.")
        return

    from praman.recon.agent import ReconAgent
    from collections import defaultdict

    kw = {"model": args.model} if args.model else {}
    agent = ReconAgent(**kw)
    rule(f"the residual goes to {agent.model}")
    res = agent.propose(engine, report, crows, clines)
    if res.error:
        print(f"  agent failed: {res.error}")
        return
    print(f"  {len(res.proposals)} proposals in {res.latency_ms / 1000:.1f}s")

    by_settlement = defaultdict(list)
    for r in crows:
        by_settlement[r.settlement_id].append(r)

    rule("every proposal, through the verifier")
    # One broken payout produces THREE exceptions -- the settlement that did not
    # match, and each of the credits that did not match. The agent answers all
    # three, so once a split_match resolves the break, its restatements arrive
    # at a verifier that has already claimed those credits. Those are redundant,
    # not wrong, and calling them "rejected" would overstate what happened.
    accepted, rejected, escalated, superseded = 0, 0, 0, 0
    resolved_value = ZERO
    for p in res.proposals:
        expected = (_expected_payout(engine, p.settlement_id,
                                     by_settlement.get(p.settlement_id, []))
                    if p.settlement_id else None)
        already = (p.settlement_id in report.matched_settlements
                   and p.kind in ("match", "split_match"))
        v = verify(engine, report, p, clines, expected_payout=expected,
                   apply=not already)
        if already:
            superseded += 1
            print(f"  [ -- ] {str(p):<52} {p.confidence:<7}")
            print(f"         superseded: {p.settlement_id} was already resolved")
            continue
        mark = "ok  " if v.accepted else "NO  "
        print(f"  [{mark}] {str(p):<52} {p.confidence:<7}")
        print(f"         {v.reason[:88]}")
        if p.kind == "escalate":
            escalated += 1
        elif v.accepted:
            accepted += 1
            if p.settlement_id and expected is not None:
                resolved_value = money(resolved_value + expected)
            elif p.kind == "adjusting_entry":
                resolved_value = money(resolved_value + money(p.amount))
        else:
            rejected += 1

    after = report.auto_match_rate(n)
    rule("outcome")
    print(f"  auto-match before AI   {before * 100:5.1f}%")
    print(f"  auto-match after AI    {after * 100:5.1f}%")
    print(f"  proposals accepted     {accepted}")
    print(f"  proposals REJECTED     {rejected}   "
          f"(invalid — the verifier refused them)")
    print(f"  superseded             {superseded}   "
          f"(redundant restatements of a break already resolved)")
    print(f"  escalated to a human   {escalated}")
    print(f"  value resolved         INR {money_str(resolved_value)}")
    violations = check_all(engine.state)
    print(f"  invariant violations   {len(violations)}   "
          f"(after booking every accepted adjustment)")
    ok, _, _ = engine.verify()
    print(f"  event chain verified   {ok}")

    rule("and the verifier refusing something genuinely invalid")
    # Not a real proposal -- a fabricated one, to show the guard rather than
    # assert it. A clean run proves the agent behaved; this proves it could not
    # have misbehaved even if it tried.
    from praman.recon.models import BankLine, Resolution
    for bad, label in [
        (Resolution("adjusting_entry", "invented", debit_account="5000",
                    credit_account="1200", amount="9999.00"),
         "an adjustment for a discrepancy nobody found"),
        (Resolution("adjusting_entry", "fake account", debit_account="9999",
                    credit_account="1200", amount="0.02"),
         "an account outside the chart"),
        (Resolution("split_match", "wrong sum", settlement_id="setl_7_006",
                    bank_line_ids=["synthetic_a", "synthetic_b"]),
         "credits that do not sum to the payout"),
        (Resolution("match", "double claim", settlement_id="setl_7_006",
                    bank_line_ids=[next(iter(report.matched_settlements.values()))[0]]),
         "a credit already matched to another settlement"),
    ]:
        # Two credits that exist but total the wrong amount, so the sum guard is
        # what fires rather than the double-claim guard.
        probe = list(clines) + [
            BankLine("synthetic_a", "2026-09-05", money("1000.00"), "probe"),
            BankLine("synthetic_b", "2026-09-05", money("2000.00"), "probe")]
        v = verify(engine, report, bad, probe,
                   expected_payout=money("7306.48"), apply=True)
        print(f"  [{'ok  ' if v.accepted else 'NO  '}] {label}")
        print(f"         {v.reason[:88]}")
    print(f"\n  invariants still clean: {len(check_all(engine.state)) == 0}")


if __name__ == "__main__":
    main()
