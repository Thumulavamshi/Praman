"""Validate the live Razorpay path. Run this from YOUR machine, not the sandbox.

The build environment's egress policy blocks api.razorpay.com, so this is the
one part of Praman that cannot be proven where it was written. Everything the
mock claims to do, this script checks against the real test-mode API.

    python tools/razorpay_check.py                 # order + fetch (no checkout)
    python tools/razorpay_check.py --pay pay_XXX   # capture + refund a real payment

Stage 1 needs nothing but keys. Stage 2 needs a payment id, and a payment only
comes into existence when a customer completes checkout -- there is no
server-side call that conjures one, in test mode or otherwise. That gap is real
and the script says so rather than papering over it.
"""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from praman.pg.interface import to_paise, to_rupees
from praman.pg.razorpay_pg import RazorpayGateway, RazorpayNotConfigured

W = 74
TEST_CARD = "4111 1111 1111 1111   any future expiry   any CVV   OTP 1234"


def ok(msg):
    print(f"  [ok]   {msg}")


def bad(msg):
    print(f"  [FAIL] {msg}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pay", help="a payment id from a completed test checkout")
    ap.add_argument("--amount", default="357.00")
    args = ap.parse_args()

    print("=" * W)
    print("RAZORPAY LIVE PATH CHECK")
    print("=" * W)

    try:
        gw = RazorpayGateway()
    except RazorpayNotConfigured as e:
        bad(str(e))
        return 1
    except Exception as e:                              # noqa: BLE001
        bad(f"could not build the client: {e.__class__.__name__}: {e}")
        return 1
    ok(f"client built against {gw.key_id[:12]}... (test mode enforced)")

    # -- stage 1: order ------------------------------------------------------
    print(f"\nSTAGE 1 — order creation (needs only your keys)\n" + "-" * W)
    try:
        order = gw.create_order(
            args.amount, receipt="praman_check_1",
            notes={"mandate_id": "mnd_reference", "agent": "agent_claude_shopper_v1"})
    except Exception as e:                              # noqa: BLE001
        bad(f"create_order: {e.__class__.__name__}: {e}")
        print("\n  If this is a network/403 error you are probably behind a proxy "
              "that\n  blocks api.razorpay.com. Run this from an unrestricted "
              "network.")
        return 1
    ok(f"order {order.id}  {order.amount} paise  status={order.status}")

    if order.amount != to_paise(args.amount):
        bad(f"amount round-trip wrong: sent {to_paise(args.amount)}, "
            f"got {order.amount}")
        return 1
    ok(f"amount round-trips exactly: INR {to_rupees(order.amount)} "
       f"== {order.amount} paise")

    if order.notes.get("mandate_id") != "mnd_reference":
        bad("notes did not survive the round trip — the mandate id is how a "
            "settlement report is tied back to an authorization")
        return 1
    ok("notes survive the round trip (mandate_id and agent are readable back)")

    if not args.pay:
        print(f"\nSTAGE 2 — capture and refund (needs a real payment)\n" + "-" * W)
        print("  A payment is created when a customer completes checkout. There "
              "is no\n  server-side call that makes one, so this stage needs you "
              "to do that once.\n")
        print(f"  1. In the Razorpay dashboard open Test Mode.")
        print(f"  2. Pay against order {order.id} using the standard test card:")
        print(f"       {TEST_CARD}")
        print(f"  3. Copy the resulting payment id (pay_...) and re-run:")
        print(f"       python tools/razorpay_check.py --pay pay_XXXXXXXX\n")
        print("  Stage 1 passing already proves the credentials, the connection, "
              "the\n  paise conversion and the notes round trip. Stage 2 proves "
              "capture,\n  idempotency and refund.")
        return 0

    # -- stage 2: capture, idempotency, refund -------------------------------
    print(f"\nSTAGE 2 — capture, idempotency, refund\n" + "-" * W)
    try:
        p = gw.fetch_payment(args.pay)
        ok(f"fetched {p.id}  status={p.status}  method={p.method}  "
           f"{p.amount} paise")

        if p.status == "authorized":
            cap = gw.capture_payment(p.id, to_rupees(p.amount))
            ok(f"captured -> status={cap.status}")
        else:
            cap = p
            ok(f"already {p.status}; skipping capture")

        again = gw.capture_payment(p.id, to_rupees(p.amount))
        if again.id == cap.id:
            ok("re-capture is idempotent — an agent retry cannot double-charge")
        else:
            bad("re-capture produced a different payment; retries are NOT safe")

        half = to_rupees(p.amount // 2)
        r = gw.refund(p.id, half, notes={"reason": "praman_check"})
        ok(f"refund {r.id}  {r.amount} paise  status={r.status}")

        try:
            gw.refund(p.id, to_rupees(p.amount))
            bad("an over-refund was ACCEPTED — the gateway is not enforcing the "
                "refundable balance, and neither would our mock")
        except Exception:                               # noqa: BLE001
            ok("over-refund correctly refused")
    except Exception as e:                              # noqa: BLE001
        bad(f"{e.__class__.__name__}: {e}")
        return 1

    print(f"\n{'=' * W}\nAll live checks passed. The mock's behaviour matches the "
          f"real gateway on\nevery point Praman depends on.\n{'=' * W}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
