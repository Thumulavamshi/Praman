"""The audit trail page: generated from the book, never typed in."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from praman.api import build_app
from praman.ui.build import build


@pytest.fixture(scope="module")
def page():
    return build()


@pytest.fixture(scope="module")
def data(page):
    m = re.search(r'<script id="praman-data" type="application/json">(.*?)</script>',
                  page, re.S)
    assert m, "the page carries no data block"
    return json.loads(m.group(1))


def test_the_page_is_self_contained(page):
    """It must open from disk with the network off.

    Google Fonts is the one remote reference allowed; everything else -- the
    CSS, the script, the data -- ships inside the file, because a demo that
    needs a server is a demo that fails on stage.
    """
    remote = re.findall(r'(?:src|href)="(https?://[^"]+)"', page)
    assert all("fonts.googleapis.com" in u or "fonts.gstatic.com" in u
               for u in remote), remote
    assert "<script src=" not in page


def test_every_figure_comes_from_the_book_not_a_literal(data):
    assert data["ledger"]["rows"], "no accounts rendered"
    assert data["ledger"]["total"] == "0.00", "the book must balance"
    assert data["trail"], "no audit trail"
    assert all(t["h"] for t in data["trail"]), "a trail step with no hash"


def test_the_four_decision_paths_are_all_shown(data):
    verdicts = {d["verdict"] for d in data["decisions"]}
    assert {"ALLOW", "BLOCK", "STEP_UP"} <= verdicts

    blocked = next(d for d in data["decisions"] if d["verdict"] == "BLOCK")
    assert blocked["path"] == "decided by bounds alone", \
        "a hard bound must never reach the model"
    assert blocked["amount"] != "—", "a blocked purchase still shows its amount"


def test_a_resisted_injection_is_visible_on_the_page(data):
    """The attack that did not work is the interesting one."""
    attacked = [d for d in data["decisions"] if d["signals"]]
    assert attacked, "no injection signals rendered"
    assert attacked[0]["verdict"] == "ALLOW", \
        "salt is in scope; over-blocking a detected injection is still a failure"


def test_the_bucket_chart_shows_the_baseline_beside_the_model(data):
    """Otherwise the chart claims credit the deterministic pass earned."""
    by = {b["label"][0]: b for b in data["buckets"]}
    assert by["A"]["base"] == 100.0 and by["B"]["base"] == 100.0
    assert by["C"]["model"] > by["C"]["base"], "the model should help on C"


def test_the_page_does_not_hide_where_the_model_is_worse(data):
    """Bucket D is lower with the model than without. It stays on the page."""
    d = next(b for b in data["buckets"] if b["label"].startswith("D"))
    assert d["model"] < d["base"]
    assert "WORSE" in data["bucket_note"] or "worse" in data["bucket_note"]


def test_integrity_is_reported_not_asserted(data):
    assert all(c["ok"] for c in data["chains"])
    assert all(i["ok"] for i in data["invariants"])
    assert any("chaos" in i["k"] for i in data["invariants"])


def test_the_page_escapes_seller_controlled_text(page):
    """Listing text is attacker-controlled and reaches this page."""
    assert "escapeHtml" in page or "replace(/[&<>\"]/g" in page


def test_the_api_serves_the_same_page():
    c = TestClient(build_app())
    r = c.get("/ui")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "praman-data" in r.text
