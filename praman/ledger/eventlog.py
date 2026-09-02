"""The append-only event log, and the hash chain that makes it evidence.

Two jobs, and they are not the same job.

**Append-only storage.** Events go in, nothing comes out, nothing is edited.
JSON is parsed with ``parse_float=Decimal`` so a monetary value never exists as
a binary float even for the instant between reading the file and calling
``dec()``. This is not paranoia: ``json.load`` on ``{"amount": 1799.10}`` gives
you a float that is already wrong before any of our code runs.

**The hash chain.** Each record carries ``prev_hash`` and its own ``hash``,
computed over a canonical serialization of the record minus the hash field.
Change a captured amount six months later and every subsequent hash breaks, and
``verify_chain`` names the first record that does not line up.

That property is the whole reason a merchant can answer *"I didn't authorize
that, my agent did"*. The defence is not "trust our database" -- it is "here is
the mandate, here is the cart the agent saw, here is the decision and its
reasons, here is the capture, and here is a chain that shows none of it was
written after the dispute arrived."

The chain is tamper-**evident**, not tamper-proof: anyone who can rewrite the
whole file can recompute every hash. Making it tamper-proof means anchoring the
head hash somewhere the merchant does not control, which is a deployment
decision, not a code one. Stated plainly here rather than overclaimed.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    """Serialize deterministically: sorted keys, no whitespace, Decimals as strings.

    Two records that mean the same thing must hash the same, so key order and
    float formatting cannot be allowed to vary. ``default=str`` renders a
    Decimal as its exact digits rather than letting json reach for a float.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=_json_default)


def _json_default(o: Any) -> str:
    if isinstance(o, Decimal):
        return format(o, "f")
    raise TypeError(f"{type(o).__name__} is not JSON serializable")


def record_hash(record: dict) -> str:
    """SHA-256 over the record with its own ``hash`` field removed."""
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(_canonical(body).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Event:
    """One entry in the log.

    ``event_id`` is the idempotency key. It comes from the producer -- the
    gateway's payment id, the agent's proposal id -- because that is what a
    retry will repeat. Generating it here would defeat the seen-gate entirely:
    a retried delivery would arrive with a fresh id and get booked twice.
    """

    event_id: str
    type: str
    seq: int
    ts: str
    payload: dict
    prev_hash: str
    hash: str

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "type": self.type,
            "seq": self.seq,
            "ts": self.ts,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }

    @staticmethod
    def from_dict(d: dict) -> "Event":
        return Event(
            event_id=d["event_id"], type=d["type"], seq=d["seq"], ts=d["ts"],
            payload=d["payload"], prev_hash=d["prev_hash"], hash=d["hash"],
        )


class EventLog:
    """Append-only, hash-chained, optionally file-backed.

    File format is JSON Lines: one record per line, appended and fsynced. A
    crash mid-append can leave a torn final line, so ``load`` stops at the first
    unparseable line rather than raising -- a truncated tail is a recoverable
    state, and refusing to open the log because of it would be worse.
    """

    def __init__(self, path: str | os.PathLike | None = None):
        self.path = Path(path) if path is not None else None
        self._events: list[Event] = []
        self._by_id: dict[str, Event] = {}
        self._lock = threading.Lock()
        if self.path is not None and self.path.exists():
            self._load()

    # -- reading -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self) -> Iterator[Event]:
        return iter(self._events)

    @property
    def head_hash(self) -> str:
        return self._events[-1].hash if self._events else GENESIS

    def has(self, event_id: str) -> bool:
        return event_id in self._by_id

    def get(self, event_id: str) -> Event | None:
        return self._by_id.get(event_id)

    def events(self) -> list[Event]:
        return list(self._events)

    def until(self, event_id: str) -> list[Event]:
        """Every event up to and including ``event_id``.

        This is the as-of query, and it is the dispute evidence: "here is the
        book at the instant of the disputed transaction, not as it looks today
        after four refunds and a settlement."
        """
        out: list[Event] = []
        for e in self._events:
            out.append(e)
            if e.event_id == event_id:
                return out
        raise KeyError(f"event {event_id!r} is not in the log")

    def until_seq(self, seq: int) -> list[Event]:
        return [e for e in self._events if e.seq <= seq]

    # -- writing -------------------------------------------------------------

    def prepare(self, event_id: str, type: str, ts: str, payload: dict) -> Event:
        """Build the next record without committing it.

        The engine has to run the handler and the invariant suite *before* it is
        willing to say the event happened, and the handler needs a real Event
        (id, seq, ts) to work with. Preparing and committing separately is what
        lets a refused event stay out of the log entirely -- which is what keeps
        the promise that the log always folds cleanly.
        """
        rec = {
            "event_id": event_id,
            "type": type,
            "seq": len(self._events),
            "ts": ts,
            "payload": payload,
            "prev_hash": self.head_hash,
        }
        rec["hash"] = record_hash(rec)
        return Event.from_dict(rec)

    def commit(self, ev: Event) -> Event:
        """Append a prepared event. Re-prepares if the head moved underneath it."""
        with self._lock:
            existing = self._by_id.get(ev.event_id)
            if existing is not None:
                return existing
            if ev.prev_hash != self.head_hash or ev.seq != len(self._events):
                rec = ev.to_dict()
                rec["seq"] = len(self._events)
                rec["prev_hash"] = self.head_hash
                rec["hash"] = record_hash(rec)
                ev = Event.from_dict(rec)
            self._events.append(ev)
            self._by_id[ev.event_id] = ev
            if self.path is not None:
                self._append_line(ev.to_dict())
            return ev

    def append(self, event_id: str, type: str, ts: str, payload: dict) -> Event:
        """Append one event and return it. Duplicate ``event_id`` returns the original.

        Returning the original rather than raising is the deliberate choice: the
        caller asked for this event to exist, and it does. Raising would push
        every caller into a try/except that means "fine, carry on".
        """
        with self._lock:
            existing = self._by_id.get(event_id)
            if existing is not None:
                return existing
            prev = self.head_hash
            rec = {
                "event_id": event_id,
                "type": type,
                "seq": len(self._events),
                "ts": ts,
                "payload": payload,
                "prev_hash": prev,
            }
            rec["hash"] = record_hash(rec)
            ev = Event.from_dict(rec)
            self._events.append(ev)
            self._by_id[event_id] = ev
            if self.path is not None:
                self._append_line(rec)
            return ev

    def extend(self, events: Iterable[dict]) -> list[Event]:
        return [self.append(e["event_id"], e["type"], e["ts"], e["payload"])
                for e in events]

    def _append_line(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(_canonical(rec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def _load(self) -> None:
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line, parse_float=Decimal)
                except json.JSONDecodeError:
                    break        # torn tail from a crash mid-append; stop here
                ev = Event.from_dict(rec)
                self._events.append(ev)
                self._by_id[ev.event_id] = ev

    # -- integrity -----------------------------------------------------------

    def verify_chain(self) -> tuple[bool, str | None]:
        """Recompute every hash. Returns (ok, first_bad_event_id)."""
        prev = GENESIS
        for i, ev in enumerate(self._events):
            if ev.prev_hash != prev or ev.seq != i:
                return False, ev.event_id
            if record_hash(ev.to_dict()) != ev.hash:
                return False, ev.event_id
            prev = ev.hash
        return True, None


def load_events_file(path: str | os.PathLike) -> list[dict]:
    """Read a generated event stream (JSON Lines) with Decimal-safe parsing."""
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line, parse_float=Decimal))
    return out


def write_events_file(path: str | os.PathLike, events: Iterable[dict]) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(_canonical(e) + "\n")
    os.replace(tmp, p)
