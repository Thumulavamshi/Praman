"""Loading the catalog and merchant register.

The register is not decoration. ``familiarity`` is the only evidence that exists
for a mandate saying "my usual stores", and it comes from the merchant record
rather than from anything the seller writes -- which is exactly why it can be
trusted and the seller's store name cannot.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@lru_cache(maxsize=1)
def load_catalog() -> list[dict]:
    return json.loads((DATA_DIR / "catalog.json").read_text())["products"]


@lru_cache(maxsize=1)
def load_merchants() -> dict[str, dict]:
    ms = json.loads((DATA_DIR / "merchants.json").read_text())["merchants"]
    return {m["merchant_id"]: m for m in ms}


@lru_cache(maxsize=1)
def by_sku() -> dict[str, dict]:
    return {p["sku"]: p for p in load_catalog()}


def product(sku: str) -> dict:
    try:
        return by_sku()[sku]
    except KeyError:
        raise KeyError(f"{sku} is not in the catalog")


def merchant(merchant_id: str) -> dict:
    return load_merchants().get(merchant_id, {
        "merchant_id": merchant_id, "name": merchant_id,
        "familiarity": "unknown", "onboarded": ""})


def ambiguous_skus() -> list[str]:
    return [p["sku"] for p in load_catalog() if p.get("ambiguity")]


def clean_skus(category: str | None = None) -> list[str]:
    return [p["sku"] for p in load_catalog()
            if not p.get("ambiguity") and (category is None or p["category"] == category)]
