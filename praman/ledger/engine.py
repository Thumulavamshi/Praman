"""The apply loop: seen-gate first, then handler, then verify, then commit.

Three structural decisions, each of which exists because of a specific failure
mode in agentic commerce.

**The seen-gate is the first statement of ``apply``.** Not the first statement
of each handler -- the first statement of ``apply``, before dispatch, before
parsing, before anything. AI buyer agents retry aggressively and gateways
re-deliver webhooks; a non-idempotent merchant double-charges. Solving it
structurally, in one place, means no handler can forget and no new event type
can reintroduce the bug. This is the single most valuable line in the file.

**Three guards, so nothing ever ends the run.**

  1. A ``Rejected`` is a normal outcome. It is recorded with its reason and the
     loop moves on. Rejections are data -- they are the exception list a
     reconciler works from, and pretending they are errors produces a system
     that stops on its first surprise.
  2. An unexpected handler exception costs exactly one event. It is caught,
     recorded as a handler error with its traceback, and the loop moves on. One
     bad event must not take the book offline.
  3. The invariant suite runs after every applied entry. A violation means the
     entry is rolled back, not merely logged: the book returns to its last known
     good state and the event is recorded as refused. **The verifier outranks
     the handler, and it outranks the AI.**

Rollback is implemented as a re-fold from the log rather than by unwinding. Fold
is total and deterministic, so re-folding N events is exactly the state that
would exist had the bad event never arrived -- no compensating entries, no
"mostly undone".
"""
from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass, field

from .eventlog import Event, EventLog
from .handlers import HANDLERS
from .invariants import ALL_CHECKS, Violation, check_all
from .journal import Rejected
from .state import LedgerState, fold

log = logging.getLogger(__name__)


@dataclass
class ApplyResult:
    status: str                # applied | duplicate | rejected | error | refused
    event_id: str
    detail: str = ""
    violations: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("applied", "duplicate")


class Engine:
    """Owns the log, the state, and the only path by which they change."""

    def __init__(self, log_path=None, handlers=None, checks=ALL_CHECKS):
        self.eventlog = EventLog(log_path)
        self.handlers = dict(handlers or HANDLERS)
        self.checks = checks
        self.errors: list[dict] = []
        self.refusals: list[dict] = []
        self.quarantine: list[dict] = []
        self.state: LedgerState = self._refold()

    # -- derivation ----------------------------------------------------------

    def _refold(self, upto: list[Event] | None = None) -> LedgerState:
        return fold(upto if upto is not None else self.eventlog.events(), self.handlers)

    def snapshot_as_of(self, event_id: str) -> LedgerState:
        """The book exactly as it stood when ``event_id`` was applied.

        The dispute question -- "what did you know at 03:14 on the 12th" --
        answered without a snapshot table, because the answer is a fold of a
        prefix of an append-only log.
        """
        return fold(self.eventlog.until(event_id), self.handlers)

    # -- the apply loop ------------------------------------------------------

    def apply(self, event_id: str, event_type: str, ts: str, payload: dict) -> ApplyResult:
        # GUARD 0 -- the seen-gate. First statement, before anything else.
        if self.eventlog.has(event_id):
            return ApplyResult("duplicate", event_id, "already applied; ignored")

        if event_type not in self.handlers:
            # Forward compatibility: an unknown type is logged for the record but
            # posts nothing. An older build must not refuse a newer stream.
            self.eventlog.append(event_id, event_type, ts, payload)
            return ApplyResult("applied", event_id, f"no handler for {event_type}; logged only")

        # Prepared, not committed. Nothing enters the log until the entry has
        # balanced and the invariant suite has passed, which is what lets a
        # refused event be retried later instead of poisoning the log forever.
        ev = self.eventlog.prepare(event_id, event_type, ts, payload)

        try:
            raw = self.handlers[event_type](self.state, ev)
            entry = raw.normalized()
        except Rejected as r:
            # GUARD 1 -- rejection is a normal outcome, and often a transient
            # one: a refund settlement that overtook its initiation is rejected
            # now and applies cleanly when the gateway redelivers it.
            self.state = self._refold()
            self._quarantine(event_id, event_type, ts, payload, "rejected", r.reason,
                             code=r.code)
            return ApplyResult("rejected", event_id, r.reason)
        except Exception as exc:                  # noqa: BLE001 -- deliberate
            # GUARD 2 -- a handler crash costs exactly one event.
            self.state = self._refold()
            self.errors.append({"event_id": event_id, "type": event_type,
                                "error": f"{exc.__class__.__name__}: {exc}",
                                "traceback": traceback.format_exc()})
            self._quarantine(event_id, event_type, ts, payload, "error",
                             f"{exc.__class__.__name__}: {exc}")
            log.exception("handler for %s crashed on %s", event_type, event_id)
            return ApplyResult("error", event_id, f"{exc.__class__.__name__}: {exc}")

        if not entry.balanced():
            self.state = self._refold()
            v = [Violation("entry_balance", "unbalanced entry", {"legs": entry.legs})]
            self._quarantine(event_id, event_type, ts, payload, "refused",
                             "entry does not balance")
            return ApplyResult("refused", event_id, "entry does not balance", v)

        # An entry can legitimately be empty two ways, and only one is a bug.
        # A handler that returned no legs at all, for a type that is supposed to
        # move money, is a defect. A handler that returned legs which all
        # happened to be zero -- a zero-MDR UPI capture is the common case -- is
        # a correct no-op, and normalization is what emptied it. So the check
        # looks at what the handler produced, not at what survived normalization.
        if not raw.legs and event_type not in _no_impact_types():
            self.state = self._refold()
            detail = f"{event_type} produced no legs but is not a no-impact event"
            self._quarantine(event_id, event_type, ts, payload, "refused", detail)
            return ApplyResult("refused", event_id, detail)

        self.state.post(entry.legs)
        self.state.entries.append(entry)
        self.state.applied.add(event_id)

        # GUARD 3 -- the verifier outranks the handler, and it outranks the AI.
        violations = check_all(self.state, self.checks)
        if violations:
            self.state = self._refold()
            detail = "; ".join(str(v) for v in violations)
            self._quarantine(event_id, event_type, ts, payload, "refused", detail,
                             violations=[str(v) for v in violations])
            return ApplyResult("refused", event_id, detail, violations)

        self.eventlog.commit(ev)
        return ApplyResult("applied", event_id, entry.memo)

    def _quarantine(self, event_id, event_type, ts, payload, status, detail,
                    *, code="", violations=None) -> None:
        """Everything that arrived and did not land, with the reason why.

        This list is a deliverable, not debug output: it is the honest exception
        list the metrics report publishes, and the queue a reconciliation agent
        works from. An event here is not lost -- it is simply not in the book,
        and it can be redelivered.
        """
        self.quarantine.append({
            "event_id": event_id, "type": event_type, "ts": ts,
            "status": status, "code": code, "detail": detail,
            "violations": violations or [], "payload": payload,
        })
        if status == "refused":
            self.refusals.append({"event_id": event_id, "type": event_type,
                                  "detail": detail, "violations": violations or []})

    def apply_event(self, e: dict) -> ApplyResult:
        return self.apply(e["event_id"], e["type"], e["ts"], e["payload"])

    def apply_many(self, events) -> list[ApplyResult]:
        """Ingest a stream. Never raises: that is the whole point of the guards.

        In a library the reconnect belongs to the caller, so what is guaranteed
        here is the part a caller cannot supply: a result for every event, and a
        state that is never left half-updated.
        """
        return [self.apply_event(e) for e in events]

    def drain_quarantine(self, max_passes: int = 10) -> dict:
        """Re-offer quarantined events until no further progress is made.

        This is the third guard at the level where it actually lives. A gateway
        that gets no ack retries, so an event rejected because it overtook its
        cause is not lost -- it comes back, and by then the cause has landed.
        Draining models that redelivery explicitly instead of assuming the
        stream arrived in a helpful order.

        The loop terminates because each pass either applies at least one event
        (strictly shrinking the queue) or applies none, which ends it. What is
        left after draining is the honest exception list: events that are not
        merely early, but wrong.
        """
        passes = 0
        applied_total = 0
        while passes < max_passes:
            pending, self.quarantine = self.quarantine, []
            if not pending:
                break
            before = applied_total
            for q in pending:
                r = self.apply(q["event_id"], q["type"], q["ts"], q["payload"])
                if r.status == "applied":
                    applied_total += 1
            passes += 1
            if applied_total == before:
                break
        return {"passes": passes, "applied": applied_total,
                "still_quarantined": len(self.quarantine)}

    # -- integrity -----------------------------------------------------------

    def verify(self) -> tuple[bool, list[Violation], str | None]:
        chain_ok, bad = self.eventlog.verify_chain()
        return chain_ok, check_all(self.state, self.checks), bad


def _no_impact_types():
    from . import events as E
    return E.NO_JOURNAL_IMPACT
