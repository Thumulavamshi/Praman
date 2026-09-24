"""The MCP server: the buyer agent's side of the gate.

The property worth testing here is not that the tools return sensible JSON. It
is that the tool surface gives the agent no way to move money except through a
decision. An agent is the least trustworthy component in this system -- it reads
seller-written text and its reasoning is not auditable afterwards -- so what
matters is what it *cannot* do.
"""
import json

import pytest

pytest.importorskip(
    "mcp", reason="pip install -r requirements.txt to test the MCP server")


@pytest.fixture
def srv(monkeypatch):
    """A fresh server with the deterministic double. No keys, no network."""
    monkeypatch.setenv("PRAMAN_PROVIDER", "offline")
    monkeypatch.setenv("PRAMAN_PG", "mock")
    # A Thursday evening, inside the reference mandate's window. Without this
    # the suite passes by day and fails between 23:00 and 06:00 IST, because
    # the gate correctly refuses a middle-of-the-night purchase -- cases.py
    # says it plainly: a case that depends on the wall clock fails at 3am.
    monkeypatch.setenv("PRAMAN_MCP_AT", "2026-09-03T19:20:00+05:30")
    from praman.mcp import server as s
    s._state.clear()
    yield s
    s._state.clear()


def test_no_tool_can_move_money_without_a_decision(srv):
    """The whole thesis, as a test over the tool surface.

    If a later change adds a capture, refund or override tool, this fails --
    which is the point. An agent that can reach the gateway directly makes every
    other guarantee in this repo decorative.
    """
    import asyncio
    names = {t.name for t in asyncio.run(srv.server.list_tools())}
    assert names == {"get_mandate", "search_catalog", "propose_purchase",
                     "get_decision"}
    for banned in ("capture", "refund", "pay", "override", "approve"):
        assert not any(banned in n for n in names), f"{banned} tool is exposed"


def test_a_denied_category_is_blocked_and_never_reaches_the_gateway(srv):
    out = srv.propose_purchase(["ALC-0005"], reason="restocking the cabinet")
    assert out["verdict"] == "BLOCK"
    assert out["cited_clause"] == "categories_denied"
    assert out["payment_id"] is None
    assert out["captured_amount"] is None


def test_an_in_scope_cart_is_captured_and_booked(srv):
    out = srv.propose_purchase(["GRO-0001", "GRO-0006"], reason="weekly staples")
    assert out["verdict"] == "ALLOW"
    assert out["payment_id"].startswith("pay_")
    assert out["captured_amount"] == "357.00"


def test_the_agents_own_reason_is_hash_chained_into_the_record(srv):
    """The agent's stated reasoning is the part of the trail that lives nowhere
    else, and it is exactly what a cardholder disputes a year later. It has to
    be in the tamper-evident log, not only in the tool's reply."""
    reason = "atta and milk, the weekly staples, from the usual supermarket"
    srv.propose_purchase(["GRO-0001", "GRO-0006"], reason=reason)

    engine = srv._state["praman"].engine
    proposed = [e for e in engine.eventlog
                if e.type == "agent_purchase_proposed"]
    assert len(proposed) == 1
    assert proposed[0].payload["proposal"]["agent_reason"] == reason

    chain_ok, violations, _ = engine.verify()
    assert chain_ok and not violations


def test_a_decision_reads_back_and_the_chain_verifies(srv):
    out = srv.propose_purchase(["ALC-0005"], reason="whisky")
    back = srv.get_decision(out["decision_id"])
    assert back["chain_verified"] is True
    assert back["decision"]["verdict"] == "BLOCK"
    assert srv.get_decision("dec_nope")["error"]


def test_the_catalog_hands_over_seller_text_labelled_as_the_sellers(srv):
    """Injections live in the catalog on purpose. The agent must be able to read
    them -- that is the attack surface being demonstrated -- but never handed
    them as though they were ours."""
    res = srv.search_catalog("salt")
    assert res["n"] >= 1
    assert "seller" in res["note"]
    assert all("seller_description" in r for r in res["results"])
    assert all("registered_category" in r for r in res["results"])


def test_an_unknown_sku_is_refused_rather_than_guessed(srv):
    assert "error" in srv.propose_purchase(["NOPE-9999"])
    assert "error" in srv.propose_purchase([])
    assert "error" in srv.propose_purchase(["GRO-0001"], quantities=[1, 2])


def test_the_mandate_says_which_adjudicator_actually_decided(srv):
    """A run on the deterministic double must never be mistaken for a model's
    verdict. The fallback is legitimate; hiding it is not."""
    m = srv.get_mandate()
    assert m["signature_verifies"] is True
    assert m["adjudicator"] == "StaticAdjudicator"
    assert "NOT a model" in m["adjudicator_note"] or "offline" in m["adjudicator_note"]
    assert json.dumps(m)          # the tool's reply must be serialisable


def test_the_same_cart_is_refused_in_the_middle_of_the_night(srv, monkeypatch):
    """"Not in the middle of the night" is a bound the person actually wrote.

    This is the case that caught the suite out: run unpinned at 01:45 IST and
    the groceries cart above blocks, correctly. Worth an explicit test rather
    than a pinned clock that hides it -- the time window is a real constraint
    and a demo rehearsed at midnight will meet it.
    """
    monkeypatch.setenv("PRAMAN_MCP_AT", "2026-09-03T02:00:00+05:30")
    srv._state.clear()
    out = srv.propose_purchase(["GRO-0001", "GRO-0006"], reason="weekly staples")
    assert out["verdict"] != "ALLOW"
    assert out["payment_id"] is None


def test_the_agent_cannot_supply_its_own_timestamp(srv):
    """The clock is a fact the agent must not be able to forge.

    If propose_purchase took a time, an agent could walk a 3am purchase into the
    allowed window by asserting a different hour, and the time bound would stop
    being a bound. The override is an environment variable the operator sets,
    on the server's side of the boundary.
    """
    import asyncio
    tools = {t.name: t for t in asyncio.run(srv.server.list_tools())}
    params = tools["propose_purchase"].input_schema.get("properties", {})
    for forgeable in ("at", "proposed_at", "timestamp", "time", "now"):
        assert forgeable not in params, f"agent can supply {forgeable}"
    assert set(params) == {"skus", "reason", "quantities", "merchant_id"}
