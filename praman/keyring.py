"""Rotating a pool of free-tier Gemini keys, and pacing calls so they last.

Free-tier quota has two limits that fail in completely different ways, and
treating them alike is what burns a key pool in ten minutes:

  **RPM** — a burst limit. You hit it, you wait a few seconds, it clears. The
  fix is pacing, and pacing is something we control: a token bucket that simply
  does not issue more calls per minute than the pool can serve is strictly
  better than firing them and handling the 429s, because a rejected request
  still cost a round trip and, on some quotas, still counted.

  **RPD** — a daily allocation. It does not clear for hours. A key that has hit
  it must be taken out of rotation entirely, not retried; retrying it wastes a
  slot on every pass through the ring.

Measured, not assumed, against real keys in September 2026:

    GenerateRequestsPerMinutePerProjectPerModel-FreeTier   5
    GenerateRequestsPerDayPerProjectPerModel-FreeTier     20

**Twenty requests per day, per key, per model.** Not the ~1,500 the public
free-tier tables suggest. Six keys therefore buy 120 requests per model per day
-- roughly one 100-case evaluation, or fifteen dispute investigations. Quota is
per MODEL though, so switching models is what actually multiplies the budget.
Plan around 20, and check before starting anything long.

So this class does three things: paces calls to fit the pool's aggregate RPM,
rotates across keys so the load spreads, and quarantines a key that has
exhausted its day until the quota resets. It is thread-safe because the
evaluation harness runs a pool of workers.

Google resets daily quota at midnight US/Pacific. That is the reset this uses;
if it turns out to be wrong for a given quota the only cost is a key sitting out
slightly too long, which is the safe direction to be wrong in.
"""
from __future__ import annotations

import itertools
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# US/Pacific without pulling in a tz database dependency. Pacific is UTC-8 in
# winter and UTC-7 in summer; using -8 means we consider the day reset an hour
# late during DST, which keeps a key quarantined slightly longer than necessary
# rather than putting it back into rotation before its quota actually reset.
PACIFIC = timezone(timedelta(hours=-8))

# Markers that distinguish "you have used up today" from "you are going too
# fast". Google phrases these differently across quota types, so match broadly
# and default to the recoverable interpretation when unsure -- treating an RPM
# blip as a dead key would idle a perfectly good key for the rest of the day.
_DAILY_MARKERS = ("perday", "per day", "requests per day", "daily limit",
                  "generate_requests_per_model_per_day", "quota_limit_value")

# Server-side hiccups. 503 UNAVAILABLE ("this model is currently experiencing
# high demand") is the common one, and it is emphatically NOT a key problem --
# retiring or even penalising a key for it would be wrong. Retry with backoff,
# rotating while doing so only because a different key may land on a less
# congested endpoint. Observed on a real run: three of a hundred cases came back
# 503 and, with only 429 retried, each burned a case and failed closed to
# STEP_UP. Correct behaviour, wasted measurement.
_TRANSIENT_SERVER_CODES = (500, 502, 503, 504)


def _transport_errors() -> tuple:
    """Connection-level failures worth retrying, if httpx is importable.

    These arrive as exceptions from the HTTP layer rather than as an APIError
    with a status, so the API error path never sees them.
    """
    try:
        import httpx
        return (httpx.RemoteProtocolError, httpx.ConnectError,
                httpx.ReadError, httpx.WriteError, httpx.ReadTimeout,
                httpx.ConnectTimeout, httpx.PoolTimeout)
    except Exception:                                   # noqa: BLE001
        return (ConnectionError,)


_TRANSPORT_ERRORS = _transport_errors()


def next_pacific_midnight(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(PACIFIC)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.astimezone(timezone.utc)


def looks_like_daily_exhaustion(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _DAILY_MARKERS)


def retry_delay_from(message: str) -> float | None:
    """Google says when to come back. Believe it rather than guessing.

    A 429 carries a RetryInfo block -- 'retryDelay': '45s' -- which is the
    server's own statement of when the window reopens. Blind exponential
    backoff either undershoots it (and burns another rejection) or overshoots
    it (and wastes the wait). Observed: a 5 RPM limit returning retryDelay 45s
    while our backoff was still at 1.5 seconds.
    """
    m = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", message or "")
    if not m:
        m = re.search(r"retry in (\d+(?:\.\d+)?)s", message or "", re.I)
    if not m:
        return None
    try:
        return min(float(m.group(1)), 120.0)
    except ValueError:
        return None


@dataclass
class KeyState:
    """One key's state, with daily exhaustion tracked PER MODEL.

    Verified empirically, not assumed: a key that returns 429 "exceeded your
    quota" on gemini-3.5-flash answers normally on gemini-3-flash-preview in the
    same second. Daily allocation is per key per model, so retiring a key
    wholesale on one model's quota would throw away every other model it can
    still serve -- on a six-key pool that is most of the day's budget.
    """
    key: str
    label: str
    calls: int = 0
    failures: int = 0
    # model -> the UTC instant that model's daily quota resets for this key
    exhausted_until: dict[str, datetime] = field(default_factory=dict)

    def available(self, now: datetime, model: str = "") -> bool:
        until = self.exhausted_until.get(model)
        return until is None or now >= until

    def spent_models(self) -> list[str]:
        now = datetime.now(timezone.utc)
        return sorted(m for m, t in self.exhausted_until.items() if now < t)

    @property
    def masked(self) -> str:
        return f"{self.label} ({self.key[:6]}...{self.key[-4:]})" if len(self.key) > 12 \
            else self.label


class AllKeysExhausted(RuntimeError):
    """Every key in the pool has spent its daily allocation."""


class RateLimiter:
    """A token bucket over the whole pool. Paces, rather than reacting to 429s.

    ``per_minute`` is the aggregate the pool can serve. Refills continuously
    rather than in one-minute steps, so a burst at the top of the minute does
    not starve the rest of it.
    """

    def __init__(self, per_minute: float):
        self.rate = max(per_minute, 0.1) / 60.0        # tokens per second
        self.capacity = max(per_minute, 1.0)
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> float:
        """Block until a call may be made. Returns how long it waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return waited
                need = (1.0 - self._tokens) / self.rate
            time.sleep(min(need, 5.0))
            waited += min(need, 5.0)


class GeminiKeyRing:
    """A pool of free-tier keys, rotated and paced.

    Keys come from ``GEMINI_API_KEYS`` as a comma-separated list, falling back to
    a single ``GEMINI_API_KEY``. Nothing here logs a key; ``masked`` exists so a
    progress line can name which key is misbehaving without printing it.
    """

    def __init__(self, keys: list[str] | None = None, *,
                 rpm_per_key: float = 5.0, model: str = ""):
        keys = keys if keys is not None else load_keys_from_env()
        if not keys:
            raise RuntimeError(
                "no Gemini keys found. Set GEMINI_API_KEYS in .env as a "
                "comma-separated list, e.g.\n"
                '  GEMINI_API_KEYS="AQ.Ab...one,AQ.Ab...two,AQ.Ab...three"\n'
                'AI Studio issues Auth keys (AQ.Ab) now; older Standard keys '
                '(AIza) still work\nbut are being retired, and each format '
                'needs a google-genai new enough to send it.')
        # Deduplicate while preserving order. The same key pasted twice does not
        # double your quota, and silently letting it look like two keys would
        # make the pacing maths wrong in the dangerous direction.
        seen, uniq = set(), []
        for k in keys:
            if k and k not in seen:
                seen.add(k)
                uniq.append(k)
        self.keys = [KeyState(key=k, label=f"key{i + 1}")
                     for i, k in enumerate(uniq)]
        self.rpm_per_key = rpm_per_key
        # The model this ring is serving. Quota is per key per model, so the
        # ring has to know which one it is spending.
        self.model = model
        self.limiter = RateLimiter(rpm_per_key * len(self.keys))
        self._cycle = itertools.cycle(range(len(self.keys)))
        # Reentrant, deliberately: acquire() holds this lock while it picks a
        # key and then calls _client_for(), which takes it again to memoize the
        # client. With a plain Lock that is a deadlock on the very first call --
        # and one that no amount of reading catches, because it looks fine.
        self._lock = threading.RLock()
        self._clients: dict[str, object] = {}

    def __len__(self) -> int:
        return len(self.keys)

    @property
    def live(self) -> int:
        now = datetime.now(timezone.utc)
        return sum(1 for k in self.keys if k.available(now, self.model))

    def _client_for(self, state: KeyState):
        from google import genai
        with self._lock:
            c = self._clients.get(state.key)
            if c is None:
                c = genai.Client(api_key=state.key)
                self._clients[state.key] = c
            return c

    def acquire(self) -> tuple[object, KeyState]:
        """The next usable key, after waiting for a rate-limit slot."""
        self.limiter.acquire()
        now = datetime.now(timezone.utc)
        with self._lock:
            for _ in range(len(self.keys)):
                state = self.keys[next(self._cycle)]
                if state.available(now, self.model):
                    state.calls += 1
                    return self._client_for(state), state
            soonest = min((t for k in self.keys
                           for m, t in k.exhausted_until.items()
                           if m == self.model), default=None)
            raise AllKeysExhausted(
                f"all {len(self.keys)} keys have spent today's quota for "
                f"{self.model or '(model unset)'}"
                + (f"; it resets at {soonest.isoformat()}" if soonest else "")
                + ". Quota is per key PER MODEL -- another model may still have "
                  "budget on these same keys.")

    def mark_daily_exhausted(self, state: KeyState, model: str = "") -> None:
        with self._lock:
            state.exhausted_until[model or self.model] = next_pacific_midnight()
            state.failures += 1

    def mark_transient(self, state: KeyState) -> None:
        with self._lock:
            state.failures += 1

    def usage(self) -> list[dict]:
        now = datetime.now(timezone.utc)
        return [{"key": k.masked, "calls": k.calls, "failures": k.failures,
                 "spent_models": k.spent_models(),
                 "available": k.available(now, self.model)}
                for k in self.keys]

    def report(self) -> str:
        # "live" used to mean "not retired for today", which is an optimistic
        # default: a key that has never worked, and never will, counts as live
        # until something proves otherwise. A pool of one invalid key therefore
        # printed "1/1 live" at the top of a run in which every single call was
        # rejected with API_KEY_INVALID. The word was doing work it had not
        # earned, and it was the first line an operator reads.
        #
        # A key is USABLE when the ring is willing to try it. It is VERIFIED
        # only once a call through it has come back. Say which is which.
        verified = sum(1 for kk in self.keys if kk.calls and not kk.failures)
        head = (f"key pool: {self.live}/{len(self.keys)} usable on "
                f"{self.model or '(model unset)'}, paced at "
                f"{self.rpm_per_key * len(self.keys):.0f} calls/min")
        if not verified:
            head += "\n  (none verified yet — usable means not retired, not "
            head += "known good;\n   tools/check_keys.py tests each key for real)"
        lines = [head]
        for u in self.usage():
            flag = ("  SPENT TODAY on: " + ", ".join(u["spent_models"])
                    if u["spent_models"] else "")
            lines.append(f"  {u['key']:<28} {u['calls']:>4} calls  "
                         f"{u['failures']:>2} failures{flag}")
        return "\n".join(lines)


def load_keys(*names: str) -> list[str]:
    """Keys from the first of ``names`` that is set, comma separated.

    One parser for every provider, because the ways a key list gets mangled --
    a JSON array, stray quotes, a trailing comma -- have nothing to do with who
    issues the key, and a second copy of this would be a second place for the
    bracket bug to come back.
    """
    for name in names:
        raw = os.getenv(name, "").strip()
        if not raw:
            continue
        if raw.startswith("[") and raw.endswith("]"):
            raw = raw[1:-1]
        keys = [k.strip().strip('"').strip("'") for k in raw.split(",")]
        keys = [k for k in keys if k]
        if keys:
            return keys
    return []


def load_groq_keys_from_env() -> list[str]:
    """GROQ_API_KEYS, or GROQ_API_KEY, which people also fill with a list."""
    return load_keys("GROQ_API_KEYS", "GROQ_API_KEY")


def load_keys_from_env() -> list[str]:
    """Keys from GEMINI_API_KEYS, comma separated.

    A JSON-array spelling -- ``["AQ.one","AQ.two"]`` -- is accepted too, because
    it is an obvious thing to write for a list and there is no ambiguity about
    what was meant. It only works on ONE line, though, and that is not a
    limitation this function can lift: python-dotenv ends a value at the
    newline, so a bracket on its own line arrives here as the single character
    "[" and the keys were never in the process to begin with. check_keys.py
    recognises that shape and says so, because the resulting error -- "API key
    not valid" against a one-character key -- sends you to the API console
    rather than to your .env.
    """
    keys = load_keys("GEMINI_API_KEYS", "GEMINI_API_KEY")
    return keys


def gemini_classify(exc) -> tuple[bool, bool, int | None]:
    """(rate_limited, overloaded, code) for a google-genai error."""
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    message = str(exc)
    return (code == 429 or "RESOURCE_EXHAUSTED" in message.upper(),
            code in _TRANSIENT_SERVER_CODES,
            code)


def groq_classify(exc) -> tuple[bool, bool, int | None]:
    """(rate_limited, overloaded, code) for a groq SDK error.

    Groq's daily ceilings arrive as 429s whose text names the window, which
    ``looks_like_daily_exhaustion`` already recognises -- its markers are about
    English phrasing ("per day", "requests per day") rather than anything
    Google-specific.

    One thing worth knowing before trusting a pool here: Groq rate limits are
    enforced per ORGANISATION, not per key. Three keys cut from one account
    share one allowance, so rotation buys resilience against a single bad key
    and nothing at all in throughput. Keys from separate accounts do multiply.
    """
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if not isinstance(code, int):
        code = None
    return (code == 429, code in _TRANSIENT_SERVER_CODES, code)


def call_with_rotation(ring: GeminiKeyRing, fn, *, max_attempts: int | None = None,
                       max_transient: int = 10, on_retry=None,
                       api_error=None, classify=None):
    """Run ``fn(client)`` against the pool, rotating and backing off on 429.

    ``fn`` is called with a live client and must be idempotent -- a retry after a
    rate-limit rejection will call it again, and in this system every call is a
    fresh adjudication with no side effects, so that holds.

    Backoff is jittered because a pool of workers that all back off for exactly
    the same interval simply collides again one interval later.
    """
    if api_error is None:
        from google.genai import errors
        api_error = errors.APIError
    if classify is None:
        classify = gemini_classify

    attempts = max_attempts or max(6, len(ring) * 3)
    last: Exception | None = None
    # A "model is busy" 503 is not the caller's fault, not the key's fault, and
    # not a quota event -- it is congestion that clears. Counting it against the
    # same budget as a rate limit meant a burst of them could kill an
    # investigation that had already spent five tool calls. Transient failures
    # get their own, much more generous budget.
    transient_left = max_transient
    attempt = -1
    while True:
        attempt += 1
        if attempt >= attempts:
            break
        try:
            client, state = ring.acquire()
        except AllKeysExhausted:
            raise
        try:
            return fn(client)
        except _TRANSPORT_ERRORS as exc:
            # The connection died before the API could answer -- not a quota
            # problem, not a bad request, just the network. Observed killing a
            # dispute investigation that had already spent five tool calls, so
            # it is worth a retry rather than losing that work, and it draws on
            # the transient allowance rather than the attempt budget.
            last = exc
            if transient_left > 0:
                transient_left -= 1
                attempt -= 1
            delay = min(2 ** max(attempt, 0), 20) * (0.5 + random.random())
            if on_retry:
                on_retry(state, f"{exc.__class__.__name__}; retrying in "
                                f"{delay:.1f}s")
            time.sleep(delay)
            continue
        except api_error as exc:
            message = str(exc)
            rate_limited, overloaded, code = classify(exc)

            # Anything else -- a 400 schema error, a 403 bad key -- is a bug in
            # the request, not congestion. Rotating keys would just repeat it on
            # a different key and burn quota discovering that.
            if not (rate_limited or overloaded):
                raise

            last = exc
            if rate_limited and looks_like_daily_exhaustion(message):
                ring.mark_daily_exhausted(state, ring.model)
                if on_retry:
                    on_retry(state, "daily quota spent; key retired for today")
                continue

            # A 503 is the model being busy, not the key being bad, so it is
            # deliberately not counted against the key's failure tally -- nor
            # against the attempt budget, while its own allowance lasts.
            if rate_limited:
                ring.mark_transient(state)
            elif transient_left > 0:
                transient_left -= 1
                attempt -= 1
            told = retry_delay_from(message)
            delay = (told + random.random()
                     if told is not None
                     else min(2 ** attempt, 30) * (0.5 + random.random()))
            if on_retry:
                on_retry(state, ("rate limited" if rate_limited
                                 else f"model overloaded ({code})")
                         + f"; backing off {delay:.1f}s")
            time.sleep(delay)
    raise RuntimeError(
        f"exhausted {attempts} attempts across {len(ring)} keys; "
        f"last error: {last}")
