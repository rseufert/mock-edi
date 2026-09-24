"""Reading an order: the same business document out of two very different wires."""
import datetime
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, transactions, x12
from mockedi.envelope import seg


def read_x12(body):
    message = x12.parse(x12.render(x12.wrap(
        [x12.message("850", "0001", body)], "ACME", "MOCKEDI", "1", "1", "PO")
    )).groups[0].messages[0]
    return transactions.read_order(message, "X12")


def read_edifact(body):
    message = edifact.parse(edifact.render(edifact.wrap(
        [edifact.message("ORDERS", "1", body)], "EURODIS", "MOCKEDI", "1")
    )).groups[0].messages[0]
    return transactions.read_order(message, "EDIFACT")


X12_BODY = [
    seg("BEG", "00", "SA", "4500000900", "", "20260924"),
    seg("CUR", "BY", "GBP"),
    seg("DTM", "002", "20261010"),
    seg("N1", "ST", "Acme DC 4", "92", "ACME-DC4"),
    seg("N3", "9 Dock Road"),
    seg("N4", "Columbus", "OH", "43217", "US"),
    seg("PO1", "1", "100", "EA", "12.50", "", "VP", "WIDGET-001", "UP", "076123400003"),
    seg("PID", "F", "", "", "", "Widget, blue"),
    seg("PO1", "2", "40", "CA", "4.15", "", "VP", "BRKT-050"),
    seg("CTT", "2"),
]

EDIFACT_BODY = [
    seg("BGM", ["220"], ["PO-2026-00900"], "9"),
    seg("DTM", ["137", "20260924", "102"]),
    seg("DTM", ["2", "20261010", "102"]),
    seg("NAD", "DP", ["ACME-DC4", "", "92"], "", ["Acme DC 4"], ["9 Dock Road"],
        "Columbus", "OH", "43217", "US"),
    seg("CUX", ["2", "GBP", "9"]),
    seg("LIN", "1", "", ["WIDGET-001", "VP"]),
    seg("PIA", "1", ["076123400003", "UP"]),
    seg("IMD", "F", "", ["", "", "", "Widget, blue"]),
    seg("QTY", ["21", "100", "PCE"]),
    seg("PRI", ["AAA", "12.50"]),
    seg("LIN", "2", "", ["BRKT-050", "VP"]),
    seg("QTY", ["21", "40", "CT"]),
    seg("PRI", ["AAA", "4.15"]),
    seg("UNS", "S"),
]


class TheSameOrderInBothDialects(unittest.TestCase):
    def setUp(self):
        self.x12 = read_x12(X12_BODY)
        self.edifact = read_edifact(EDIFACT_BODY)

    def test_the_header_reads_alike(self):
        for order in (self.x12, self.edifact):
            self.assertEqual(order.currency, "GBP")
            self.assertEqual(order.ordered_on, datetime.date(2026, 9, 24))
            self.assertEqual(order.requested_on, datetime.date(2026, 10, 10))

    def test_the_purchase_order_number_comes_from_each_dialects_own_place(self):
        self.assertEqual(self.x12.po_number, "4500000900")
        self.assertEqual(self.edifact.po_number, "PO-2026-00900")

    def test_the_ship_to_address_reads_alike(self):
        for order in (self.x12, self.edifact):
            party = order.ship_to
            self.assertEqual(party.name, "Acme DC 4")
            self.assertEqual(party.identifier, "ACME-DC4")
            self.assertEqual(party.street, "9 Dock Road")
            self.assertEqual((party.city, party.region, party.postal),
                             ("Columbus", "OH", "43217"))

    def test_the_lines_read_alike_including_the_unit_translation(self):
        for order in (self.x12, self.edifact):
            self.assertEqual(len(order.lines), 2)
            first, second = order.lines
            self.assertEqual(first.sku, "WIDGET-001")
            self.assertEqual(first.upc, "076123400003")
            self.assertEqual(first.quantity, Decimal("100"))
            self.assertEqual(first.uom, "EA")
            self.assertEqual(first.price, Decimal("12.50"))
            self.assertEqual(first.description, "Widget, blue")
            self.assertEqual(second.uom, "CA")    # CT in EDIFACT, CA in X12

    def test_the_totals_agree(self):
        self.assertEqual(self.x12.total, self.edifact.total)
        self.assertEqual(self.x12.total, Decimal("1416.00"))


class ForgivingReading(unittest.TestCase):
    """Real partners send what their software sends."""

    def test_a_missing_line_number_falls_back_to_the_position(self):
        order = read_x12([X12_BODY[0],
                          seg("PO1", "", "5", "EA", "1.00", "", "VP", "A")])
        self.assertEqual(order.lines[0].number, "1")

    def test_any_of_the_qualifiers_that_mean_requested_delivery_will_do(self):
        for qualifier in ("002", "010", "038", "017"):
            order = read_x12([X12_BODY[0], seg("DTM", qualifier, "20261111")])
            self.assertEqual(order.requested_on, datetime.date(2026, 11, 11),
                             "DTM %s was not read" % qualifier)

    def test_an_item_sent_only_as_a_upc_is_not_mistaken_for_a_part_number(self):
        order = read_x12([X12_BODY[0],
                          seg("PO1", "1", "5", "EA", "1.00", "", "UP", "076123400003")])
        self.assertEqual(order.lines[0].upc, "076123400003")
        self.assertEqual(order.lines[0].sku, "")

    def test_an_unfamiliar_qualifier_is_taken_rather_than_the_line_refused(self):
        order = read_x12([X12_BODY[0],
                          seg("PO1", "1", "5", "EA", "1.00", "", "ZZ", "ODD-123")])
        self.assertEqual(order.lines[0].sku, "ODD-123")

    def test_a_missing_currency_defaults_rather_than_failing(self):
        order = read_x12([X12_BODY[0], X12_BODY[6]])
        self.assertEqual(order.currency, "USD")

    def test_a_ship_to_under_any_of_its_role_codes_is_found(self):
        for role in ("ST", "DP", "BY", "CN"):
            order = read_edifact([
                EDIFACT_BODY[0],
                seg("NAD", role, ["X1", "", "92"], "", ["Somewhere"])])
            self.assertEqual(order.ship_to.name, "Somewhere",
                             "NAD %s was not found" % role)

    def test_a_quantity_that_is_not_a_number_becomes_zero_not_an_exception(self):
        order = read_x12([X12_BODY[0],
                          seg("PO1", "1", "lots", "EA", "1.00", "", "VP", "A")])
        self.assertEqual(order.lines[0].quantity, Decimal("0"))


class WireFormats(unittest.TestCase):
    def test_a_quantity_loses_its_pointless_decimals(self):
        self.assertEqual(transactions.quantity_text(Decimal("10.00")), "10")
        self.assertEqual(transactions.quantity_text(Decimal("10.50")), "10.5")

    def test_a_price_keeps_exactly_two(self):
        self.assertEqual(transactions.price_text(Decimal("12.5")), "12.50")

    def test_tds_carries_an_integer_with_two_implied_decimals(self):
        self.assertEqual(transactions.implied_decimal(Decimal("125.00")), "12500")
        self.assertEqual(transactions.implied_decimal(Decimal("0.07")), "7")

    def test_units_translate_both_ways(self):
        from mockedi import schema
        for x12_code, edifact_code in (("EA", "PCE"), ("CA", "CT"), ("KG", "KGM")):
            self.assertEqual(schema.UOM_TO_EDIFACT[x12_code], edifact_code)
            self.assertEqual(schema.UOM_FROM_EDIFACT[edifact_code], x12_code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
