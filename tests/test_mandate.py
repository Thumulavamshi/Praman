"""Mandate schema, period parsing, and Ed25519 signing."""
import pytest
from datetime import datetime

from praman.mandate.schema import (Mandate, PeriodCap, Scope, TimeWindow, Velocity,
                                   parse_window)
from praman.mandate.signing import MandateIssuer, verify_mandate


def m(**scope_kwargs) -> Mandate:
    return Mandate(
        mandate_id="mnd_1", principal="user_9931", agent="agent_1",
        scope=Scope(**scope_kwargs),
        issued_at="2026-08-01T10:00:00+05:30",
        expires_at="2026-12-01T10:00:00+05:30")


def test_period_windows_accept_days_and_hours_and_reject_the_ambiguous_rest():
    assert parse_window("P7D").days == 7
    assert parse_window("PT24H").total_seconds() == 86400
    for bad in ["P1M", "P1Y", "7 days", "P", "P0D"]:
        with pytest.raises(ValueError):
            parse_window(bad)


def test_a_step_up_above_the_cap_can_never_fire_and_is_rejected():
    with pytest.raises(ValueError, match="never fire"):
        m(per_transaction_cap="1000.00", requires_step_up_above="2000.00")


def test_a_mandate_that_expires_before_it_is_issued_is_rejected():
    with pytest.raises(ValueError, match="expires before"):
        Mandate(mandate_id="x", principal="p", agent="a", scope=Scope(),
                issued_at="2026-12-01T10:00:00+05:30",
                expires_at="2026-08-01T10:00:00+05:30")


def test_time_windows_wrap_midnight():
    night = TimeWindow(start="22:00", end="02:00")
    assert night.contains(datetime.fromisoformat("2026-09-03T23:30:00+05:30"))
    assert night.contains(datetime.fromisoformat("2026-09-03T01:00:00+05:30"))
    assert not night.contains(datetime.fromisoformat("2026-09-03T12:00:00+05:30"))


def test_time_windows_are_evaluated_in_ist_not_the_host_clock():
    day = TimeWindow(start="06:00", end="23:00", tz="Asia/Kolkata")
    # 22:00 UTC is 03:30 IST -- the middle of the night, whatever the server thinks.
    assert not day.contains(datetime.fromisoformat("2026-09-03T22:00:00+00:00"))
    assert day.contains(datetime.fromisoformat("2026-09-03T09:00:00+00:00"))


def test_unknown_scope_fields_are_rejected_rather_than_silently_ignored():
    with pytest.raises(Exception):
        Scope(per_txn_cap="2000.00")     # typo'd field name must not pass silently


def test_a_signed_mandate_verifies_and_a_widened_one_does_not():
    issuer = MandateIssuer()
    signed = issuer.issue(m(per_transaction_cap="2000.00"))
    assert issuer.verify(signed)

    widened = signed.model_copy(
        update={"scope": Scope(per_transaction_cap="10000.00")})
    assert not issuer.verify(widened)


def test_an_unsigned_mandate_does_not_verify():
    assert not MandateIssuer().verify(m())


def test_a_signature_from_a_different_issuer_does_not_verify():
    a, b = MandateIssuer(), MandateIssuer()
    assert not b.verify(a.issue(m()))


def test_a_malformed_signature_is_false_not_an_exception():
    signed = m().model_copy(update={"signature": "ed25519:not-base64!!"})
    assert not MandateIssuer().verify(signed)
