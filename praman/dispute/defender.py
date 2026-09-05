"""The dispute defender: an agent that investigates a chargeback.

    "I didn't authorize that, my agent did."

The claim is not a lie, and that is what makes it hard. The consumer really did
delegate purchasing authority and really does retain chargeback rights. So the
defence cannot be *"yes you did"*. It has to be:

    Here is what you delegated, in your own words, signed. Here is what your
    agent proposed. Here is what the gate decided and the clause it enforced.
    Here is the money. Here is a hash chain showing none of it was written
    after you filed.

The agent's job is to assemble that from the record. It is given five read-only
tools and no way to write anything.

**Why the loop is worth having at all.** ``evidence/chain.py`` already builds a
bundle deterministically, and for a complete record that is enough. What the
agent adds is judgment on the *incomplete* ones: noticing that the mandate was
revoked eleven minutes before the capture, that the gate stepped up and no human
ever approved it, that the seller's listing carried an injection. Those are
readings of the evidence, not fields in it -- and the honest answer is sometimes
``accept_liability``, which a template cannot produce.

**Why it is safe to let a model write a legal-ish document.** It is not trusted.
Every claim it makes is re-read against the record it cites by
``verify_citations``, and a packet with a single unverifiable figure is refused
rather than tidied up. The model can be as fluent as it likes; it cannot get a
number past the verifier that the books do not contain.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from ..keyring import GeminiKeyRing, call_with_rotation
from .packet import RepresentmentPacket, VerifiedPacket, verify_citations
from .tools import DisputeTools

# The defender's own model setting, NOT the adjudicator's. They are different
# jobs with different budgets: the adjudicator runs hundreds of times per
# evaluation, the defender a handful of times per dispute. Falling back to the
# adjudicator's model means an investigation dies on quota the adjudicator
# already spent -- which is exactly what happened the first time this ran.
DEFAULT_MODEL = os.getenv("PRAMAN_DISPUTE_MODEL", "gemini-3-flash-preview")

SYSTEM_PROMPT = """\
You are preparing a chargeback representment for a merchant. The cardholder has \
disputed a payment their AI shopping agent made, saying they did not authorise \
it.

Take that claim seriously. It is usually TRUE in the narrow sense: the person \
did not personally click buy. They delegated authority to an agent, and they \
keep their chargeback rights either way. So "the cardholder authorised this" is \
not your argument and will not survive an issuer reading it. Your argument is \
that the purchase fell within authority the cardholder themselves delegated, \
that the delegation was in force, and that the merchant can show it.

HOW TO WORK

Use the tools to read the record. Start with the mandate and the gate decision, \
then the cart and the money, and check integrity before you conclude. Call \
every tool at least once -- a packet that did not check whether the hash chain \
verifies is a packet that missed the most important thing an issuer can attack.

Then produce the packet.

THE ONE RULE THAT MATTERS

Every claim you make must quote a value you actually read from a tool, and must \
name the record it came from. Copy the value exactly as the tool returned it. \
Do not round it, reformat it, convert it, or infer it from two other values.

Every claim is machine-checked against the record it cites before this packet \
goes anywhere. A claim whose value is not in the cited record is refused, and \
one refused claim refuses the whole packet. You cannot help the merchant by \
writing a stronger number than the books contain -- you can only get the packet \
thrown out.

If the evidence does not support defending the charge, say so and recommend \
accept_liability. A merchant who files a losing representment pays the fee and \
loses anyway. If the record is incomplete or contradicts itself, recommend \
escalate. Those are correct answers, not failures.

List the weaknesses honestly. An issuer will find them; a packet that names \
them first reads as candid rather than caught out.\
"""


@dataclass
class DefenceResult:
    payment_id: str
    chargeback_id: str
    verified: VerifiedPacket | None = None
    tool_calls: list = field(default_factory=list)
    records_seen: dict = field(default_factory=dict)
    latency_ms: float = 0.0
    model: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.verified is not None and self.verified.accepted

    def render(self) -> str:
        if self.verified is None:
            return (f"no packet produced for {self.payment_id}: "
                    f"{self.error or 'unknown error'}")
        head = (f"investigation: {len(self.tool_calls)} tool calls, "
                f"{self.latency_ms / 1000:.1f}s, {self.model}\n")
        return head + self.verified.render()


class DisputeDefender:
    """Investigates one chargeback and returns a verified packet."""

    def __init__(self, engine, gate, *, ring: GeminiKeyRing | None = None,
                 model: str = DEFAULT_MODEL, max_tool_turns: int = 8):
        self.engine = engine
        self.gate = gate
        self.model = model
        self.max_tool_turns = max_tool_turns
        self.ring = ring or GeminiKeyRing(
            rpm_per_key=float(os.getenv("PRAMAN_GEMINI_RPM", "10")), model=model)

    def defend(self, payment_id: str, chargeback_id: str = "") -> DefenceResult:
        """Investigate and produce a verified packet.

        The tool loop is driven HERE rather than by the SDK's automatic function
        calling, and that is not a stylistic preference. Under AFC one
        ``send_message`` becomes six or more HTTP requests inside the SDK, all
        of them on the one key the client was built with and none of them
        visible to our rate limiter. On a 5-requests-per-minute free tier that
        blows the limit on a single investigation -- observed, not theorised.

        Driving it manually means every request goes through the ring: paced,
        and landing on a different key each turn. It also gives us the tool
        trail for free, which the packet's audit needs anyway.
        """
        from google.genai import types

        tools = DisputeTools(self.engine, self.gate)
        result = DefenceResult(payment_id=payment_id, chargeback_id=chargeback_id,
                               model=self.model)
        started = time.perf_counter()

        if self.ring.live == 0:
            result.error = (f"no key in the pool has budget left for "
                            f"{self.model} today")
            return result

        cb = self.engine.state.chargebacks.get(chargeback_id)
        opening = (
            f"Chargeback {chargeback_id or '(unfiled)'} has been raised against "
            f"payment {payment_id}"
            + (f" for INR {cb.amount} under reason code {cb.reason_code}"
               if cb else "")
            + ". The cardholder says they did not authorise it — that their AI "
              "agent made the purchase.\n\nInvestigate the record and prepare "
              "the representment."
        )

        by_name = {f.__name__: f for f in tools.as_list()}
        # from_callable() wants a live client just to read one flag off it;
        # from_callable_with_api_option() takes the flag directly, which is what
        # we want here because the declarations are built once and reused across
        # every key in the ring.
        declared = types.Tool(function_declarations=[
            types.FunctionDeclaration.from_callable_with_api_option(
                callable=f, api_option="GEMINI_API")
            for f in tools.as_list()])

        contents = [types.Content(role="user",
                                  parts=[types.Part(text=opening)])]
        investigate_cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            tools=[declared],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True),
            max_output_tokens=2048)

        try:
            for _ in range(self.max_tool_turns):
                resp = call_with_rotation(
                    self.ring,
                    lambda c, _cts=list(contents): c.models.generate_content(
                        model=self.model, contents=_cts, config=investigate_cfg),
                    max_attempts=3)
                cand = (resp.candidates or [None])[0]
                if cand is None or not cand.content:
                    break
                contents.append(cand.content)
                calls = [p.function_call for p in (cand.content.parts or [])
                         if getattr(p, "function_call", None)]
                if not calls:
                    break
                replies = []
                for fc in calls:
                    fn = by_name.get(fc.name)
                    args = dict(fc.args or {})
                    out = ({"error": f"no such tool {fc.name}"} if fn is None
                           else _safe(fn, args))
                    replies.append(types.Part.from_function_response(
                        name=fc.name, response={"result": out}))
                contents.append(types.Content(role="user", parts=replies))

            # The packet is asked for from a COMPACT DIGEST of what the
            # investigation retrieved, not from the whole conversation.
            #
            # Resending the full transcript means every tool payload goes back
            # over the wire, and that single large slow request was reliably
            # drawing 503s and tunnel closures. The digest carries the same
            # facts in a fraction of the bytes, and it is strictly better for
            # correctness too: the model writes the packet from the records the
            # verifier will check it against, rather than from a conversation
            # that also contains its own earlier speculation.
            records = self._records_fetched(tools)
            digest = json.dumps(records, indent=1, default=str, sort_keys=True)
            contents = [types.Content(role="user", parts=[types.Part(text=(
                f"{opening}\n\nThe investigation retrieved these records. "
                f"Every claim you make must quote a value from them and name "
                f"the record's id:\n\n{digest}\n\n"
                f"Produce the representment packet."))])]
            # Streamed, deliberately. The final request carries the whole
            # investigation -- five tool payloads -- and asks for a long
            # structured answer, so it is a single slow exchange. Observed: the
            # egress tunnel closing mid-exchange before the first byte came
            # back. Streaming keeps bytes moving and the connection alive.
            # thinking_budget=0 on purpose. Gemini counts reasoning tokens
            # against max_output_tokens, and this call is serialisation, not
            # judgment -- the model already reasoned over the evidence during
            # the investigation. Leaving thinking on spent the budget before the
            # JSON was finished and returned a packet truncated mid-string at
            # 578 characters, which surfaces as an opaque pydantic parse error.
            packet_cfg = types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                response_schema=RepresentmentPacket,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
                max_output_tokens=8192)

            def emit(c):
                chunks, finish = [], None
                for chunk in c.models.generate_content_stream(
                        model=self.model, contents=contents, config=packet_cfg):
                    if getattr(chunk, "text", None):
                        chunks.append(chunk.text)
                    for cand in (getattr(chunk, "candidates", None) or []):
                        if getattr(cand, "finish_reason", None):
                            finish = str(cand.finish_reason)
                raw = "".join(chunks)
                # Say what actually went wrong. A truncated packet is a budget
                # problem, and reporting it as a JSON syntax error sends the
                # reader looking in entirely the wrong place.
                if finish and "MAX_TOKENS" in finish.upper():
                    raise ValueError(
                        f"the packet was cut off by max_output_tokens after "
                        f"{len(raw)} characters; raise it or shorten the digest")
                return raw

            raw = call_with_rotation(self.ring, emit, max_attempts=5)
            packet = RepresentmentPacket.model_validate_json(raw.strip())
        except Exception as exc:                        # noqa: BLE001
            result.latency_ms = (time.perf_counter() - started) * 1000
            result.tool_calls = list(tools.calls)
            result.error = f"{exc.__class__.__name__}: {exc}"
            return result

        result.latency_ms = (time.perf_counter() - started) * 1000
        result.tool_calls = list(tools.calls)

        # Only records the investigation actually fetched are admissible. A
        # citation to something never retrieved is a fabricated citation.
        result.records_seen = records
        result.verified = verify_citations(packet, records)
        return result

    def _records_fetched(self, tools: DisputeTools) -> dict[str, dict]:
        """The records the investigation actually saw, read from the trail.

        Read, not re-run. Re-running each tool to rebuild this would append to
        the very list being iterated -- an infinite loop, found by test -- and
        would also let the ledger move underneath, so the verifier would check
        claims against records the model never saw.
        """
        out: dict[str, dict] = {}
        for call in list(tools.calls):
            rec = call.get("result")
            if not isinstance(rec, dict):
                continue
            sid = rec.get("source_id")
            if sid:
                out[sid] = rec
            # A money_trail also licenses citations to the individual events it
            # names, since it returned them.
            for eid in rec.get("events_touching_this_payment", []) or []:
                out.setdefault(eid, rec)
        return out


def _safe(fn, args: dict):
    """Run a tool, returning its error rather than killing the investigation."""
    try:
        return fn(**args)
    except Exception as exc:                            # noqa: BLE001
        return {"error": f"{exc.__class__.__name__}: {exc}"}


def _parsed(resp) -> RepresentmentPacket:
    parsed = getattr(resp, "parsed", None)
    if isinstance(parsed, RepresentmentPacket):
        return parsed
    if isinstance(parsed, dict):
        return RepresentmentPacket.model_validate(parsed)
    text = (getattr(resp, "text", "") or "").strip()
    if not text:
        raise ValueError("the model returned no packet (likely truncated)")
    return RepresentmentPacket.model_validate_json(text)
