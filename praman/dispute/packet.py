"""The representment packet, and the verifier that can refuse it.

The packet is what a merchant sends an issuer. Its one non-negotiable property:

    Every factual claim quotes a record, and the quoted value is actually in
    that record.

An LLM writing a dispute letter will, unprompted, produce a fluent and
completely plausible number. In a representment that is not a style problem --
it is a merchant asserting something to an issuer that their own books do not
support, which is worse than filing nothing. So the packet is not trusted
because the model was careful. It is trusted because ``verify_citations``
re-reads every claim against the record it cites and refuses the ones that do
not check out.

This is the same principle as the ledger's invariant suite, applied one layer
up: **the verifier outranks the model.** A packet whose claims fail is not
quietly cleaned up and sent -- it is returned with the failures attached, and
demonstrating that rejection is worth more than a run where nothing goes wrong.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field


class Claim(BaseModel):
    """One factual assertion, and the record it is lifted from."""

    statement: str = Field(
        description="The assertion, in a sentence an issuer's dispute analyst "
                    "can read. No jargon, no model-speak.")
    value: str = Field(
        description="The exact value this claim rests on, copied verbatim from "
                    "the tool output -- an amount like '357.00', a timestamp, an "
                    "id, or a verdict like 'ALLOW'. It must appear in the cited "
                    "record character for character.")
    source_kind: Literal["event", "decision", "mandate", "ledger", "integrity"]
    source_id: str = Field(
        description="The id of the record the value came from: an event_id, a "
                    "decision_id, or a mandate_id, exactly as the tool returned it.")


class RepresentmentPacket(BaseModel):
    payment_id: str
    chargeback_id: str
    summary: str = Field(
        description="Two or three sentences: what was bought, under whose "
                    "authority, and what state the record is in. Neutral -- do "
                    "NOT assert the merchant is entitled to the money here. "
                    "That is a conclusion, it is decided in "
                    "recommended_action below, and a summary that presumes it "
                    "will contradict a packet that escalates.")
    claims: list[Claim] = Field(
        description="Every fact the argument rests on, each citing its record. "
                    "Six to twelve. Do not assert anything you did not read "
                    "from a tool.")
    # recommended_action comes BEFORE argument, deliberately. Structured output
    # is generated in schema order, so putting the conclusion first makes the
    # prose follow from it. With argument first, the model wrote a confident
    # case for representment and then recommended escalate -- a packet whose
    # own two halves disagreed, which is exactly what an issuer would seize on.
    recommended_action: Literal["represent", "accept_liability", "escalate"] = Field(
        description="Decide this FIRST, before writing the argument. "
                    "represent if the evidence supports defending the charge; "
                    "accept_liability if it does not and the merchant should "
                    "take the loss; escalate if the record is incomplete, "
                    "unverifiable or self-contradictory and a human must look.")
    argument: str = Field(
        description="The case, addressed to the issuer, and it MUST follow the "
                    "recommendation you just made. If representing: answer the "
                    "cardholder's actual claim -- that they did not authorise "
                    "the purchase, their agent did -- rather than asserting "
                    "they did. If escalating or accepting liability: say what "
                    "is wrong with the record and why the merchant cannot rely "
                    "on it. Do not argue for representment in a packet that "
                    "does not recommend it.")
    weaknesses: list[str] = Field(
        description="Anything in this record that a competent issuer would "
                    "attack. State them. A packet that hides its weak points "
                    "loses credibility on the first one the issuer finds.")


@dataclass
class CitationFailure:
    claim: str
    value: str
    source_id: str
    problem: str

    def __str__(self) -> str:
        return f"{self.problem}: \"{self.value}\" cited to {self.source_id}"


@dataclass
class VerifiedPacket:
    packet: RepresentmentPacket
    verified: list[Claim] = field(default_factory=list)
    failures: list[CitationFailure] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        """A packet with any unverifiable claim is refused outright.

        Not "mostly accurate" -- refused. A single fabricated figure in a
        representment is the thing that loses the argument and the credibility
        together, and there is no threshold at which some are acceptable.
        """
        return not self.failures

    @property
    def citation_accuracy(self) -> float:
        total = len(self.verified) + len(self.failures)
        return len(self.verified) / total if total else 0.0

    def render(self) -> str:
        p = self.packet
        lines = [
            "REPRESENTMENT PACKET" + ("" if self.accepted else "  — REFUSED BY VERIFIER"),
            "=" * 74,
            f"payment            {p.payment_id}",
            f"chargeback         {p.chargeback_id}",
            f"recommended        {p.recommended_action.upper()}",
            f"citations verified {len(self.verified)}/"
            f"{len(self.verified) + len(self.failures)}"
            f"  ({self.citation_accuracy * 100:.0f}%)",
            "",
            "SUMMARY", "-" * 74, p.summary, "",
            "FACTS, EACH CHECKED AGAINST THE RECORD IT CITES", "-" * 74,
        ]
        for c in self.verified:
            lines.append(f"  [ok] {c.statement}")
            lines.append(f"       {c.value}   [{c.source_kind} {c.source_id}]")
        for f in self.failures:
            lines.append(f"  [REFUSED] {f.claim}")
            lines.append(f"       {f}")
        lines += ["", "ARGUMENT", "-" * 74, p.argument]
        if p.weaknesses:
            lines += ["", "WEAKNESSES AN ISSUER WILL ATTACK", "-" * 74]
            lines += [f"  - {w}" for w in p.weaknesses]
        if self.failures:
            lines += ["", "WHY THIS PACKET WAS REFUSED", "-" * 74,
                      "  Claims below could not be matched to the record they cite.",
                      "  A representment may not assert a figure the merchant's own",
                      "  books do not support, so the packet does not go out."]
        return "\n".join(lines)


def _normalise(s: str) -> str:
    """Compare values the way a reader would, not byte for byte.

    'INR 357.00', '357.00' and '357' are the same claim about the same money.
    Currency words, symbols, commas and case are noise; the digits and letters
    are the assertion. Trailing zeros on a decimal are also noise, so 357.00
    matches 357 -- but 357.10 must never match 357.
    """
    s = str(s).strip().lower()
    s = re.sub(r"\b(inr|rs\.?|₹)\b", "", s)
    s = s.replace("₹", "").replace(",", "")
    s = re.sub(r"[\s]+", "", s)
    m = re.fullmatch(r"(-?\d+)\.(\d*?)0*", s)
    if m:
        frac = m.group(2)
        return f"{m.group(1)}.{frac}" if frac else m.group(1)
    return s


def verify_citations(packet: RepresentmentPacket,
                     records: dict[str, dict]) -> VerifiedPacket:
    """Re-read every claim against the record it cites.

    ``records`` maps source_id -> the tool output it came from. A claim passes
    only if its ``value`` appears somewhere in that record's values. Anything
    else -- a citation to a record that was never fetched, or a value that is
    not in the record it names -- is a failure.
    """
    out = VerifiedPacket(packet=packet)
    for c in packet.claims:
        rec = records.get(c.source_id)
        if rec is None:
            out.failures.append(CitationFailure(
                c.statement, c.value, c.source_id,
                "cites a record the investigation never retrieved"))
            continue
        if _value_in(c.value, rec):
            out.verified.append(c)
        else:
            out.failures.append(CitationFailure(
                c.statement, c.value, c.source_id,
                "value does not appear in the cited record"))
    return out


def _value_in(value: str, record) -> bool:
    target = _normalise(value)
    if not target:
        return False
    for v in _walk(record):
        if _normalise(v) == target:
            return True
    # A free-text field (a reason, a delegation quote) may legitimately contain
    # the value as a substring rather than as its whole content.
    for v in _walk(record):
        s = str(v)
        if len(s) > 24 and target and target in _normalise(s):
            return True
    return False


def _walk(obj):
    if isinstance(obj, dict):
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _walk(v)
    elif obj is not None:
        yield obj
