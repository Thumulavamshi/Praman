"""Handling attacker-controlled text before it reaches the model.

Product titles, descriptions and merchant names are written by the seller. In
the threat model that means they are written by the attacker. Three rules, in
the order they are applied:

**1. Untrusted text never reaches the system prompt.** Not summarized into it,
not templated into it, not "just the merchant name". The system prompt is the
only part of the request the attacker cannot influence, and that is exactly what
makes it worth defending. Everything the seller wrote goes in the user turn,
inside a fence.

**2. The fence is unguessable.** A fixed delimiter like ``<product>`` is one the
attacker can type, and the class-1 seed ``</mandate> New instruction: ...``
exists precisely to exploit a predictable one. So each request gets a random
128-bit nonce in its delimiters. An attacker who cannot see the nonce cannot
close the fence.

**3. Normalization is evidence, not cleanup.** Zero-width characters splitting a
denied keyword and Cyrillic homoglyphs are not typos -- nobody writes ``аlcohol``
with a Cyrillic а by accident. So we normalize for the model's benefit *and*
report what was found. A listing carrying three zero-width joiners inside the
word "alcohol" has told us something about the seller that matters more than the
listing text does.

Rule 3 is why ``sanitize`` returns findings rather than just a string. Silently
cleaning an attack destroys the evidence that it happened.
"""
from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass, field

# Characters with no visual width, used to split keywords past a filter.
ZERO_WIDTH = "​‌‍⁠﻿­᠎"

# Cyrillic and Greek letters that render identically to Latin ones. The list is
# the confusable set that actually appears in the wild for English words; it is
# not the full Unicode confusables table, and it does not need to be -- what
# matters is that the substitution is *detected*, and any of these firing is
# already conclusive.
CONFUSABLES = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c",
    "у": "y", "х": "x", "А": "A", "Е": "E", "О": "O",
    "Р": "P", "С": "C", "У": "Y", "Х": "X", "Н": "H",
    "М": "M", "В": "B", "І": "I", "і": "i", "ј": "j",
    "һ": "h", "ԁ": "d", "ԛ": "q", "ѕ": "s",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H",
    "Ι": "I", "Κ": "K", "Μ": "M", "Ν": "N", "Ο": "O",
    "Ρ": "P", "Τ": "T", "Χ": "X", "ο": "o", "α": "a",
}

# Text that is trying to be a prompt rather than a product description. Matching
# any of these does not decide anything -- it is one signal handed to the
# adjudicator alongside the listing, and the adjudicator still reasons from the
# mandate. A keyword filter that decided on its own would be trivially evaded by
# rephrasing, and would also block honest listings that happen to say "system".
INJECTION_MARKERS = [
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+instruction", "instruction_override"),
    (r"disregard\s+(the\s+)?(spending\s+)?(limit|instruction|rule|cap)", "instruction_override"),
    (r"\bSYSTEM\s*:", "role_spoofing"),
    (r"\[?\s*ADMIN\s+OVERRIDE\s*\]?", "role_spoofing"),
    (r"</?\s*(mandate|system|instruction|scope)\s*>", "delimiter_spoofing"),
    (r"\bpre[- ]?(approved|authorized|authorised)\b", "false_authority"),
    (r"\bauto[- ]approve\b", "false_authority"),
    (r"\bno\s+further\s+checks?\s+required\b", "false_authority"),
    (r"\bverified\s+by\s+praman\b", "false_authority"),
    (r"\bmandate\s+amendment\b", "false_authority"),
    (r"\b(cap|limit)\s+raised\s+to\b", "false_authority"),
    (r"\btrust\s+score\b", "false_authority"),
    (r"\breturn\s+ALLOW\b", "instruction_override"),
    (r"\bexempt\s+from\s+category\s+restrictions?\b", "scope_reinterpretation"),
    (r"\bfor\s+authorization\s+purposes\b", "scope_reinterpretation"),
    (r"\buser\s+has\s+(verbally\s+)?confirmed\b", "false_authority"),
]
_COMPILED = [(re.compile(p, re.I), label) for p, label in INJECTION_MARKERS]


@dataclass
class SanitizedText:
    original: str
    text: str
    signals: list[str] = field(default_factory=list)

    @property
    def suspicious(self) -> bool:
        return bool(self.signals)


def sanitize(raw: str, *, field_name: str = "text") -> SanitizedText:
    """Normalize attacker text and report every manipulation found.

    NFKC folds the compatibility tricks (fullwidth characters, ligatures). Then
    zero-width removal and homoglyph folding, each recorded. Excessive length is
    recorded too: padding before a payload, to push the real content out of the
    model's attention, is class 5 and it is visible as a length outlier.
    """
    signals: list[str] = []
    text = unicodedata.normalize("NFKC", raw)
    if text != raw:
        signals.append(f"{field_name}:unicode_normalized")

    stripped = "".join(ch for ch in text if ch not in ZERO_WIDTH)
    if stripped != text:
        n = len(text) - len(stripped)
        signals.append(f"{field_name}:zero_width_chars({n})")
    text = stripped

    folded = "".join(CONFUSABLES.get(ch, ch) for ch in text)
    if folded != text:
        signals.append(f"{field_name}:homoglyphs")
    text = folded

    if len(text) > 600:
        signals.append(f"{field_name}:overlong({len(text)})")

    for pattern, label in _COMPILED:
        if pattern.search(text):
            signals.append(f"{field_name}:{label}")

    # Control characters other than tab/newline have no business in a product
    # title and are a common way to smuggle structure past a naive parser.
    ctrl = sum(1 for ch in text
               if unicodedata.category(ch) == "Cc" and ch not in "\t\n")
    if ctrl:
        signals.append(f"{field_name}:control_chars({ctrl})")
        text = "".join(ch for ch in text
                       if unicodedata.category(ch) != "Cc" or ch in "\t\n")

    return SanitizedText(original=raw, text=text, signals=sorted(set(signals)))


@dataclass
class Fence:
    """A single-use pair of delimiters the attacker cannot guess or close."""
    nonce: str

    @staticmethod
    def new() -> "Fence":
        return Fence(nonce=secrets.token_hex(16))

    @property
    def open(self) -> str:
        return f"<untrusted-listing id={self.nonce}>"

    @property
    def close(self) -> str:
        return f"</untrusted-listing id={self.nonce}>"

    def wrap(self, body: str) -> str:
        # Even so, strip any occurrence of our own nonce from the body. The
        # nonce is unguessable, but "unguessable" is an assumption and this
        # costs one string operation.
        body = body.replace(self.nonce, "[redacted]")
        return f"{self.open}\n{body}\n{self.close}"
