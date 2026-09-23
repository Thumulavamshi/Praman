"""The Groq adjudicator: same contract, third provider.

What is worth testing without a network is the contract, not the model: that
the prompt crossing the wire is byte-identical to the other providers', that an
unreachable provider fails closed to STEP_UP rather than open to ALLOW, and
that the pool is paced for how Groq actually meters.
"""
import json
import types

import pytest

pytest.importorskip("groq", reason="pip install -r requirements.txt")

from praman.data.heldout import HELDOUT_CASES
from praman.data.mandates import variant
from praman.gate.adjudicator import SYSTEM_PROMPT
from praman.gate.bounds import check_bounds
from praman.gate.groq import GroqAdjudicator, GroqKeyRing

VERDICT = {"verdict": "STEP_UP", "reason": "poised on a bridge the person did "
           "not write", "cited_clause": "nothing fancy or premium",
           "confidence": "medium", "listing_attempted_instruction": False}


def fake_client(sent, payload=None, raises=None):
    class Completions:
        def create(self, **kw):
            sent.append(kw)
            if raises:
                raise raises
            msg = types.SimpleNamespace(content=json.dumps(payload or VERDICT))
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg)],
                usage=types.SimpleNamespace(prompt_tokens=1900,
                                            completion_tokens=70))

    return types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=Completions()))


def adjudicate(adj, ring, case_id="c_oliveoil_staples"):
    case = next(c for c in HELDOUT_CASES if c.case_id == case_id)
    m, p = variant(case.mandate_variant), case.proposal()
    return adj.adjudicate(m, p, check_bounds(m, p))


@pytest.fixture
def ring(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEYS", "gsk_a,gsk_b,gsk_c")
    return GroqKeyRing()


def test_the_pool_is_paced_for_an_organisation_not_for_each_key(ring):
    """Groq's 30/min is per organisation. Pacing each of three keys at 30 asks
    for 90 and collects 429s, which is exactly how the Gemini run died."""
    assert len(ring) == 3
    assert ring.rpm_per_key * len(ring) == pytest.approx(30.0)


def test_the_prompt_on_the_wire_is_identical_to_the_other_providers(ring):
    """If each provider got its own prompt, the three sets of numbers would be
    measuring three different systems and comparing them would mean nothing."""
    sent = []
    ring._clients = {k.key: fake_client(sent) for k in ring.keys}
    adjudicate(GroqAdjudicator(ring=ring), ring)

    assert sent[0]["messages"][0]["role"] == "system"
    assert sent[0]["messages"][0]["content"] == SYSTEM_PROMPT
    assert sent[0]["temperature"] == 0.0


def test_the_untrusted_fence_survives_the_trip(ring):
    sent = []
    ring._clients = {k.key: fake_client(sent) for k in ring.keys}
    adjudicate(GroqAdjudicator(ring=ring), ring)
    user = sent[0]["messages"][1]["content"]
    assert "<untrusted-listing" in user and "</untrusted-listing" in user


def test_a_verdict_comes_back_with_its_provenance(ring):
    sent = []
    ring._clients = {k.key: fake_client(sent) for k in ring.keys}
    r = adjudicate(GroqAdjudicator(ring=ring, model="openai/gpt-oss-120b"), ring)
    assert r.verdict == "STEP_UP"
    assert r.model == "openai/gpt-oss-120b"
    assert r.input_tokens == 1900 and r.output_tokens == 70
    assert not r.error


def test_an_unparseable_answer_fails_closed_to_step_up(ring):
    """Never coerce a malformed verdict into a default. A default would be a
    guess, and the only safe guess in an authorization path is 'ask someone'."""
    sent = []
    bad = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(
            create=lambda **kw: types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content="not json at all"))],
                usage=None))))
    ring._clients = {k.key: bad for k in ring.keys}
    r = adjudicate(GroqAdjudicator(ring=ring), ring)
    assert r.verdict == "STEP_UP"
    assert r.error
    assert "(adjudicator error)" == r.cited_clause


def test_an_empty_completion_is_an_error_not_an_allow(ring):
    empty = types.SimpleNamespace(chat=types.SimpleNamespace(
        completions=types.SimpleNamespace(
            create=lambda **kw: types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    message=types.SimpleNamespace(content=""))], usage=None))))
    ring._clients = {k.key: empty for k in ring.keys}
    r = adjudicate(GroqAdjudicator(ring=ring), ring)
    assert r.verdict == "STEP_UP" and r.error


def test_a_model_without_structured_outputs_falls_back_once(ring):
    """The set of models supporting json_schema moves, so the code probes
    rather than carrying a list that goes stale -- but it must remember the
    answer, or every case costs two requests on the tier where requests are
    scarce."""
    import httpx
    from groq import BadRequestError

    def refusal():
        """A real SDK error. A stand-in built from SimpleNamespace raises
        AttributeError inside the SDK instead, which the adjudicator then
        reports as a generic failure -- so the test would pass on the fallback
        never running."""
        req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        return BadRequestError(
            "response_format json_schema is not supported by this model",
            response=httpx.Response(400, request=req,
                                    json={"error": {"message": "unsupported"}}),
            body=None)

    calls = []

    class Completions:
        def create(self, **kw):
            calls.append(kw["response_format"]["type"])
            if kw["response_format"]["type"] == "json_schema":
                raise refusal()
            msg = types.SimpleNamespace(content=json.dumps(VERDICT))
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=msg)], usage=None)

    c = types.SimpleNamespace(chat=types.SimpleNamespace(completions=Completions()))
    ring._clients = {k.key: c for k in ring.keys}
    adj = GroqAdjudicator(ring=ring)

    assert adjudicate(adj, ring).verdict == "STEP_UP"
    assert calls == ["json_schema", "json_object"]

    adjudicate(adj, ring)                    # second case: no second probe
    assert calls == ["json_schema", "json_object", "json_object"]


def test_keys_are_read_from_either_variable(monkeypatch):
    from praman.keyring import load_groq_keys_from_env
    monkeypatch.delenv("GROQ_API_KEYS", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_one, gsk_two,gsk_three")
    assert load_groq_keys_from_env() == ["gsk_one", "gsk_two", "gsk_three"]

    monkeypatch.setenv("GROQ_API_KEYS", "gsk_x,gsk_y")
    assert load_groq_keys_from_env() == ["gsk_x", "gsk_y"]
