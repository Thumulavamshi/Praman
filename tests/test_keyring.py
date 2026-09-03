"""Key rotation, pacing, and the RPM-vs-RPD distinction that makes a pool last."""
from datetime import datetime, timedelta, timezone

import pytest

from praman.keyring import (AllKeysExhausted, GeminiKeyRing, RateLimiter,
                            call_with_rotation, looks_like_daily_exhaustion,
                            next_pacific_midnight)

KEYS = ["AIzaKEY_ONE_xxxxxxxx", "AIzaKEY_TWO_xxxxxxxx", "AIzaKEY_THREE_xxxxxx"]


class FakeAPIError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@pytest.fixture(autouse=True)
def fake_genai(monkeypatch):
    """Stand in for google.genai so the ring can be tested without network."""
    import sys
    import types as pytypes

    made = []

    class FakeClient:
        def __init__(self, api_key):
            self.api_key = api_key
            made.append(api_key)

    genai_mod = pytypes.ModuleType("google.genai")
    genai_mod.Client = FakeClient
    errors_mod = pytypes.ModuleType("google.genai.errors")
    errors_mod.APIError = FakeAPIError
    google_mod = pytypes.ModuleType("google")
    google_mod.genai = genai_mod
    genai_mod.errors = errors_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors_mod)
    return made


def ring(**kw):
    return GeminiKeyRing(KEYS, rpm_per_key=kw.pop("rpm_per_key", 6000), **kw)


# --- the pool ---------------------------------------------------------------

def test_the_same_key_pasted_twice_does_not_double_your_quota():
    r = GeminiKeyRing(["a" * 20, "b" * 20, "a" * 20], rpm_per_key=6000)
    assert len(r) == 2


def test_an_empty_pool_says_how_to_fix_it():
    with pytest.raises(RuntimeError, match="GEMINI_API_KEYS"):
        GeminiKeyRing([])


def test_keys_are_never_printed_in_full():
    r = ring()
    for line in r.report().splitlines()[1:]:
        for k in KEYS:
            assert k not in line


def test_keys_rotate_round_robin():
    r = ring()
    used = [r.acquire()[1].label for _ in range(6)]
    assert used == ["key1", "key2", "key3", "key1", "key2", "key3"]


def test_env_parsing_accepts_a_list_or_a_single_key(monkeypatch):
    from praman.keyring import load_keys_from_env
    monkeypatch.setenv("GEMINI_API_KEYS", " a , b ,, c ")
    assert load_keys_from_env() == ["a", "b", "c"]
    monkeypatch.delenv("GEMINI_API_KEYS")
    monkeypatch.setenv("GEMINI_API_KEY", "solo")
    assert load_keys_from_env() == ["solo"]


# --- RPM vs RPD -------------------------------------------------------------

def test_a_burst_limit_is_not_mistaken_for_a_spent_day():
    assert not looks_like_daily_exhaustion(
        "429 RESOURCE_EXHAUSTED: Resource has been exhausted (e.g. check quota).")
    assert looks_like_daily_exhaustion(
        "Quota exceeded for quota metric generate_requests_per_model_per_day")
    assert looks_like_daily_exhaustion("You exceeded your requests per day limit")


def test_an_ambiguous_429_is_treated_as_recoverable():
    """Retiring a good key for the day is the expensive mistake."""
    assert not looks_like_daily_exhaustion("something went wrong")


def test_a_daily_exhausted_key_leaves_rotation_until_the_quota_resets():
    r = ring()
    _, state = r.acquire()
    r.mark_daily_exhausted(state)
    assert r.live == 2
    assert state.exhausted_until == next_pacific_midnight()
    labels = {r.acquire()[1].label for _ in range(6)}
    assert state.label not in labels


def test_a_pool_with_every_key_spent_raises_rather_than_looping():
    r = ring()
    for k in list(r.keys):
        r.mark_daily_exhausted(k)
    with pytest.raises(AllKeysExhausted, match="daily quota"):
        r.acquire()


# --- rotation under load ----------------------------------------------------

def test_a_rate_limited_call_retries_on_the_next_key_and_succeeds():
    r = ring()
    calls = []

    def fn(client):
        calls.append(client.api_key)
        if len(calls) < 3:
            raise FakeAPIError(429, "Resource has been exhausted")
        return "verdict"

    assert call_with_rotation(r, fn, max_attempts=6) == "verdict"
    assert len(calls) == 3
    assert len(set(calls)) == 3          # it moved on rather than hammering one


def test_a_daily_429_retires_the_key_instead_of_backing_off():
    r = ring()
    seen = []

    def fn(client):
        seen.append(client.api_key)
        if len(seen) == 1:
            raise FakeAPIError(429, "requests per day limit exceeded")
        return "ok"

    assert call_with_rotation(r, fn, max_attempts=4) == "ok"
    assert r.live == 2


def test_a_non_rate_limit_error_is_raised_not_retried():
    """A 400 is a bug in our request. Rotating keys would just repeat it."""
    r = ring()

    def fn(client):
        raise FakeAPIError(400, "invalid response_schema")

    with pytest.raises(FakeAPIError):
        call_with_rotation(r, fn, max_attempts=4)


def test_exhausting_the_pool_mid_run_propagates_rather_than_hanging():
    r = ring()

    def fn(client):
        raise FakeAPIError(429, "per day quota exceeded")

    with pytest.raises(AllKeysExhausted):
        call_with_rotation(r, fn, max_attempts=10)


# --- pacing -----------------------------------------------------------------

def test_the_limiter_paces_calls_to_the_pools_aggregate_rate():
    import time
    lim = RateLimiter(per_minute=120)        # 2/sec
    for _ in range(int(lim.capacity)):       # drain the initial bucket
        lim.acquire()
    start = time.monotonic()
    lim.acquire()
    assert time.monotonic() - start >= 0.3   # had to wait for a refill


def test_the_pool_rate_scales_with_the_number_of_keys():
    assert GeminiKeyRing(KEYS, rpm_per_key=10).limiter.capacity == 30
    assert GeminiKeyRing(KEYS[:1], rpm_per_key=10).limiter.capacity == 10


# --- the adjudicator on top of the ring ------------------------------------

def test_the_gemini_adjudicator_returns_a_verdict_from_a_fake_client(monkeypatch):
    """The whole path, without spending a single free-tier request."""
    import sys
    import types as pytypes

    from praman.gate.adjudicator import Adjudication

    verdict = Adjudication(
        verdict="ALLOW", reason="in scope", cited_clause="categories_allowed",
        listing_attempted_instruction=False, confidence="high")

    class FakeModels:
        def generate_content(self, model, contents, config):
            assert "untrusted-listing" in contents      # fencing survived
            return pytypes.SimpleNamespace(
                parsed=verdict, text=verdict.model_dump_json(),
                usage_metadata=pytypes.SimpleNamespace(
                    prompt_token_count=1800, candidates_token_count=250))

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    genai_mod = sys.modules["google.genai"]
    monkeypatch.setattr(genai_mod, "Client", FakeClient)
    types_mod = pytypes.ModuleType("google.genai.types")
    types_mod.GenerateContentConfig = lambda **kw: kw
    types_mod.ThinkingConfig = lambda **kw: kw
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)
    monkeypatch.setattr(genai_mod, "types", types_mod, raising=False)

    from praman.gate.bounds import check_bounds
    from praman.gate.gemini import GeminiAdjudicator
    from praman.mandate.schema import CartItem, Proposal, Mandate, Scope

    m = Mandate(mandate_id="mnd_1", principal="u", agent="a",
                scope=Scope(categories_allowed=["groceries"],
                            per_transaction_cap="2000.00"),
                issued_at="2026-08-01T10:00:00+05:30",
                expires_at="2026-12-01T10:00:00+05:30")
    p = Proposal(proposal_id="p1", mandate_id="mnd_1", agent="a",
                 merchant_id="mch_dailymart", proposed_at="2026-09-03T19:20:00+05:30",
                 amount="285.00",
                 items=[CartItem(sku="S1", name="Atta", category="groceries",
                                 merchant_id="mch_dailymart", price="285.00")])

    adj = GeminiAdjudicator(ring=GeminiKeyRing(KEYS, rpm_per_key=6000))
    r = adj.adjudicate(m, p, check_bounds(m, p))
    assert r.verdict == "ALLOW" and r.error == ""
    assert r.input_tokens == 1800 and r.output_tokens == 250


def test_an_exhausted_pool_fails_closed_to_step_up_never_to_allow(monkeypatch):
    """The failure mode that actually happens on a free tier."""
    from praman.gate.bounds import check_bounds
    from praman.gate.gemini import GeminiAdjudicator
    from praman.mandate.schema import CartItem, Proposal, Mandate, Scope

    r = GeminiKeyRing(KEYS, rpm_per_key=6000)
    for k in list(r.keys):
        r.mark_daily_exhausted(k)

    m = Mandate(mandate_id="mnd_1", principal="u", agent="a",
                scope=Scope(categories_allowed=["groceries"]),
                issued_at="2026-08-01T10:00:00+05:30",
                expires_at="2026-12-01T10:00:00+05:30")
    p = Proposal(proposal_id="p1", mandate_id="mnd_1", agent="a",
                 merchant_id="mch_dailymart", proposed_at="2026-09-03T19:20:00+05:30",
                 amount="285.00",
                 items=[CartItem(sku="S1", name="Atta", category="groceries",
                                 merchant_id="mch_dailymart", price="285.00")])

    out = GeminiAdjudicator(ring=r).adjudicate(m, p, check_bounds(m, p))
    assert out.verdict == "STEP_UP"
    assert "daily quota" in out.reason
    assert out.error
