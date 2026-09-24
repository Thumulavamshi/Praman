"""The deterministic bounds checker. Runs first, and can decide alone.

Architecture point, and it is the one that makes the whole gate defensible:

    Anything mechanically decidable is decided mechanically. The model is
    consulted only where hard bounds pass but semantic scope is in question.

Three things follow from that, and each is a claim we get to make honestly:

  * **Latency.** A clear cap violation is answered in microseconds, with no
    network call. Only the genuinely ambiguous cases pay for a model round-trip,
    and the measured p50 reflects that rather than hiding behind an average.
  * **Cost.** Same argument, in rupees.
  * **Separable failure modes.** When the gate is wrong, it is wrong either in
    the arithmetic or in the judgment, and those are different bugs with
    different fixes. A system that folds them together cannot be debugged, and
    cannot report an honest per-bucket number.

Every amount used here is read from the payment object -- ``proposal.amount``
and the merchant record -- never from product text. That is what makes the
class-6 injections in the red-team set (display price disagreeing with the
charge, a cart total that lies about its line items) structurally impossible
rather than merely resisted. There is no path by which attacker-controlled text
reaches an arithmetic comparison in this file.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..ledger.money import ZERO, money, money_str
from ..mandate.schema import Mandate, Proposal


@dataclass(frozen=True)
class Finding:
    """One bound, and what it said.

    ``clause`` names the mandate field, because a decision that cannot cite the
    clause it enforced is not a defensible decision -- it is an opinion. The
    dispute packet quotes these verbatim.

    ``verdict`` is one of four:

      BLOCK   -- decided, and final. The model is never asked.
      STEP_UP -- the human asked to be consulted at this point.
      REVIEW  -- NOT decided. A signal the adjudicator must weigh. See below.
      PASS    -- the bound was checked and satisfied.
    """
    code: str
    clause: str
    verdict: str              # BLOCK | STEP_UP | REVIEW | PASS
    detail: str
    observed: str = ""
    limit: str = ""

    def __str__(self) -> str:
        return f"{self.clause}: {self.detail}"


@dataclass
class BoundsResult:
    verdict: str                                  # BLOCK | STEP_UP | PASS
    findings: list[Finding] = field(default_factory=list)

    @property
    def blocks(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == "BLOCK"]

    @property
    def step_ups(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == "STEP_UP"]

    @property
    def reviews(self) -> list[Finding]:
        return [f for f in self.findings if f.verdict == "REVIEW"]

    @property
    def decided(self) -> bool:
        """True when the checker has settled it and the model adds nothing.

        A BLOCK is final: no amount of semantic reasoning makes a purchase that
        breaks a hard bound acceptable, and asking the model would only create a
        surface for an injection to talk it out of the answer. This is a
        security property, not an optimization.
        """
        return self.verdict == "BLOCK"


@dataclass
class SpendHistory:
    """Prior settled spend under a mandate, for period-cap and velocity checks.

    Supplied by the caller from the ledger, not recomputed here: the ledger is
    the record of what actually happened, and a checker that kept its own
    parallel tally would be a second source of truth.
    """
    transactions: list[tuple[datetime, Decimal]] = field(default_factory=list)

    def spent_within(self, window_start: datetime) -> Decimal:
        return money(sum((a for t, a in self.transactions if t >= window_start), ZERO))

    def count_within(self, window_start: datetime) -> int:
        return sum(1 for t, _ in self.transactions if t >= window_start)


def _matches(value: str, patterns: list[str]) -> bool:
    return "*" in patterns or value in patterns


def check_bounds(mandate: Mandate, proposal: Proposal,
                 history: SpendHistory | None = None,
                 *, signature_valid: bool | None = None,
                 revoked: bool = False) -> BoundsResult:
    """Run every mechanical check. Returns BLOCK, STEP_UP or PASS with citations.

    Order is deliberate: authority first (is this mandate even usable), then
    scope, then amount, then time. A purchase under a revoked mandate is not
    "over the cap" -- it is unauthorized, and the reason a merchant gives an
    issuer has to be the real one.
    """
    findings: list[Finding] = []
    when = proposal.when

    # --- authority ---------------------------------------------------------
    if signature_valid is False:
        findings.append(Finding(
            "bad_signature", "signature", "BLOCK",
            "mandate signature does not verify; the mandate may have been altered "
            "after issue"))
    if revoked:
        findings.append(Finding(
            "revoked", "mandate_revoked", "BLOCK",
            "mandate was revoked before this purchase was proposed"))
    if proposal.mandate_id != mandate.mandate_id:
        findings.append(Finding(
            "mandate_mismatch", "mandate_id", "BLOCK",
            f"proposal cites mandate {proposal.mandate_id} but was adjudicated "
            f"against {mandate.mandate_id}"))
    if proposal.agent != mandate.agent:
        findings.append(Finding(
            "wrong_agent", "agent", "BLOCK",
            f"mandate delegates to {mandate.agent}, proposal came from "
            f"{proposal.agent}",
            observed=proposal.agent, limit=mandate.agent))
    if not mandate.is_active(when):
        which = "not yet valid" if when < mandate.issued else "expired"
        findings.append(Finding(
            "expired", "expires_at", "BLOCK",
            f"mandate is {which} at {proposal.proposed_at}",
            observed=proposal.proposed_at, limit=mandate.expires_at))

    scope = mandate.scope

    # --- category ----------------------------------------------------------
    # The two category lists are NOT the same kind of thing, and treating them
    # alike was a real bug that measurement caught.
    #
    # `categories_denied` is what the human explicitly refused. "Nothing from
    # liquor stores" is a sentence they said. A denied category is a hard BLOCK,
    # final, and the model is never asked to reconsider it.
    #
    # `categories_allowed` is a COMPILED INFERENCE. The human said "groceries
    # and household stuff"; the compiler mapped that onto a fixed taxonomy. The
    # boundaries of that taxonomy are the taxonomy's, not the human's -- AA
    # batteries bought in a supermarket are `electronics` to the register and
    # "household stuff" to the person who asked for them. Treating a miss as a
    # hard BLOCK silently promotes the taxonomy's category boundaries into
    # commitments the human never made.
    #
    # The evidence that settled it: a mandate whose own source text said
    # "delivery charges are fine" was blocking its own delivery charge, because
    # a delivery fee is registered under `services`. No reading of that
    # delegation supports the block.
    #
    # So an allowed-list miss is a REVIEW: not decided, handed to the
    # adjudicator with the mismatch stated, and the adjudicator reasons about
    # what the person meant. Denied stays hard. This is exactly the line the
    # whole architecture draws -- mechanical where the human was mechanical,
    # semantic where the compiler had to interpret.
    for cat in sorted(proposal.categories()):
        if cat in scope.categories_denied:
            findings.append(Finding(
                "denied_category", "categories_denied", "BLOCK",
                f"cart contains {cat}, which the mandate denies",
                observed=cat, limit=", ".join(scope.categories_denied)))
        elif not _matches(cat, scope.categories_allowed):
            findings.append(Finding(
                "category_not_allowed", "categories_allowed", "REVIEW",
                f"cart contains {cat}, which is not in the compiled allowed list "
                f"({', '.join(scope.categories_allowed)}). The allowed list was "
                f"inferred from the delegation, so this is a mismatch to weigh, "
                f"not a rule the human wrote",
                observed=cat, limit=", ".join(scope.categories_allowed)))

    # --- merchant ----------------------------------------------------------
    if proposal.merchant_id in scope.merchants_denied:
        findings.append(Finding(
            "denied_merchant", "merchants_denied", "BLOCK",
            f"{proposal.merchant_id} is on the mandate's denied list",
            observed=proposal.merchant_id))
    elif not _matches(proposal.merchant_id, scope.merchants_allowed):
        findings.append(Finding(
            "merchant_not_allowed", "merchants_allowed", "BLOCK",
            f"{proposal.merchant_id} is not in the allowed merchant list",
            observed=proposal.merchant_id,
            limit=", ".join(scope.merchants_allowed)))

    # --- amount ------------------------------------------------------------
    total = proposal.total
    if total != proposal.items_total():
        # Class-6 red team: a cart total that disagrees with its own lines. The
        # charge is authoritative, and the disagreement itself is the signal.
        findings.append(Finding(
            "total_mismatch", "amount", "BLOCK",
            f"charge of {money_str(total)} does not equal the sum of line items "
            f"({money_str(proposal.items_total())})",
            observed=money_str(total), limit=money_str(proposal.items_total())))

    if scope.cap is not None and total > scope.cap:
        findings.append(Finding(
            "over_cap", "per_transaction_cap", "BLOCK",
            f"{money_str(total)} exceeds the per-transaction cap of "
            f"{money_str(scope.cap)}",
            observed=money_str(total), limit=money_str(scope.cap)))

    if history is not None and scope.period_cap is not None:
        start = when - scope.period_cap.delta
        spent = history.spent_within(start)
        if money(spent + total) > scope.period_cap.as_decimal:
            findings.append(Finding(
                "over_period_cap", "period_cap", "BLOCK",
                f"{money_str(total)} would take {scope.period_cap.window} spend to "
                f"{money_str(spent + total)}, over the cap of "
                f"{scope.period_cap.amount}",
                observed=money_str(spent + total), limit=scope.period_cap.amount))

    if history is not None and scope.velocity is not None:
        start = when - scope.velocity.delta
        n = history.count_within(start)
        if n + 1 > scope.velocity.max_txns:
            findings.append(Finding(
                "over_velocity", "velocity", "BLOCK",
                f"this would be transaction {n + 1} in {scope.velocity.window}, "
                f"over the limit of {scope.velocity.max_txns}",
                observed=str(n + 1), limit=str(scope.velocity.max_txns)))

    # --- time of day -------------------------------------------------------
    if scope.time_window is not None and not scope.time_window.contains(when):
        findings.append(Finding(
            "outside_time_window", "time_window", "BLOCK",
            f"proposed at {when.strftime('%H:%M')} , outside the permitted window "
            f"{scope.time_window.start}-{scope.time_window.end} "
            f"{scope.time_window.tz}",
            observed=when.strftime("%H:%M"),
            limit=f"{scope.time_window.start}-{scope.time_window.end}"))

    # --- step-up -----------------------------------------------------------
    # Evaluated last and separately: a step-up is not a soft block, it is a
    # different answer. It says the bounds are satisfied and a human should
    # still look. Folding it into the error count is how a gate that behaves
    # correctly gets scored as if it failed.
    if scope.step_up_threshold is not None and total > scope.step_up_threshold:
        findings.append(Finding(
            "above_step_up", "requires_step_up_above", "STEP_UP",
            f"{money_str(total)} is above the {money_str(scope.step_up_threshold)} "
            f"threshold at which the mandate asks for human confirmation",
            observed=money_str(total),
            limit=money_str(scope.step_up_threshold)))

    if any(f.verdict == "BLOCK" for f in findings):
        verdict = "BLOCK"
    elif any(f.verdict == "STEP_UP" for f in findings):
        verdict = "STEP_UP"
    elif any(f.verdict == "REVIEW" for f in findings):
        verdict = "REVIEW"
    else:
        verdict = "PASS"
    return BoundsResult(verdict=verdict, findings=findings)
