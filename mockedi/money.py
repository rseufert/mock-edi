"""The one rounding rule for money: to the cent, half up.

Every amount the mock stores or writes - a line's extended amount, tax, a
total, the integer in TDS - is rounded here and nowhere else. 0.625 is 0.63.

Half up, because that is what an invoice clerk does and what a test written
by hand expects. `Decimal.quantize` on its own rounds half to even, which
made 12.50 at 5% a tax of 0.62 and was never anybody's decision (#206). A
negative amount rounds the same distance from zero, so a credit of 0.625 is
as large as a charge of it.

It has no imports from the package, so `db` and `transactions` can both use
it without either depending on the other.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Union

CENT = Decimal("0.01")


def cents(value: Union[Decimal, str, int, float]) -> Decimal:
    """`value` as an amount of money: two decimal places, a half cent up."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def unit_price(value: Union[Decimal, str, int, float]) -> str:
    """A unit price as it is written: every decimal it was given, and at
    least two.

    A unit price is not an amount, and is not rounded: 0.125 each is ordinary
    for anything sold by the thousand, and the elements that carry it (X12's
    212, EDIFACT's 5118) are decimals of up to 17 and 15 characters. Rounding
    it to 0.12 changed the order - a thousand of them is 125.00, not 120.00 -
    and then compared 0.12 with 0.12 when the supplier answered at the real
    price (#206). The *amount* a price produces is rounded, by `cents`.
    """
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    whole, _, decimals = format(value.normalize(), "f").partition(".")
    return "%s.%s" % (whole, decimals.ljust(2, "0"))
