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


def next_pacific_midnight(now: datetime | None = None) -> datetime:
    now = (now or datetime.now(timezone.utc)).astimezone(PACIFIC)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.astimezone(timezone.utc)


def looks_like_daily_exhaustion(message: str) -> bool:
    m = (message or "").lower()
    return any(marker in m for marker in _DAILY_MARKERS)


@dataclass
class KeyState:
    key: str
    label: str
    calls: int = 0
    failures: int = 0
    exhausted_until: datetime | None = None

    def available(self, now: datetime) -> bool:
        return self.exhausted_until is None or now >= self.exhausted_until

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
                 rpm_per_key: float = 10.0):
        keys = keys if keys is not None else load_keys_from_env()
        if not keys:
            raise RuntimeError(
                "no Gemini keys found. Set GEMINI_API_KEYS in .env as a "
                "comma-separated list, e.g.\n"
                '  GEMINI_API_KEYS="AIza...one,AIza...two,AIza...three"')
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
        return sum(1 for k in self.keys if k.available(now))

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
                if state.available(now):
                    state.calls += 1
                    return self._client_for(state), state
            soonest = min((k.exhausted_until for k in self.keys
                           if k.exhausted_until), default=None)
            raise AllKeysExhausted(
                f"all {len(self.keys)} keys have spent their daily quota"
                + (f"; the first resets at {soonest.isoformat()}" if soonest else ""))

    def mark_daily_exhausted(self, state: KeyState) -> None:
        with self._lock:
            state.exhausted_until = next_pacific_midnight()
            state.failures += 1

    def mark_transient(self, state: KeyState) -> None:
        with self._lock:
            state.failures += 1

    def usage(self) -> list[dict]:
        return [{"key": k.masked, "calls": k.calls, "failures": k.failures,
                 "exhausted_until": (k.exhausted_until.isoformat()
                                     if k.exhausted_until else None)}
                for k in self.keys]

    def report(self) -> str:
        lines = [f"key pool: {self.live}/{len(self.keys)} live, "
                 f"paced at {self.rpm_per_key * len(self.keys):.0f} calls/min"]
        for u in self.usage():
            flag = "  EXHAUSTED until " + u["exhausted_until"][:16] \
                if u["exhausted_until"] else ""
            lines.append(f"  {u['key']:<28} {u['calls']:>4} calls  "
                         f"{u['failures']:>2} failures{flag}")
        return "\n".join(lines)


def load_keys_from_env() -> list[str]:
    raw = os.getenv("GEMINI_API_KEYS", "")
    keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not keys:
        single = os.getenv("GEMINI_API_KEY", "").strip()
        if single:
            keys = [single]
    return keys


def call_with_rotation(ring: GeminiKeyRing, fn, *, max_attempts: int | None = None,
                       on_retry=None):
    """Run ``fn(client)`` against the pool, rotating and backing off on 429.

    ``fn`` is called with a live client and must be idempotent -- a retry after a
    rate-limit rejection will call it again, and in this system every call is a
    fresh adjudication with no side effects, so that holds.

    Backoff is jittered because a pool of workers that all back off for exactly
    the same interval simply collides again one interval later.
    """
    from google.genai import errors

    attempts = max_attempts or max(6, len(ring) * 3)
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            client, state = ring.acquire()
        except AllKeysExhausted:
            raise
        try:
            return fn(client)
        except errors.APIError as exc:
            code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
            message = str(exc)
            rate_limited = code == 429 or "RESOURCE_EXHAUSTED" in message.upper()
            overloaded = code in _TRANSIENT_SERVER_CODES

            # Anything else -- a 400 schema error, a 403 bad key -- is a bug in
            # the request, not congestion. Rotating keys would just repeat it on
            # a different key and burn quota discovering that.
            if not (rate_limited or overloaded):
                raise

            last = exc
            if rate_limited and looks_like_daily_exhaustion(message):
                ring.mark_daily_exhausted(state)
                if on_retry:
                    on_retry(state, "daily quota spent; key retired for today")
                continue

            # A 503 is the model being busy, not the key being bad, so it is
            # deliberately not counted against the key's failure tally.
            if rate_limited:
                ring.mark_transient(state)
            delay = min(2 ** attempt, 30) * (0.5 + random.random())
            if on_retry:
                on_retry(state, ("rate limited" if rate_limited
                                 else f"model overloaded ({code})")
                         + f"; backing off {delay:.1f}s")
            time.sleep(delay)
    raise RuntimeError(
        f"exhausted {attempts} attempts across {len(ring)} keys; "
        f"last error: {last}")
