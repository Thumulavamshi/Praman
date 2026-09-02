import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def catalog():
    return json.loads((ROOT / "data" / "catalog.json").read_text())["products"]


@pytest.fixture(scope="session")
def merchants():
    return json.loads((ROOT / "data" / "merchants.json").read_text())["merchants"]
