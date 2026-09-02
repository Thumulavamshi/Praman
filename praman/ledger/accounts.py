"""Chart of accounts for a merchant receiving agent-initiated payments.

Whose book is this? The merchant's. Praman is merchant-side infrastructure: it
answers "what does my book look like, and can I defend it" -- not "what does the
gateway think".

The account set is deliberately small. Every account here exists because some
event in ``events.py`` has to post to it, and an account nothing posts to is a
line in a diagram, not a ledger.
"""
from __future__ import annotations

from enum import Enum


class AccountType(str, Enum):
    ASSET = "asset"
    LIABILITY = "liability"
    INCOME = "income"
    EXPENSE = "expense"


# code -> (name, type)
CHART: dict[str, tuple[str, AccountType]] = {
    "1100": ("Bank Account", AccountType.ASSET),
    "1200": ("PG Settlement Receivable", AccountType.ASSET),
    "1300": ("Reserve Held at PG", AccountType.ASSET),
    "1400": ("GST Input Credit (on MDR)", AccountType.ASSET),
    "1500": ("TDS Receivable (194-O)", AccountType.ASSET),
    "1600": ("GST TCS Credit (s.52)", AccountType.ASSET),
    "2100": ("Refunds Payable", AccountType.LIABILITY),
    "2200": ("GST Output Payable", AccountType.LIABILITY),
    "2300": ("Chargeback Provision", AccountType.LIABILITY),
    "4000": ("Sales Revenue", AccountType.INCOME),
    "5000": ("Payment Gateway Fees (MDR)", AccountType.EXPENSE),
    "5100": ("Chargeback Losses", AccountType.EXPENSE),
}

BANK = "1100"
PG_RECEIVABLE = "1200"
RESERVE = "1300"
GST_INPUT = "1400"
TDS_RECEIVABLE = "1500"
TCS_CREDIT = "1600"
REFUNDS_PAYABLE = "2100"
GST_OUTPUT = "2200"
CHARGEBACK_PROVISION = "2300"
SALES_REVENUE = "4000"
MDR_EXPENSE = "5000"
CHARGEBACK_LOSSES = "5100"

# Accounts whose natural balance is a debit. Used by the balance-sheet
# invariant to decide the sign convention, and by the reporting layer so a
# liability of 500 does not print as -500.
DEBIT_NATURED = {AccountType.ASSET, AccountType.EXPENSE}


def account_type(code: str) -> AccountType:
    try:
        return CHART[code][1]
    except KeyError:
        raise KeyError(f"unknown account code {code!r} -- not in the chart of accounts")


def account_name(code: str) -> str:
    return CHART[code][0]


def is_debit_natured(code: str) -> bool:
    return account_type(code) in DEBIT_NATURED
