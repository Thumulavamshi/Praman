"""The dispute defender: read-only tools, and a verifier that can refuse the AI."""
import pytest

from praman.data.mandates import AGENT, ISSUER, variant
from praman.dispute.packet import (Claim, RepresentmentPacket, _normalise,
                                   verify_citations)
from praman.dispute.tools import DisputeTools
from praman.gate.adjudicator import StaticAdjudicator
from praman.gate.gate import Gate
from praman.ledger.engine import Engine
from praman.mandate.schema import CartItem, Proposal
from praman.orchestrator import Praman
from praman.pg.mock import MockRazorpay

WHEN = "2026-09-03T19:20:00+05:30"


def prop(amount="357.00", pid="p1", description=""):
    return Proposal(
        proposal_id=pid, mandate_id="mnd_reference", agent=AGENT,
        merchant_id="mch_dailymart", merchant_familiarity="established",
        merchant_onboarded="2023-04-11", proposed_at=WHEN, amount=amount,
        instrument="card",
        items=[CartItem(sku="GRO-0001", name="Atta 5kg", category="groceries",
                        merchant_id="mch_dailymart", price=amount,
                        description=description)])


@pytest.fixture
def case():
    p = Praman(Gate(adjudicator=StaticAdjudicator(), issuer=ISSUER),
               Engine(), MockRazorpay(seed=21))
    p.register_mandate(variant("reference"))
    out = p.purchase(prop())
    p.raise_chargeback(out.payment_id, "agent_not_authorized")
    cb = next(iter(p.engine.state.chargebacks.values()))
    return p, out.payment_id, cb.chargeback_id


# --- the tools --------------------------------------------------------------

def test_the_tools_reconstruct_the_whole_authorization_trail(case):
    p, pay, cb = case
    t = DisputeTools(p.engine, p.gate)

    m = t.mandate_chain(pay)
    assert m["principal"] == "user_9931" and m["signed"] is True
    assert "groceries" in m["delegation_in_the_humans_words"]

    d = t.decision_record(pay)
    assert d["verdict"] == "ALLOW" and d["cited_clause"]

    c = t.proposed_cart(pay)
    assert c["items"][0]["sku"] == "GRO-0001"

    money = t.money_trail(pay)
    assert money["amount_captured"] == "357.00"

    i = t.integrity_check()
    assert i["event_log_verified"] and i["decision_chain_verified"]


def test_every_tool_reports_a_missing_record_rather_than_inventing_one(case):
    p, _, _ = case
    t = DisputeTools(p.engine, p.gate)
    for fn in (t.mandate_chain, t.decision_record, t.proposed_cart, t.money_trail):
        assert "error" in fn("pay_does_not_exist")


def test_the_money_trail_reports_the_book_as_of_capture_not_today(case):
    p, pay, _ = case
    t = DisputeTools(p.engine, p.gate)
    at_capture = t.money_trail(pay)["pg_receivable_as_of_capture"]
    for i in range(3):
        p.purchase(prop("400.00", pid=f"later{i}"))
    assert t.money_trail(pay)["pg_receivable_as_of_capture"] == at_capture


def test_integrity_check_catches_a_tampered_log(case):
    p, pay, _ = case
    t = DisputeTools(p.engine, p.gate)
    p.engine.eventlog.events()[1].payload["amount"] = "99999.00"
    i = t.integrity_check()
    assert not i["event_log_verified"] and i["first_bad_event"]


def test_the_tools_record_their_own_call_trail(case):
    p, pay, _ = case
    t = DisputeTools(p.engine, p.gate)
    t.mandate_chain(pay)
    t.money_trail("nope")
    assert [c["tool"] for c in t.calls] == ["mandate_chain", "money_trail"]
    assert t.calls[0]["found"] and not t.calls[1]["found"]


# --- the verifier: this is the part that matters ---------------------------

def packet(**kw) -> RepresentmentPacket:
    base = dict(payment_id="pay_1", chargeback_id="cb_1", summary="s",
                argument="a", recommended_action="represent", weaknesses=[],
                claims=[])
    base.update(kw)
    return RepresentmentPacket(**base)


RECORDS = {
    "ev_cap_1": {"source_id": "ev_cap_1", "amount_captured": "357.00",
                 "captured_at": "2026-09-03T19:20:00+05:30"},
    "mnd_reference": {"source_id": "mnd_reference", "principal": "user_9931",
                      "delegation_in_the_humans_words":
                          "You can order my groceries and household things."},
}


def claim(value, source_id="ev_cap_1", kind="event"):
    return Claim(statement="a claim", value=value, source_kind=kind,
                 source_id=source_id)


def test_a_claim_that_quotes_the_record_is_accepted():
    v = verify_citations(packet(claims=[claim("357.00")]), RECORDS)
    assert v.accepted and v.citation_accuracy == 1.0


def test_a_fabricated_figure_is_refused():
    """The failure mode this whole layer exists to catch."""
    v = verify_citations(packet(claims=[claim("35700.00")]), RECORDS)
    assert not v.accepted
    assert "does not appear" in v.failures[0].problem


def test_one_bad_claim_refuses_the_whole_packet():
    """No threshold at which some fabrication is acceptable."""
    v = verify_citations(
        packet(claims=[claim("357.00"), claim("999.00")]), RECORDS)
    assert not v.accepted
    assert len(v.verified) == 1 and len(v.failures) == 1
    assert "REFUSED BY VERIFIER" in v.render()


def test_a_citation_to_a_record_never_fetched_is_refused():
    """Citing something the investigation never read is a fabricated citation."""
    v = verify_citations(
        packet(claims=[claim("357.00", source_id="ev_never_read")]), RECORDS)
    assert not v.accepted
    assert "never retrieved" in v.failures[0].problem


def test_currency_and_formatting_noise_does_not_fail_an_honest_claim():
    for value in ["INR 357.00", "357.00", "357", "₹357.00", " 357.00 "]:
        assert verify_citations(packet(claims=[claim(value)]), RECORDS).accepted, value


def test_a_genuinely_different_amount_is_never_matched_by_normalisation():
    for value in ["357.10", "3570", "35.70"]:
        assert not verify_citations(packet(claims=[claim(value)]), RECORDS).accepted, value


def test_a_quote_from_a_long_free_text_field_is_accepted():
    v = verify_citations(packet(claims=[
        claim("order my groceries", source_id="mnd_reference", kind="mandate")]),
        RECORDS)
    assert v.accepted


def test_an_empty_value_is_refused_rather_than_matching_anything():
    assert not verify_citations(packet(claims=[claim("")]), RECORDS).accepted


def test_normalisation_is_not_so_loose_that_it_matches_across_fields():
    assert _normalise("INR 1,899.00") == "1899"
    assert _normalise("1899.50") == "1899.5"
    assert _normalise("ALLOW") == "allow"


# --- the loop itself, driven against a fake client -------------------------

def test_the_defender_investigates_then_produces_a_verified_packet(case, monkeypatch):
    """The whole loop: tool turn, then packet, then verification.

    Driven against a fake client so the logic is provable without spending a
    free-tier request. The live investigation phase is confirmed separately;
    what this pins down is the loop's own behaviour.
    """
    import types as pytypes

    from google.genai import types as gtypes

    from praman.dispute.defender import DisputeDefender
    from praman.keyring import GeminiKeyRing

    p, pay, cb = case
    packet_json = None

    class FakeModels:
        def __init__(self):
            self.turn = 0

        def generate_content(self, model, contents, config):
            self.turn += 1
            if self.turn == 1:
                # ask for one tool
                fc = pytypes.SimpleNamespace(name="money_trail",
                                             args={"payment_id": pay})
                part = pytypes.SimpleNamespace(function_call=fc, text=None)
                content = gtypes.Content(role="model", parts=[
                    gtypes.Part.from_function_call(name="money_trail",
                                                   args={"payment_id": pay})])
                return pytypes.SimpleNamespace(candidates=[
                    pytypes.SimpleNamespace(content=content)])
            content = gtypes.Content(role="model",
                                     parts=[gtypes.Part(text="investigated")])
            return pytypes.SimpleNamespace(candidates=[
                pytypes.SimpleNamespace(content=content)])

        def generate_content_stream(self, model, contents, config):
            yield pytypes.SimpleNamespace(text=packet_json)

    class FakeClient:
        def __init__(self, api_key=None):
            self.models = FakeModels()

    fake = FakeClient()
    ring = GeminiKeyRing(["k" * 20], rpm_per_key=6000, model="fake")
    monkeypatch.setattr(ring, "_client_for", lambda state: fake)

    d = DisputeDefender(p.engine, p.gate, ring=ring, model="fake")

    # a packet whose claims quote the record the tool actually returned
    trail = DisputeTools(p.engine, p.gate).money_trail(pay)
    packet_json = RepresentmentPacket(
        payment_id=pay, chargeback_id=cb, summary="s", argument="a",
        recommended_action="represent", weaknesses=["none"],
        claims=[Claim(statement="the captured amount",
                      value=trail["amount_captured"], source_kind="event",
                      source_id=trail["source_id"])]).model_dump_json()

    res = d.defend(pay, cb)
    assert res.error == ""
    assert [c["tool"] for c in res.tool_calls] == ["money_trail"]
    assert res.ok and res.verified.accepted
    assert "REPRESENTMENT PACKET" in res.render()


def test_a_packet_citing_a_figure_not_in_the_record_is_refused(case, monkeypatch):
    """The verifier outranks the model, demonstrated through the real loop."""
    import types as pytypes

    from google.genai import types as gtypes

    from praman.dispute.defender import DisputeDefender
    from praman.keyring import GeminiKeyRing

    p, pay, cb = case

    fabricated = RepresentmentPacket(
        payment_id=pay, chargeback_id=cb, summary="s", argument="a",
        recommended_action="represent", weaknesses=[],
        claims=[Claim(statement="the captured amount", value="99999.00",
                      source_kind="event", source_id=f"ev_cap_{pay}")]
    ).model_dump_json()

    class FakeModels:
        def __init__(self):
            self.turn = 0

        def generate_content(self, model, contents, config):
            self.turn += 1
            if self.turn == 1:
                return pytypes.SimpleNamespace(candidates=[
                    pytypes.SimpleNamespace(content=gtypes.Content(
                        role="model", parts=[gtypes.Part.from_function_call(
                            name="money_trail", args={"payment_id": pay})]))])
            return pytypes.SimpleNamespace(candidates=[
                pytypes.SimpleNamespace(content=gtypes.Content(
                    role="model", parts=[gtypes.Part(text="done")]))])

        def generate_content_stream(self, model, contents, config):
            yield pytypes.SimpleNamespace(text=fabricated)

    class FakeClient:
        def __init__(self, api_key=None):
            self.models = FakeModels()

    fake = FakeClient()
    ring = GeminiKeyRing(["k" * 20], rpm_per_key=6000, model="fake")
    monkeypatch.setattr(ring, "_client_for", lambda state: fake)

    res = DisputeDefender(p.engine, p.gate, ring=ring, model="fake").defend(pay, cb)
    assert not res.ok
    assert res.verified.failures
    assert "REFUSED BY VERIFIER" in res.render()


def test_a_pool_with_no_budget_fails_before_spending_tool_calls(case):
    """An investigation is ~7 calls; discovering the pool is dry halfway wastes them."""
    from praman.dispute.defender import DisputeDefender
    from praman.keyring import GeminiKeyRing

    p, pay, cb = case
    ring = GeminiKeyRing(["k" * 20], rpm_per_key=6000, model="fake")
    ring.mark_daily_exhausted(ring.keys[0])
    res = DisputeDefender(p.engine, p.gate, ring=ring, model="fake").defend(pay, cb)
    assert res.tool_calls == [] and "no key in the pool has budget" in res.error
