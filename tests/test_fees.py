"""Fee, GST and withholding treatment on an Indian capture."""
from decimal import Decimal

from praman.ledger.fees import (FeeSchedule, compute_fees, output_gst_rate,
                                split_inclusive_gst)


def test_upi_is_zero_mdr_and_therefore_zero_gst():
    f = compute_fees("1000.00", "upi")
    assert f.mdr == Decimal("0.00")
    assert f.gst_on_mdr == Decimal("0.00")
    assert f.net_settlement == Decimal("1000.00")


def test_card_mdr_carries_18_percent_gst_as_a_recoverable_asset():
    f = compute_fees("1000.00", "card")
    assert f.mdr == Decimal("20.00")
    assert f.gst_on_mdr == Decimal("3.60")
    assert f.total_deducted == Decimal("23.60")
    assert f.net_settlement == Decimal("976.40")


def test_194o_and_tcs_are_off_by_default_for_a_pure_payment_aggregator():
    f = compute_fees("1000.00", "card")
    assert f.tds == Decimal("0.00")
    assert f.tcs == Decimal("0.00")


def test_marketplace_schedule_withholds_at_the_post_2024_rates():
    marketplace = FeeSchedule(tds_194o=True, tcs_52=True)
    f = compute_fees("10000.00", "card", marketplace)
    assert f.tds == Decimal("10.00")      # 0.1% w.e.f. 2024-10-01
    assert f.tcs == Decimal("50.00")      # 0.5% w.e.f. 2024-07-10


def test_unknown_instrument_is_an_error_not_a_guess():
    import pytest
    with pytest.raises(KeyError):
        compute_fees("100.00", "crypto")


def test_inclusive_gst_split_always_sums_back_to_the_gross():
    for gross in ["285.00", "1899.00", "0.01", "1450.00", "2000.00", "33.33"]:
        for cat in ["groceries", "household", "electronics"]:
            taxable, tax = split_inclusive_gst(gross, cat)
            assert taxable + tax == Decimal(gross), (gross, cat)


def test_unknown_category_falls_back_rather_than_crashing():
    assert output_gst_rate("nonsense") == Decimal("0.1800")
