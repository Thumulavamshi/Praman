"""Phase 7: render the audit trail to a single self-contained page."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from praman.ui.build import build

out = ROOT / "out" / "praman_audit.html"
out.parent.mkdir(exist_ok=True)
html = build()
out.write_text(html, encoding="utf-8")
print(f"wrote {out.relative_to(ROOT)}  ({len(html):,} bytes)")
print(f"open it directly, or serve the same page at /ui")
