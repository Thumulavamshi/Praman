"""Preflight the Gemini key pool. One tiny call per key, then stop.

Worth its own tool because the failure it catches is expensive: a bad key in a
pool of six does not announce itself, it just makes one call in six fail, and on
a 500-case run that looks like a flaky model rather than a typo. This costs six
requests out of a ~1,500/day budget and tells you exactly which keys are live,
which are rejected, and which have already spent their day.

Deliberately does NOT use the key ring's rotation: the point is to test every
key individually, including ones the ring would have skipped.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from praman.keyring import load_keys_from_env, looks_like_daily_exhaustion

MODEL = os.getenv("PRAMAN_GEMINI_MODEL", "gemini-2.5-flash")


def mask(k: str) -> str:
    return f"{k[:6]}...{k[-4:]}" if len(k) > 12 else "(short)"


def _sdk_version() -> str:
    import importlib.metadata as md
    try:
        return f"google-genai {md.version('google-genai')}"
    except Exception:                                   # noqa: BLE001
        return "an unknown google-genai version"


def main():
    keys = load_keys_from_env()
    if not keys:
        print("no keys found. Set GEMINI_API_KEYS in .env as a comma-separated "
              "list:\n  GEMINI_API_KEYS=\"AQ.Ab...one,AQ.Ab...two\"\n"
              "AI Studio issues Auth keys (AQ.Ab) now rather than Standard "
              "keys (AIza).")
        return 1

    seen, uniq = set(), []
    for k in keys:
        (uniq.append(k), seen.add(k)) if k not in seen else None
    if len(uniq) != len(keys):
        print(f"note: {len(keys) - len(uniq)} duplicate key(s) ignored — the same "
              f"key twice does not double your quota\n")

    from google import genai
    from google.genai import errors, types

    print(f"checking {len(uniq)} key(s) against {MODEL}\n")
    live = 0
    for i, key in enumerate(uniq, 1):
        label = f"key{i} {mask(key)}"
        try:
            client = genai.Client(api_key=key)
            t = time.perf_counter()
            r = client.models.generate_content(
                model=MODEL, contents="Reply with the single word: ok",
                config=types.GenerateContentConfig(
                    max_output_tokens=2000,
                    thinking_config=types.ThinkingConfig(thinking_budget=0)))
            ms = (time.perf_counter() - t) * 1000
            u = getattr(r, "usage_metadata", None)
            tok = getattr(u, "total_token_count", 0) or 0
            print(f"  {label:<28} LIVE     {ms:6.0f} ms   {tok:>4} tokens")
            live += 1
        except errors.APIError as e:
            msg = str(e)
            code = getattr(e, "code", None) or getattr(e, "status_code", "?")
            if code == 429 and looks_like_daily_exhaustion(msg):
                print(f"  {label:<28} SPENT    daily quota already used up today")
            elif code == 429:
                print(f"  {label:<28} BUSY     rate limited right now (RPM) — "
                      f"this key is fine, just pace it")
                live += 1
            elif code in (400, 401, 403):
                print(f"  {label:<28} BAD      {msg[:66]}")
                # An Auth key rejected as invalid is almost never the key. AI
                # Studio now issues AQ.Ab keys instead of AIza ones, and older
                # releases of google-genai refuse them with exactly this error,
                # which reads like a revoked credential and sends you to the
                # console to mint another one that fails identically.
                if key.startswith("AQ.") and (
                        "API_KEY_INVALID" in msg.upper()
                        or "ACCESS_TOKEN_TYPE_UNSUPPORTED" in msg.upper()
                        or "API KEY NOT VALID" in msg.upper()):
                    print(f"  {'':28}          ^ this is an Auth key (AQ.), "
                          f"not a Standard key (AIza).")
                    print(f"  {'':28}            Older google-genai rejects "
                          f"these. You have {_sdk_version()}.")
                    print(f"  {'':28}            Try:  pip install -U "
                          f"google-genai")
            else:
                print(f"  {label:<28} ERROR    {code}: {msg[:60]}")
        except Exception as e:                          # noqa: BLE001
            print(f"  {label:<28} ERROR    {e.__class__.__name__}: {str(e)[:56]}")

    rpm = float(os.getenv("PRAMAN_GEMINI_RPM", "10"))
    print(f"\n{live}/{len(uniq)} keys usable — pool paced at "
          f"{live * rpm:.0f} calls/min")
    if live:
        for label, calls in (("held-out 100 cases", 85), ("full 600-case set", 525)):
            mins = calls / max(live * rpm, 1)
            print(f"  {label:<22} ~{calls:>3} calls  ~{mins:4.0f} min")
    return 0 if live else 1


if __name__ == "__main__":
    raise SystemExit(main())
