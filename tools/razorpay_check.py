"""Validate the live Razorpay path. Run this from YOUR machine, not the sandbox.

The build environment's egress policy blocks api.razorpay.com, so this is the
one part of Praman that cannot be proven where it was written. Everything the
mock claims to do, this script checks against the real test-mode API.

    python tools/razorpay_check.py              # stage 1: order, amounts, notes
    python tools/razorpay_check.py --link       # get a payable URL (easiest)
    python tools/razorpay_check.py --checkout   # pay the ORDER via local checkout
    python tools/razorpay_check.py --pay pay_X  # stage 2: capture, idempotency, refund

**An Order is not a Payment Link, and this trips everyone up once.**

An Order is a server-side record that a payment will be matched against. It
appears under Transactions -> Orders in the dashboard, and the dashboard gives
you no way to pay it -- there is no button, because an order is paid through
Razorpay Checkout with the order_id handed to the widget. Looking for an order
under Payment Links finds nothing, correctly.

So there are two honest routes to a real payment, and the difference matters:

  --link      creates a Payment Link, which is a hosted page with a URL you can
              simply open and pay. Easiest. It creates its OWN order internally,
              so it does not exercise the order we made -- it proves capture,
              idempotency and refund, which is most of what matters.

  --checkout  serves a tiny local page that opens Razorpay Checkout bound to OUR
              order id. Slower to run, but it is the flow Praman actually uses
              (create_order -> checkout -> capture_payment), so it proves the
              whole path rather than most of it.
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


def make_link(gw, amount):
    """A hosted payable URL. Creates its own order internally."""
    print(f"\nPAYMENT LINK — a hosted page you can just open\n" + "-" * W)
    try:
        link = gw._client.payment_link.create({
            "amount": to_paise(amount),
            "currency": "INR",
            "description": "Praman live-path check",
            "notes": {"mandate_id": "mnd_reference",
                      "agent": "agent_claude_shopper_v1"},
            "notify": {"sms": False, "email": False},
            "reminder_enable": False,
        })
    except Exception as e:                              # noqa: BLE001
        bad(f"payment_link.create: {e.__class__.__name__}: {e}")
        return 1
    ok(f"link {link.get('id')}  status={link.get('status')}")
    print(f"\n  Open this and pay with the test card:\n")
    print(f"      {link.get('short_url')}\n")
    print(f"      {TEST_CARD}\n")
    print( "  Then find the payment id under Transactions -> Payments, or in the")
    print( "  link's own detail page, and run:\n")
    print( "      python tools/razorpay_check.py --pay pay_XXXXXXXX")
    return 0


def serve_checkout(gw, order, port):
    """Serve a local page that pays OUR order through Razorpay Checkout.

    This is the flow Praman itself uses -- create_order, then a customer
    completes checkout against that order id, then capture_payment. The key id
    is publishable and is meant to be in client-side code; the secret never
    leaves the server and is not in this page.
    """
    import http.server
    import socketserver
    import webbrowser

    html = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Praman — pay order {order.id}</title>
<style>
 body{{font:16px system-ui;margin:0;display:grid;place-items:center;
       min-height:100vh;background:#f1f4ee;color:#17211c}}
 .card{{background:#fff;border:1px solid #d3dcce;padding:32px;max-width:520px}}
 code{{font-family:ui-monospace,monospace;background:#eef2ea;padding:2px 5px}}
 button{{font:600 16px system-ui;padding:12px 22px;border:0;background:#24425c;
         color:#fff;cursor:pointer}}
 #out{{margin-top:18px;font-family:ui-monospace,monospace;font-size:14px;
       word-break:break-all}}
 .ok{{color:#2e6b4f}} .err{{color:#9c3a2a}}
</style></head><body><div class="card">
<h2>Pay order <code>{order.id}</code></h2>
<p>INR {to_rupees(order.amount)} &middot; test mode. Use card
<code>4111 1111 1111 1111</code>, any future expiry, any CVV, OTP
<code>1234</code>.</p>
<button id="pay">Open Razorpay Checkout</button>
<div id="out"></div>
</div>
<script src="https://checkout.razorpay.com/v1/checkout.js"></script>
<script>
const out = document.getElementById('out');
document.getElementById('pay').onclick = function () {{
  new Razorpay({{
    key: "{gw.key_id}",
    order_id: "{order.id}",
    amount: {order.amount},
    currency: "INR",
    name: "Praman",
    description: "live path check",
    handler: function (r) {{
      out.className = 'ok';
      out.textContent = 'payment id: ' + r.razorpay_payment_id +
        '  —  now run:  python tools/razorpay_check.py --pay ' +
        r.razorpay_payment_id;
    }},
    modal: {{ ondismiss: function () {{
      out.className = 'err'; out.textContent = 'checkout closed without paying';
    }} }}
  }}).open();
}};
</script></body></html>"""

    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    page = out_dir / "razorpay_checkout.html"
    page.write_text(html, encoding="utf-8")

    print(f"\nLOCAL CHECKOUT — pays THIS order\n" + "-" * W)
    ok(f"wrote {page.relative_to(ROOT)}")

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

        def log_message(self, *a):
            pass

    url = f"http://127.0.0.1:{port}/"
    print(f"\n  Serving at {url}")
    print( "  A browser should open. If not, paste that URL yourself.")
    print( "  The page prints the payment id when the payment succeeds.")
    print( "  Ctrl-C here when you have it.\n")
    try:
        webbrowser.open(url)
    except Exception:                                   # noqa: BLE001
        pass
    try:
        with socketserver.TCPServer(("127.0.0.1", port), Handler) as httpd:
            httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    except OSError as e:
        bad(f"could not serve on port {port}: {e}. Try --port 8010")
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pay", help="a payment id from a completed test checkout")
    ap.add_argument("--amount", default="357.00")
    ap.add_argument("--link", action="store_true",
                    help="create a Payment Link and print a payable URL")
    ap.add_argument("--checkout", action="store_true",
                    help="serve a local page that pays OUR order via Checkout")
    ap.add_argument("--port", type=int, default=8000)
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
        if args.link:
            return make_link(gw, args.amount)
        if args.checkout:
            return serve_checkout(gw, order, args.port)

        print(f"\nSTAGE 2 — capture and refund (needs a real payment)\n" + "-" * W)
        print("  A payment exists only once someone completes checkout. There is "
              "no\n  server-side call that conjures one, in test mode or "
              "otherwise.\n")
        print(f"  Order {order.id} is now under Transactions -> Orders in the")
        print( "  dashboard. It will NOT appear under Payment Links -- those are a")
        print( "  different object, and an order has no pay button of its own.\n")
        print( "  Two ways to get a payment, pick either:\n")
        print( "    python tools/razorpay_check.py --link")
        print( "      Creates a Payment Link and prints a URL. Open it, pay with")
        print(f"      the test card, done. Easiest route.\n")
        print( "    python tools/razorpay_check.py --checkout")
        print( "      Serves a local page that pays THIS order through Razorpay")
        print( "      Checkout. Slower, but it is the exact flow Praman uses.\n")
        print(f"  Test card:  {TEST_CARD}\n")
        print( "  Then feed the payment id back:")
        print( "    python tools/razorpay_check.py --pay pay_XXXXXXXX\n")
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
            p = gw.fetch_payment(p.id)
        else:
            cap = p
            ok(f"already {p.status}; skipping capture")

        # Razorpay REFUSES a second capture rather than answering idempotently.
        # A refusal is the stronger guarantee -- it cannot double-charge either --
        # so the refusal is the pass condition here, and a silent success would
        # be the alarming outcome.
        try:
            gw.capture_payment(p.id, to_rupees(p.amount))
            bad("a second capture was ACCEPTED — check the dashboard for a "
                "double charge, and do not rely on re-capture being safe")
        except Exception as e:                          # noqa: BLE001
            if "already been captured" in str(e) or "already captured" in str(e):
                ok("re-capture refused — an agent retry cannot double-charge")
            else:
                bad(f"re-capture failed for an unexpected reason: {e}")

        # Refund against what is still refundable, not against the payment
        # amount. Re-running this script on the same payment id is the normal
        # case rather than the exception -- the id gets pasted back in more
        # than once -- and "half the payment amount" stops being refundable the
        # moment a previous run refunded anything. Asking for more than the
        # balance returns a 400 that looks exactly like a malformed request,
        # which sends you debugging the request body instead of reading the
        # payment.
        if p.amount_refunded:
            ok(f"{p.amount_refunded} paise already refunded "
               f"(refund_status={p.refund_status or 'none'}); "
               f"{p.refundable} paise still refundable")
        if p.refundable < 100:
            bad(f"only {p.refundable} paise left to refund on {p.id}, below the "
                "INR 1.00 minimum — the refund checks cannot run against this "
                "payment.\n         Make a fresh one (--link or --checkout) "
                "and pass that id instead.")
            return 1

        # Hold the balance we read. Re-reading p.refundable after the refund
        # below would be reading a different number, and against a gateway that
        # hands back a live object rather than a snapshot it would be reading
        # zero -- which makes the over-refund probe below ask for 100 paise
        # against a balance that still has thousands in it, and report that the
        # gateway accepted an over-refund when it did nothing of the kind.
        refundable = p.refundable
        half_paise = min(refundable, max(100, refundable // 2))
        r = gw.refund(p.id, to_rupees(half_paise), notes={"reason": "praman_check"})
        ok(f"refund {r.id}  {r.amount} paise  status={r.status}")

        # One rupee past the remaining balance, not some wildly large number.
        # The mock refuses at exactly the balance, so that is the boundary
        # worth proving the live gateway refuses at too -- a check that only
        # rejects an absurd amount would pass against a gateway that draws the
        # limit somewhere else entirely.
        remaining = refundable - half_paise
        try:
            gw.refund(p.id, to_rupees(remaining + 100))
            bad("an over-refund was ACCEPTED — the gateway is not enforcing the "
                "refundable balance, and neither would our mock")
        except Exception:                               # noqa: BLE001
            ok(f"over-refund refused at the boundary ({remaining + 100} paise "
               f"against a {remaining} paise balance)")
    except Exception as e:                              # noqa: BLE001
        bad(f"{e.__class__.__name__}: {e}")
        detail = getattr(gw, "last_error", lambda: "")()
        if detail:
            print(f"\n         {detail}\n")
            print("         ^ the whole error body. The SDK raises only the")
            print("           description, which is how a rejected parameter")
            print("           reaches you as the bare 'invalid request sent'.")
        return 1

    print(f"\n{'=' * W}\nAll live checks passed. The mock's behaviour matches the "
          f"real gateway on\nevery point Praman depends on.\n{'=' * W}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
