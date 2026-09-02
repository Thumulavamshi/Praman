"""The HTTP surface, driven through the app rather than a live server."""
import pytest
from fastapi.testclient import TestClient

from praman.api import build_app
from praman.data.mandates import AGENT, ISSUER, variant
from praman.gate.adjudicator import StaticAdjudicator
from praman.gate.gate import Gate
from praman.ledger.engine import Engine
from praman.orchestrator import Praman
from praman.pg.mock import MockRazorpay

WHEN = "2026-09-03T19:20:00+05:30"


@pytest.fixture
def client():
    p = Praman(Gate(adjudicator=StaticAdjudicator(), issuer=ISSUER),
               Engine(), MockRazorpay(seed=3))
    return TestClient(build_app(p))


def proposal(amount="357.00", category="groceries", merchant="mch_dailymart",
             pid="p1"):
    return {
        "proposal_id": pid, "mandate_id": "mnd_reference", "agent": AGENT,
        "merchant_id": merchant, "merchant_familiarity": "established",
        "proposed_at": WHEN, "amount": amount, "instrument": "card",
        "items": [{"sku": "S1", "name": "item", "category": category,
                   "merchant_id": merchant, "price": amount}],
    }


def register(client):
    m = variant("reference").model_dump(exclude_none=True)
    return client.post("/mandates", json=m)


def test_health_reports_which_gateway_and_adjudicator_are_live(client):
    r = client.get("/health").json()
    assert r["ok"] and r["gateway"] == "mock"
    assert r["adjudicator"] == "StaticAdjudicator"


def test_registering_a_mandate_signs_it_if_it_arrives_unsigned(client):
    p = variant("reference").model_dump(exclude_none=True)
    p.pop("signature", None)
    r = client.post("/mandates", json=p).json()
    assert r["signature"].startswith("ed25519:")
    assert client.get(f"/mandates/{r['mandate_id']}").json()["signature_verifies"]


def test_authorize_decides_captures_and_books_in_one_call(client):
    register(client)
    r = client.post("/authorize", json=proposal()).json()
    assert r["verdict"] == "ALLOW" and r["payment_id"].startswith("pay_")
    assert r["captured_amount"] == "357.00"

    bal = {a["code"]: a["balance"] for a in
           client.get("/ledger/balances").json()["accounts"]}
    assert bal["1200"] != "0.00" and bal["4000"] != "0.00"


def test_a_blocked_proposal_returns_the_clause_it_enforced(client):
    register(client)
    r = client.post("/authorize",
                    json=proposal("4800.00", "alcohol", "mch_spiritsco")).json()
    assert r["verdict"] == "BLOCK" and r["payment_id"] == ""
    assert r["cited_clause"] == "categories_denied"


def test_a_malformed_proposal_is_rejected_by_the_schema_not_the_gate(client):
    register(client)
    bad = proposal()
    bad["amount"] = "-100.00"
    assert client.post("/authorize", json=bad).status_code == 422


def test_the_step_up_flow_needs_an_explicit_human_approval(client):
    register(client)
    p = proposal("1800.00")
    r = client.post("/authorize", json=p).json()
    assert r["verdict"] == "STEP_UP" and r["payment_id"] == ""

    approved = client.post("/step-up/approve", json={
        "decision_id": r["decision_id"], "approver": "user_9931",
        "proposal": p}).json()
    assert approved["verdict"] == "STEP_UP_APPROVED"
    assert approved["payment_id"].startswith("pay_")


def test_revoking_a_mandate_stops_further_authorizations(client):
    register(client)
    assert client.post("/authorize", json=proposal(pid="p1")).json()["verdict"] == "ALLOW"
    client.post("/mandates/mnd_reference/revoke")
    r = client.post("/authorize", json=proposal(pid="p2")).json()
    assert r["verdict"] == "BLOCK" and "revoked" in r["reason"]


def test_verify_reports_a_clean_book(client):
    register(client)
    client.post("/authorize", json=proposal())
    r = client.get("/ledger/verify").json()
    assert r["event_chain_ok"] and r["decision_chain_ok"]
    assert r["invariant_violations"] == []


def test_the_evidence_endpoint_returns_a_cited_packet(client):
    register(client)
    pay = client.post("/authorize", json=proposal()).json()["payment_id"]
    r = client.get(f"/evidence/{pay}").json()
    assert r["completeness"] == 1.0 and r["chain_verified"]
    assert any(c["claim"] == "gate verdict" for c in r["citations"])
    assert "REPRESENTMENT EVIDENCE PACKET" in r["rendered"]


def test_the_as_of_snapshot_endpoint_answers_the_dispute_question(client):
    """The book at the disputed capture, not the book after everything since."""
    register(client)
    pay = client.post("/authorize", json=proposal("357.00", pid="p1")).json()["payment_id"]
    at_capture = client.get(f"/ledger/snapshot/ev_cap_{pay}").json()["balances"]

    for i in range(3):
        client.post("/authorize", json=proposal("400.00", pid=f"later{i}"))

    now = {a["code"]: a["balance"] for a in
           client.get("/ledger/balances").json()["accounts"]}
    assert now["4000"] != at_capture["4000"]          # the book moved on
    again = client.get(f"/ledger/snapshot/ev_cap_{pay}").json()["balances"]
    assert again == at_capture                        # the snapshot did not


def test_unknown_ids_return_404_rather_than_an_empty_success(client):
    assert client.get("/decisions/dec_nope").status_code == 404
    assert client.get("/mandates/mnd_nope").status_code == 404
    assert client.get("/ledger/snapshot/ev_nope").status_code == 404
