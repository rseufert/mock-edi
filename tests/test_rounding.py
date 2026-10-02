"""One rounding rule for money: to the cent, half up (#206).

Every amount used to go through `Decimal.quantize` with nothing said about
ties, and Python's default for those is half-to-even - which nobody chose.
It made 12.50 at 5% a tax of 0.62 where 0.625 rounds to 0.63, and it is not
even consistent with itself to a reader: 0.635 went up and 0.625 went down.

The rule is `money.cents`, and the last test here holds every other module
to going through it, so a second rule cannot grow back.
"""
import os
import re
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from mockedi import db, money, transactions

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order


class TheRule(unittest.TestCase):
    def test_a_half_cent_goes_up(self):
        for value, expected in (("0.625", "0.63"), ("0.635", "0.64"),
                                ("0.645", "0.65"), ("3.125", "3.13"),
                                ("0.005", "0.01"), ("2.675", "2.68")):
            with self.subTest(value=value):
                self.assertEqual(str(money.cents(value)), expected)

    def test_anything_else_goes_to_the_nearest(self):
        for value, expected in (("0.624", "0.62"), ("0.626", "0.63"),
                                ("12.5", "12.50"), ("7", "7.00"),
                                ("0.004", "0.00")):
            with self.subTest(value=value):
                self.assertEqual(str(money.cents(value)), expected)

    def test_a_negative_amount_rounds_the_same_distance(self):
        # Away from zero, so a credit of 0.625 is as large as a charge of it.
        self.assertEqual(str(money.cents("-0.625")), "-0.63")

    def test_every_way_of_writing_an_amount_agrees(self):
        value = Decimal("0.625")
        self.assertEqual(db.money(value), "0.63")
        self.assertEqual(transactions.price_text(value), "0.63")
        self.assertEqual(transactions.implied_decimal(value), "63")

    def test_implied_decimals_do_not_lose_the_half_cent_either(self):
        # TDS carries cents as an integer: 13.125 is 1313, not 1312.
        self.assertEqual(transactions.implied_decimal(Decimal("13.125")), "1313")


def wire(rows, *tags):
    out = {}
    for row in rows:
        for text in row["payload"].replace("'", "~").split("~"):
            parts = re.split(r"[*+:]", text.strip())
            if parts[0] in tags:
                out.setdefault(parts[0], []).append(parts[1:])
    return out


class OnTheWire(MockServerCase):
    """The example from the issue: 12.50 at 5% is 0.63."""
    config_kwargs = {"tax_rate": "0.05"}

    def test_tax_on_an_810(self):
        self.send(x12_order("PO-TAX", lines=(("WIDGET-001", 1, "12.50"),)))
        self.post("/_mock/advance?all")
        sent = wire(self.mailbox(ACME, "invoice"), "TXI", "TDS")
        self.assertEqual(sent["TXI"], [["ST", "0.63"]])
        self.assertEqual(sent["TDS"], [["1313"]])
        invoice = self.get("/_mock/orders/PO-TAX")[2]["invoices"][0]
        self.assertEqual((invoice["subtotal"], invoice["tax"], invoice["total"]),
                         ("12.50", "0.63", "13.13"))

    def test_a_line_amount_that_ends_on_a_half_cent(self):
        # A quarter of 12.50 is 3.125: 3.13, then 5% of that is 0.1565.
        self.send(x12_order("PO-QUARTER", lines=(("WIDGET-001", "0.25", "12.50"),)))
        self.post("/_mock/advance?all")
        invoice = self.get("/_mock/orders/PO-QUARTER")[2]["invoices"][0]
        self.assertEqual((invoice["subtotal"], invoice["tax"], invoice["total"]),
                         ("3.13", "0.16", "3.29"))
        self.assertEqual(wire(self.mailbox(ACME, "invoice"), "TDS")["TDS"],
                         [["329"]])

    def test_an_invoic_says_the_same(self):
        self.send(edifact_order("PO-EU-TAX", lines=(("WIDGET-001", 1, "12.50"),)),
                  headers={"Content-Type": "application/edifact"})
        self.post("/_mock/advance?all")
        amounts = dict((parts[0], parts[1]) for parts in
                       wire(self.mailbox(EURODIS, "invoice"), "MOA")["MOA"])
        self.assertEqual((amounts["79"], amounts["124"], amounts["139"]),
                         ("12.50", "0.63", "13.13"))


class NothingRoundsOnItsOwn(unittest.TestCase):
    """`money.cents` is the only place an amount is rounded.

    A `quantize` anywhere else is a second rule waiting to disagree with the
    first by half a cent.
    """

    def test_no_other_module_quantizes(self):
        found = []
        package = os.path.join(ROOT, "mockedi")
        for folder, _dirs, files in os.walk(package):
            for name in files:
                if not name.endswith(".py") or name == "money.py":
                    continue
                path = os.path.join(folder, name)
                with open(path, encoding="utf-8") as handle:
                    for number, line in enumerate(handle, 1):
                        if ".quantize(" in line:
                            found.append("%s:%d" % (os.path.relpath(path, ROOT),
                                                    number))
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
