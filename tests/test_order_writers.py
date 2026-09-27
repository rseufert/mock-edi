"""Writing the two documents a buyer sends, and reading them back.

`transactions.py` wrote the seller's four and read the buyer's two. These are
the other pair: `write_order` and `write_change`, for the partner the mock
buys from.

The difference that matters is not the segments - it is which side of every
party role the mock is on. A seller writes itself as `SU`/`SE` and its partner
as `BY`; an order the mock places is the other way round, and getting that
backwards produces a document that validates perfectly and names the wrong
company as the customer. Several tests below exist only to pin that down.

Nothing in the mock calls these yet (#125's wiring will). `read_order` and
`read_change` are the test: a value that does not survive the trip was written
into the wrong element.
"""
import datetime
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, edifact, transactions, validate, x12
from mockedi.transactions import Change, ChangeLine, Party

US = Party(name="Mock EDI Buying Co", identifier="MOCKEDI",
           street="500 Seaport Boulevard", city="Boston", region="MA",
           postal="02210", country="US")
SUPPLIER = {"id": "NORTHWIND", "name": "Northwind Traders",
            "street": "12 Harbour Way", "city": "Seattle", "region": "WA",
            "postal": "98101", "country": "US"}
ORDER = {"po_number": "4500001001", "ordered_on": "2026-10-01",
         "requested_on": "2026-10-15", "currency": "USD", "purpose": "00"}
LINES = [{"line": "1", "sku": "WIDGET-001", "upc": "076123400003",
          "quantity": "100", "uom": "EA", "price": "12.50",
          "description": "Widget, blue, 40mm"},
         {"line": "2", "sku": "BRKT-050", "quantity": "40", "uom": "CA",
          "price": "4.15"}]
WHEN = datetime.datetime(2026, 10, 1, 9, 30)

CODE = {"X12": ("850", "860", "PO", "PC"),
        "EDIFACT": ("ORDERS", "ORDCHG", "", "")}


def wrap(dialect, code, body, group=""):
    if dialect == "X12":
        return x12.wrap([x12.message(code, "0001", body)], "MOCKEDI",
                        "NORTHWIND", "1", "1", group, moment=WHEN)
    return edifact.wrap([edifact.message(code, "1", body, version="D:96A:UN")],
                        "MOCKEDI", "NORTHWIND", "1", moment=WHEN)


def order_message(dialect, order=None, lines=None):
    body = transactions.write_order(dialect, US, SUPPLIER, order or ORDER,
                                    lines or LINES, WHEN)
    code, _change_code, group, _pc = CODE[dialect]
    return wrap(dialect, code, body, group).groups[0].messages[0]


def change_message(dialect, change, order=None):
    body = transactions.write_change(dialect, US, SUPPLIER, order or ORDER,
                                     change, WHEN)
    _code, change_code, _po, group = CODE[dialect]
    return wrap(dialect, change_code, body, group).groups[0].messages[0]


def a_change(purpose="04", lines=None):
    return Change(
        po_number="4500001001", purpose=purpose, sequence="2",
        changed_on=datetime.date(2026, 10, 5),
        ordered_on=datetime.date(2026, 10, 1), currency="USD",
        lines=lines if lines is not None else [
            ChangeLine(number="1", sku="WIDGET-001", quantity=Decimal("60"),
                       uom="EA", price=Decimal("12.50"), action="QD"),
            ChangeLine(number="2", sku="BRKT-050", quantity=Decimal("0"),
                       uom="EA", price=Decimal("0.00"), action="DI")])


class AnOrderRoundTrips(unittest.TestCase):
    """What `write_order` was given comes back out of `read_order`."""

    def read(self, dialect, order=None, lines=None):
        return transactions.read_order(
            order_message(dialect, order, lines), dialect)

    def test_the_purchase_order_number_survives(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                self.assertEqual(self.read(dialect).po_number, "4500001001")

    def test_the_dates_survive(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                order = self.read(dialect)
                self.assertEqual(order.ordered_on, datetime.date(2026, 10, 1))
                self.assertEqual(order.requested_on,
                                 datetime.date(2026, 10, 15))

    def test_the_currency_survives(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                self.assertEqual(self.read(dialect).currency, "USD")

    def test_every_line_survives_intact(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                lines = self.read(dialect).lines
                self.assertEqual(len(lines), 2)
                self.assertEqual(lines[0].number, "1")
                self.assertEqual(lines[0].sku, "WIDGET-001")
                self.assertEqual(lines[0].upc, "076123400003")
                self.assertEqual(lines[0].quantity, Decimal("100"))
                self.assertEqual(lines[0].uom, "EA")
                self.assertEqual(lines[0].price, Decimal("12.50"))
                self.assertEqual(lines[0].description, "Widget, blue, 40mm")
                self.assertEqual(lines[1].sku, "BRKT-050")
                self.assertEqual(lines[1].upc, "")

    def test_a_unit_that_is_not_each_survives_the_translation(self):
        # CA goes out as CT and has to come back as CA, which is the one
        # round-trip in this file that goes through a code table.
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                self.assertEqual(self.read(dialect).lines[1].uom, "CA")

    def test_the_total_is_what_was_ordered(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                self.assertEqual(self.read(dialect).total, Decimal("1416.00"))

    def test_a_line_with_no_price_is_written_as_zero_not_left_out(self):
        lines = [{"line": "1", "sku": "W", "quantity": "5", "uom": "EA"}]
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                line = self.read(dialect, lines=lines).lines[0]
                self.assertEqual(line.price, Decimal("0.00"))
                self.assertEqual(line.quantity, Decimal("5"))

    def test_the_order_date_defaults_to_now_when_the_order_has_none(self):
        order = dict(ORDER)
        order.pop("ordered_on")
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                self.assertEqual(self.read(dialect, order=order).ordered_on,
                                 WHEN.date())


class TheMockIsTheBuyerNow(unittest.TestCase):
    """The flip, which is the only thing about these writers that is hard.

    A document that names the wrong company as the customer validates
    perfectly, so nothing but an assertion on the roles catches it.
    """

    def parties(self, dialect):
        return transactions.read_order(order_message(dialect), dialect).parties

    def test_x12_writes_the_mock_as_the_buyer_and_the_partner_as_the_seller(self):
        parties = self.parties("X12")
        self.assertEqual(parties["BY"].identifier, "MOCKEDI")
        self.assertEqual(parties["SE"].identifier, "NORTHWIND")

    def test_edifact_writes_the_same_two_in_its_own_words(self):
        parties = self.parties("EDIFACT")
        self.assertEqual(parties["BY"].identifier, "MOCKEDI")
        self.assertEqual(parties["SU"].identifier, "NORTHWIND")

    def test_the_partners_address_is_the_suppliers(self):
        for dialect, role in (("X12", "SE"), ("EDIFACT", "SU")):
            with self.subTest(dialect=dialect):
                party = self.parties(dialect)[role]
                self.assertEqual(party.name, "Northwind Traders")
                self.assertEqual(party.city, "Seattle")

    def test_the_goods_come_to_the_mock_when_nobody_says_otherwise(self):
        for dialect, role in (("X12", "ST"), ("EDIFACT", "DP")):
            with self.subTest(dialect=dialect):
                party = self.parties(dialect)[role]
                self.assertEqual(party.identifier, "MOCKEDI")
                self.assertEqual(party.city, "Boston")

    def test_a_named_ship_to_is_used_instead(self):
        order = dict(ORDER, ship_to_id="DC-9", ship_to_name="Depot 9",
                     ship_to_street="1 Depot Road", ship_to_city="Reno",
                     ship_to_region="NV", ship_to_postal="89501",
                     ship_to_country="US")
        for dialect, role in (("X12", "ST"), ("EDIFACT", "DP")):
            with self.subTest(dialect=dialect):
                parties = transactions.read_order(
                    order_message(dialect, order=order), dialect).parties
                self.assertEqual(parties[role].identifier, "DC-9")
                self.assertEqual(parties[role].city, "Reno")
                # And the mock is still the buyer, not the ship-to.
                self.assertEqual(parties["BY"].identifier, "MOCKEDI")


class AChangeRoundTrips(unittest.TestCase):

    def read(self, dialect, purpose="04", lines=None):
        return transactions.read_change(
            change_message(dialect, a_change(purpose, lines)), dialect)

    def test_the_order_it_changes_survives(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                self.assertEqual(self.read(dialect).po_number, "4500001001")

    def test_the_sequence_survives(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                self.assertEqual(self.read(dialect).sequence, "2")

    def test_a_change_is_not_a_cancellation(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                self.assertFalse(self.read(dialect).cancels)

    def test_a_cancellation_says_so(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                change = self.read(dialect, purpose="01")
                self.assertTrue(change.cancels)
                self.assertEqual(change.purpose, "01")

    def test_every_changed_line_survives(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                lines = self.read(dialect).lines
                self.assertEqual([line.number for line in lines], ["1", "2"])
                self.assertEqual(lines[0].quantity, Decimal("60"))
                self.assertEqual(lines[0].price, Decimal("12.50"))
                self.assertEqual(lines[0].sku, "WIDGET-001")

    def test_x12_keeps_the_exact_action_a_line_asked_for(self):
        self.assertEqual([line.action for line in self.read("X12").lines],
                         ["QD", "DI"])

    def test_edifact_keeps_only_as_much_of_it_as_1229_can_say(self):
        # 1229 has one code for every kind of amendment, so QD, PC and QI all
        # go out as 3 and come back as CA. Deleting a line has its own code
        # and survives. This is the writer being faithful to the standard, not
        # the round-trip failing - and a test that expected QD back would be
        # asserting something EDIFACT cannot express.
        self.assertEqual([line.action for line in self.read("EDIFACT").lines],
                         ["CA", "DI"])

    def test_a_deleted_line_keeps_its_zero_quantity(self):
        for dialect in ("X12", "EDIFACT"):
            with self.subTest(dialect=dialect):
                self.assertEqual(self.read(dialect).lines[1].quantity,
                                 Decimal("0"))

    def test_an_added_line_survives(self):
        lines = [ChangeLine(number="3", sku="GEAR-100", quantity=Decimal("25"),
                            uom="EA", price=Decimal("8.90"), action="AI")]
        for dialect, expected in (("X12", "AI"), ("EDIFACT", "AI")):
            with self.subTest(dialect=dialect):
                line = self.read(dialect, lines=lines).lines[0]
                self.assertEqual(line.action, expected)
                self.assertEqual(line.sku, "GEAR-100")
                self.assertEqual(line.quantity, Decimal("25"))


class BothValidateAgainstTheDictionary(unittest.TestCase):
    """The same standard everything else the mock writes is held to."""

    def check(self, interchange):
        report = validate.validate(interchange)
        self.assertTrue(report.clean, "\n".join(ack.explain(report)))

    def test_an_850_validates(self):
        body = transactions.write_order("X12", US, SUPPLIER, ORDER, LINES, WHEN)
        self.check(wrap("X12", "850", body, "PO"))

    def test_an_orders_validates(self):
        body = transactions.write_order("EDIFACT", US, SUPPLIER, ORDER, LINES,
                                        WHEN)
        self.check(wrap("EDIFACT", "ORDERS", body))

    def test_an_860_validates(self):
        body = transactions.write_change("X12", US, SUPPLIER, ORDER,
                                         a_change(), WHEN)
        self.check(wrap("X12", "860", body, "PC"))

    def test_an_ordchg_validates(self):
        body = transactions.write_change("EDIFACT", US, SUPPLIER, ORDER,
                                         a_change(), WHEN)
        self.check(wrap("EDIFACT", "ORDCHG", body))

    def test_a_cancellation_validates_in_both_dialects(self):
        for dialect, code, group in (("X12", "860", "PC"),
                                     ("EDIFACT", "ORDCHG", "")):
            with self.subTest(dialect=dialect):
                body = transactions.write_change(dialect, US, SUPPLIER, ORDER,
                                                 a_change("01"), WHEN)
                self.check(wrap(dialect, code, body, group))

    def test_an_order_of_one_line_with_nothing_optional_validates(self):
        lines = [{"line": "1", "sku": "W", "quantity": "1", "uom": "EA"}]
        order = {"po_number": "PO-BARE"}
        for dialect, code, group in (("X12", "850", "PO"),
                                     ("EDIFACT", "ORDERS", "")):
            with self.subTest(dialect=dialect):
                body = transactions.write_order(dialect, US, SUPPLIER, order,
                                                lines, WHEN)
                self.check(wrap(dialect, code, body, group))


class WhatTheSellerSideStillDoes(unittest.TestCase):
    """The writers that already existed have not moved.

    `_x12_parties` still writes the mock as the seller, and these two are new
    rather than a change to it. A regression here would mean the flip leaked.
    """

    def test_a_response_still_names_the_mock_as_the_seller(self):
        us = Party(role="SE", name="Mock EDI Supply Co", identifier="MOCKEDI")
        partner = {"id": "ACME", "name": "Acme Distribution Inc"}
        order = {"po_number": "PO-1", "ordered_on": "2026-09-24",
                 "currency": "USD", "seller_order": "5100002"}
        lines = [{"line": "1", "sku": "W", "quantity": "1", "uom": "EA",
                  "price": "1.00", "confirmed": "1", "status": "IA"}]
        body = transactions.write_response("X12", us, partner, order, lines,
                                           WHEN)
        roles = [(item.get(1), item.get(4)) for item in body
                 if item.tag == "N1"]
        self.assertIn(("SE", "MOCKEDI"), roles)


if __name__ == "__main__":
    unittest.main()
