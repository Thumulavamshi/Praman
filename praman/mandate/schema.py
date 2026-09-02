"""The mandate: a delegation of purchasing authority, in machine-checkable form.

This is the object AP2 and NPCI's UAP both produce and Praman consumes. Field
names deliberately echo AP2's intent-mandate vocabulary -- ``principal``,
``agent``, ``scope``, ``expires_at`` -- so a judge who knows AP2 can read it at a
glance, without inheriting AP2's full envelope. Where we simplify, we say so:

  * **Signing is raw Ed25519 over a canonical serialization, not W3C Verifiable
    Credentials.** A full VC stack is days of work and adds nothing a demo can
    show. What matters for the trust claim is that the mandate cannot be edited
    after issue without detection, and a detached Ed25519 signature gives
    exactly that. Stated in the README as a simplification rather than dressed
    up as a standard.
  * **One principal, one agent, one scope.** No delegation chains. Agent-to-agent
    sub-delegation is a real problem and an honest "not modelled" beats a
    half-built version of it.

Every monetary bound is a string, parsed with ``dec``. A cap that arrives as a
JSON float is a cap that is already slightly wrong.
"""
from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..ledger.money import dec, money

IST = timezone(timedelta(hours=5, minutes=30))

_ISO_PERIOD = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?)?$")


def parse_window(window: str) -> timedelta:
    """Parse the ISO-8601 duration subset we accept: P7D, P1D, PT24H.

    The full ISO grammar admits months and years, which are not fixed durations
    and would make a period cap ambiguous at exactly the moments it matters. We
    accept days and hours only, and reject the rest loudly.
    """
    m = _ISO_PERIOD.match(window)
    if not m or window in ("P", "PT"):
        raise ValueError(
            f"unsupported period {window!r}; use days or hours, e.g. P7D or PT24H")
    days = int(m.group(1) or 0)
    hours = int(m.group(2) or 0)
    if days == 0 and hours == 0:
        raise ValueError(f"period {window!r} is zero-length")
    return timedelta(days=days, hours=hours)


class PeriodCap(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount: str
    window: str = "P7D"

    @field_validator("amount")
    @classmethod
    def _amount_is_decimal(cls, v: str) -> str:
        if dec(v) <= 0:
            raise ValueError("period cap must be positive")
        return str(money(v))

    @field_validator("window")
    @classmethod
    def _window_parses(cls, v: str) -> str:
        parse_window(v)
        return v

    @property
    def as_decimal(self) -> Decimal:
        return money(self.amount)

    @property
    def delta(self) -> timedelta:
        return parse_window(self.window)


class Velocity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_txns: int = Field(gt=0)
    window: str = "P7D"

    @field_validator("window")
    @classmethod
    def _window_parses(cls, v: str) -> str:
        parse_window(v)
        return v

    @property
    def delta(self) -> timedelta:
        return parse_window(self.window)


class TimeWindow(BaseModel):
    """An allowed time-of-day range, in a named timezone.

    Windows that wrap midnight (22:00-02:00) are supported, because "not in the
    middle of the night" is the most natural thing a person says and it wraps.
    """
    model_config = ConfigDict(extra="forbid")
    start: str = "00:00"
    end: str = "23:59"
    tz: str = "Asia/Kolkata"

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        time.fromisoformat(v)
        return v

    def contains(self, when: datetime) -> bool:
        local = when.astimezone(IST) if self.tz == "Asia/Kolkata" else when
        t = local.time()
        s, e = time.fromisoformat(self.start), time.fromisoformat(self.end)
        if s <= e:
            return s <= t <= e
        return t >= s or t <= e          # wraps midnight


class Scope(BaseModel):
    """The bounds themselves. Everything here is mechanically checkable.

    That is the point of compiling natural language into this shape: the parts
    of a delegation that *can* be decided without judgment get decided without
    judgment, so the model is only consulted where judgment is genuinely
    required. It keeps latency down and, more importantly, keeps the two failure
    modes separable -- a bounds bug and an adjudication error are different
    bugs, and a system that conflates them cannot be debugged.
    """
    model_config = ConfigDict(extra="forbid")

    categories_allowed: list[str] = Field(default_factory=lambda: ["*"])
    categories_denied: list[str] = Field(default_factory=list)
    merchants_allowed: list[str] = Field(default_factory=lambda: ["*"])
    merchants_denied: list[str] = Field(default_factory=list)
    per_transaction_cap: str | None = None
    period_cap: PeriodCap | None = None
    velocity: Velocity | None = None
    time_window: TimeWindow | None = None
    requires_step_up_above: str | None = None

    # Free-text conditions the compiler could not reduce to a bound. These are
    # NOT silently dropped: they are handed to the adjudicator as part of the
    # delegated intent, and their presence is what a "my usual stores" or "keep
    # it sensible" clause turns into.
    soft_constraints: list[str] = Field(default_factory=list)

    @field_validator("per_transaction_cap", "requires_step_up_above")
    @classmethod
    def _positive_money(cls, v):
        if v is None:
            return v
        if dec(v) <= 0:
            raise ValueError("monetary bound must be positive")
        return str(money(v))

    @model_validator(mode="after")
    def _step_up_below_cap(self):
        if self.requires_step_up_above and self.per_transaction_cap:
            if money(self.requires_step_up_above) > money(self.per_transaction_cap):
                raise ValueError(
                    "requires_step_up_above is above per_transaction_cap, so it can "
                    "never fire -- one of the two is wrong")
        return self

    @property
    def cap(self) -> Decimal | None:
        return money(self.per_transaction_cap) if self.per_transaction_cap else None

    @property
    def step_up_threshold(self) -> Decimal | None:
        return (money(self.requires_step_up_above)
                if self.requires_step_up_above else None)


class Mandate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mandate_id: str
    principal: str
    agent: str
    scope: Scope
    issued_at: str
    expires_at: str
    signature: str | None = None
    source_text: str | None = None       # the delegation as the human said it

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _iso(cls, v: str) -> str:
        datetime.fromisoformat(v)
        return v

    @model_validator(mode="after")
    def _expiry_after_issue(self):
        if datetime.fromisoformat(self.expires_at) <= datetime.fromisoformat(self.issued_at):
            raise ValueError("mandate expires before it is issued")
        return self

    @property
    def issued(self) -> datetime:
        return datetime.fromisoformat(self.issued_at)

    @property
    def expires(self) -> datetime:
        return datetime.fromisoformat(self.expires_at)

    def is_active(self, when: datetime) -> bool:
        return self.issued <= when <= self.expires

    def signing_payload(self) -> dict:
        """Everything except the signature. What gets signed, and what gets verified."""
        d = self.model_dump(exclude_none=True)
        d.pop("signature", None)
        return d


class CartItem(BaseModel):
    """One line of a proposed purchase.

    ``name``, ``description`` and ``merchant_name`` are **attacker-controlled**.
    They come from a product listing that a hostile merchant wrote. Nothing in
    this class may be trusted, and the adjudicator fences all three. ``amount``
    and ``category`` come from the payment object and the merchant record, which
    is why the amount checks are structurally safe.
    """
    model_config = ConfigDict(extra="forbid")

    sku: str
    name: str
    category: str
    subcategory: str = ""
    merchant_id: str
    merchant_name: str = ""
    price: str
    quantity: int = 1
    description: str = ""

    @property
    def line_total(self) -> Decimal:
        return money(dec(self.price) * self.quantity)


class Proposal(BaseModel):
    """What the agent wants to buy, and the evidence about where it wants to buy it."""
    model_config = ConfigDict(extra="forbid")

    proposal_id: str
    mandate_id: str
    agent: str
    items: list[CartItem]
    merchant_id: str
    merchant_name: str = ""
    merchant_familiarity: Literal["established", "occasional", "new", "unknown"] = "unknown"
    merchant_onboarded: str = ""
    proposed_at: str
    instrument: str = "card"
    # amount is authoritative and comes from the payment object, never from
    # product text. See handlers.h_fee_debited for why that matters.
    amount: str

    @field_validator("amount")
    @classmethod
    def _positive(cls, v: str) -> str:
        if dec(v) <= 0:
            raise ValueError("proposal amount must be positive")
        return str(money(v))

    @property
    def total(self) -> Decimal:
        return money(self.amount)

    @property
    def when(self) -> datetime:
        return datetime.fromisoformat(self.proposed_at)

    def items_total(self) -> Decimal:
        return money(sum((i.line_total for i in self.items), Decimal("0")))

    def categories(self) -> set[str]:
        return {i.category for i in self.items}
