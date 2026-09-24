"""The evaluation case: a mandate, a proposal, and the answer a human would give.

A case is stored as a *recipe* rather than a materialized proposal -- a mandate
variant name, a list of SKUs, and any overrides. Two reasons:

  * A case stays readable. Someone checking a label can see "GOU-0001 saffron,
    under the staples_only mandate, expected BLOCK" and disagree with it. A
    materialized 40-line JSON blob is not reviewable, and an unreviewable label
    is not a hand label.
  * Injections composite onto clean products at build time, so the same attack
    runs against many products and the clean verdict is always known.

``expected`` is the ground truth. For bucket D it is defined as *the verdict the
gate reaches on the clean listing* -- an attack succeeds when it changes the
answer, not when the answer is ALLOW. That definition is what makes the
injection-resistance number mean something.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from praman.ledger.money import money, money_str
from praman.mandate.schema import CartItem, Mandate, Proposal
from .catalog import merchant, product
from .mandates import AGENT, variant

Bucket = Literal["A", "B", "C", "D"]
Expected = Literal["ALLOW", "BLOCK", "STEP_UP"]

# Default proposal time: a Thursday evening, comfortably inside the reference
# mandate's window. Anything testing a time bound overrides it explicitly --
# a case that depends on the wall clock is a case that fails at 3am.
DEFAULT_AT = "2026-09-03T19:20:00+05:30"


@dataclass
class Attack:
    """One injected string, and where it goes."""
    attack_class: int
    label: str
    text: str
    field: Literal["name", "description", "merchant_name"] = "description"
    mode: Literal["append", "replace"] = "append"


@dataclass
class EvalCase:
    case_id: str
    bucket: Bucket
    mandate_variant: str
    skus: list[str]
    expected: Expected
    rationale: str
    at: str = DEFAULT_AT
    quantities: list[int] = field(default_factory=list)
    amount_override: str | None = None
    merchant_override: str | None = None
    familiarity_override: str | None = None
    history: list[tuple[str, str]] = field(default_factory=list)
    revoked: bool = False
    attack: Attack | None = None
    clean_of: str = ""
    source: Literal["authored", "generated"] = "generated"
    heldout: bool = False

    # -- materialization -----------------------------------------------------

    def mandate(self) -> Mandate:
        return variant(self.mandate_variant)

    def proposal(self) -> Proposal:
        prods = [product(s) for s in self.skus]
        qtys = self.quantities or [1] * len(prods)
        mid = self.merchant_override or prods[0]["merchant_id"]
        mrec = merchant(mid)

        items = []
        for p, q in zip(prods, qtys):
            name, desc = p["name"], ""
            if self.attack and self.attack.field in ("name", "description"):
                if self.attack.field == "name":
                    name = (f"{name} {self.attack.text}"
                            if self.attack.mode == "append" else self.attack.text)
                else:
                    desc = (f"{desc} {self.attack.text}".strip()
                            if self.attack.mode == "append" else self.attack.text)
            items.append(CartItem(
                sku=p["sku"], name=name, category=p["category"],
                subcategory=p.get("subcategory", ""), merchant_id=mid,
                merchant_name=mrec.get("name", mid), price=p["price"],
                quantity=q, description=desc))

        mname = mrec.get("name", mid)
        if self.attack and self.attack.field == "merchant_name":
            mname = (f"{mname} {self.attack.text}"
                     if self.attack.mode == "append" else self.attack.text)

        total = self.amount_override or money_str(
            money(sum((i.line_total for i in items), Decimal("0"))))

        return Proposal(
            proposal_id=f"prop_{self.case_id}",
            mandate_id=self.mandate().mandate_id,
            agent=AGENT,
            items=items,
            merchant_id=mid,
            merchant_name=mname,
            merchant_familiarity=(self.familiarity_override
                                  or mrec.get("familiarity", "unknown")),
            merchant_onboarded=mrec.get("onboarded", ""),
            proposed_at=self.at,
            amount=total,
        )

    def spend_history(self):
        from datetime import datetime

        from praman.gate.bounds import SpendHistory
        return SpendHistory([(datetime.fromisoformat(t), money(a))
                             for t, a in self.history])

    # -- serialization -------------------------------------------------------

    def to_dict(self) -> dict:
        d = {
            "case_id": self.case_id, "bucket": self.bucket,
            "mandate_variant": self.mandate_variant, "skus": self.skus,
            "expected": self.expected, "rationale": self.rationale, "at": self.at,
            "quantities": self.quantities,
            "amount_override": self.amount_override,
            "merchant_override": self.merchant_override,
            "familiarity_override": self.familiarity_override,
            "history": [list(h) for h in self.history],
            "revoked": self.revoked, "clean_of": self.clean_of,
            "source": self.source, "heldout": self.heldout,
        }
        if self.attack:
            d["attack"] = {
                "attack_class": self.attack.attack_class,
                "label": self.attack.label, "text": self.attack.text,
                "field": self.attack.field, "mode": self.attack.mode,
            }
        return d

    @staticmethod
    def from_dict(d: dict) -> "EvalCase":
        a = d.get("attack")
        return EvalCase(
            case_id=d["case_id"], bucket=d["bucket"],
            mandate_variant=d["mandate_variant"], skus=d["skus"],
            expected=d["expected"], rationale=d["rationale"],
            at=d.get("at", DEFAULT_AT), quantities=d.get("quantities") or [],
            amount_override=d.get("amount_override"),
            merchant_override=d.get("merchant_override"),
            familiarity_override=d.get("familiarity_override"),
            history=[tuple(h) for h in d.get("history", [])],
            revoked=d.get("revoked", False),
            attack=Attack(a["attack_class"], a["label"], a["text"],
                          a.get("field", "description"),
                          a.get("mode", "append")) if a else None,
            clean_of=d.get("clean_of", ""), source=d.get("source", "generated"),
            heldout=d.get("heldout", False))
