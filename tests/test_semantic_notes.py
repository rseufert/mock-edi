"""Every X12 position the standard gives a meaning to, against what the mock puts there.

#178 happened because the 860 and 865 put their dates where the element
*names* suggested. Element 373 is "Date" wherever it appears; which date it is
comes from the segment's semantic notes, by position - and the mock's own
tests round-tripped happily, because its readers had made the same mistake.

So the expectations here are written from the notes and from nothing else.
Each test states, by hand, what the note says a position holds, then checks
that a document the mock writes carries exactly that there and that the
reader takes it from there. Nothing here asks `schema.py` what a position
means: a test that did would agree with the dictionary and prove nothing.

The notes were taken from published 004010 implementation guides, two
publishers or more for each, listed position by position in the table on
#189. A few of the standard's *comments* give a position its meaning as well
(PO101, the CTT01 counts) and are checked the same way, marked as comments.

Every date below is different, so a value in the wrong position is the wrong
value and not a coincidence.

Two positions still disagree and are marked as expected failures, each naming
the issue that fixes it: #231 and #232. (#230, the 856 reader taking BSN03 as
the ship date, is fixed.)
"""
import datetime
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, remittance, transactions, validate, x12
from mockedi.envelope import seg
from mockedi.transactions import Change, ChangeLine, Party

ORDERED = "20260901"        # the date the purchaser gave the order
CHANGED = "20260903"        # the date of the change request
WRITTEN = "20260905"        # the day the document in hand is written
SHIPPED = "20260908"        # the day the goods left
INVOICED = "20260910"       # the invoice's own date
SETTLES = "20260925"        # when the payment takes effect
WHEN = datetime.datetime(2026, 9, 5, 10, 30)


def day(text):
    return datetime.datetime.strptime(text, "%Y%m%d").date()


def iso(text):
    return day(text).isoformat()


US = Party(role="SE", name="Mock EDI Supply Co", identifier="MOCKEDI")
PARTNER = {"id": "ACME", "name": "Acme Distribution Inc"}
ORDER = {"po_number": "PO-189", "ordered_on": iso(ORDERED), "currency": "USD",
         "seller_order": "SO-5100189", "ship_to_name": "Acme DC 4",
         "ship_to_id": "ACME-DC4"}
LINES = [
    {"line": "10", "sku": "WIDGET-001", "quantity": "8", "uom": "EA",
     "price": "12.50", "confirmed": "8", "status": "IA", "shipped": "8",
     "invoiced": "8", "scheduled_on": "2026-09-20",
     "description": "Widget, blue, 40mm"},
    {"line": "20", "sku": "GADGET-002", "quantity": "3", "uom": "EA",
     "price": "40.00", "confirmed": "3", "status": "IA", "shipped": "3",
     "invoiced": "3", "scheduled_on": "2026-09-20"},
]
SHIPMENT = {"shipment_id": "SHP-77", "shipped_on": iso(SHIPPED), "cartons": 2,
            "weight": 14, "scac": "UPSN", "carrier": "UPS Ground",
            "bol": "BOL-9"}
INVOICE = {"invoice_number": "INV-55", "invoiced_on": iso(INVOICED),
           "currency": "USD", "total": "236.50", "tax": "16.50",
           "terms_days": 30}
CHANGE = Change(po_number="PO-189", purpose="04", sequence="2",
                ordered_on=day(ORDERED), changed_on=day(CHANGED),
                lines=[ChangeLine(number="10", sku="WIDGET-001",
                                  quantity=Decimal("6"), uom="EA",
                                  price=Decimal("12.50"), action="QD")])


def find(body, tag):
    return [item for item in body if item.tag == tag]


def one(body, tag):
    return find(body, tag)[0]


def message(code, body):
    return x12.message(code, "0001", body)


class Written(unittest.TestCase):
    """The documents the mock writes, built once."""

    @classmethod
    def setUpClass(cls):
        cls.po = transactions.write_order("X12", US, PARTNER, ORDER, LINES, WHEN)
        cls.change = transactions.write_change("X12", US, PARTNER, ORDER,
                                               CHANGE, WHEN)
        cls.response = transactions.write_response("X12", US, PARTNER, ORDER,
                                                   LINES, WHEN)
        cls.change_response = transactions.write_change_response(
            "X12", US, PARTNER, ORDER,
            [dict(LINES[0], change_action="QD")], CHANGE, WHEN)
        cls.despatch = transactions.write_despatch("X12", US, PARTNER, ORDER,
                                                   LINES, SHIPMENT, WHEN)
        cls.invoice = transactions.write_invoice("X12", US, PARTNER, ORDER,
                                                 LINES, INVOICE, SHIPMENT, WHEN)


class BEG(Written):
    """BEG05 is the date the purchaser assigned to the purchase order."""

    def test_written(self):
        self.assertEqual(one(self.po, "BEG").get(5), ORDERED)

    def test_read(self):
        order = transactions.read_order(message("850", [
            seg("BEG", "00", "SA", "PO-1", "", ORDERED),
            seg("PO1", "1", "1", "EA", "1.00", "", "VP", "W"),
            seg("CTT", "1")]), "X12")
        self.assertEqual(order.ordered_on, day(ORDERED))


class BCH(Written):
    """BCH06 the purchaser's date for the order, BCH09 the seller's order
    number, BCH10 the date the sender gave the acknowledgment, BCH11 the date
    of the change request."""

    def test_written(self):
        bch = one(self.change, "BCH")
        self.assertEqual(bch.get(6), ORDERED)
        self.assertEqual(bch.get(11), CHANGED)
        # An 860 is a request, not an acknowledgment: it has no such date.
        self.assertEqual(bch.get(10), "")

    def read(self):
        return transactions.read_change(message("860", [
            seg("BCH", "04", "SA", "PO-1", "", "2", ORDERED, "", "", "SO-9",
                WRITTEN, CHANGED),
            seg("POC", "10", "QD", "6", "", "EA", "12.50", "", "VP", "W"),
            seg("CTT", "1")]), "X12")

    def test_read(self):
        change = self.read()
        self.assertEqual(change.ordered_on, day(ORDERED))
        self.assertEqual(change.changed_on, day(CHANGED))

    @unittest.expectedFailure       # #232: BCH09 is not read
    def test_the_sellers_order_number_is_read_from_bch09(self):
        self.assertEqual(getattr(self.read(), "seller_order", None), "SO-9")


class BAK(Written):
    """BAK04 the purchaser's date for the order, BAK08 the seller's order
    number, BAK09 the date the sender assigned to the acknowledgment."""

    def test_written(self):
        bak = one(self.response, "BAK")
        self.assertEqual(bak.get(4), ORDERED)
        self.assertEqual(bak.get(8), "SO-5100189")
        self.assertEqual(bak.get(9), WRITTEN)

    def test_read(self):
        response = transactions.read_response(message("855", [
            seg("BAK", "00", "AD", "PO-1", ORDERED, "", "", "", "SO-9", WRITTEN),
            seg("PO1", "1", "1", "EA", "1.00", "", "VP", "W"),
            seg("ACK", "IA", "1", "EA"),
            seg("CTT", "1")]), "X12")
        self.assertEqual(response.ordered_on, day(ORDERED))
        self.assertEqual(response.seller_order, "SO-9")
        self.assertEqual(response.responded_on, day(WRITTEN))


class BCA(Written):
    """BCA06 the purchaser's date for the order, BCA09 the seller's order
    number, BCA10 the date the sender assigned to the acknowledgment, BCA11
    the date of the change request."""

    def test_written(self):
        bca = one(self.change_response, "BCA")
        self.assertEqual(bca.get(6), ORDERED)
        self.assertEqual(bca.get(10), WRITTEN)
        self.assertEqual(bca.get(11), CHANGED)

    @unittest.expectedFailure       # #232: written only in REF*VN
    def test_the_sellers_order_number_is_written_in_bca09(self):
        self.assertEqual(one(self.change_response, "BCA").get(9), "SO-5100189")

    def read(self):
        return transactions.read_change_response(message("865", [
            seg("BCA", "00", "AC", "PO-1", "", "2", ORDERED, "", "", "SO-9",
                WRITTEN, CHANGED),
            seg("POC", "10", "QD", "6", "", "EA", "12.50", "", "VP", "W"),
            seg("ACK", "IA", "6", "EA"),
            seg("CTT", "1")]), "X12")

    def test_read(self):
        response = self.read()
        self.assertEqual(response.ordered_on, day(ORDERED))
        self.assertEqual(response.responded_on, day(WRITTEN))

    @unittest.expectedFailure       # #232: BCA09 is not read
    def test_the_sellers_order_number_is_read_from_bca09(self):
        self.assertEqual(self.read().seller_order, "SO-9")


class BSN(Written):
    """BSN03 is the date the ship notice was created, BSN04 the time.

    Not the day the goods left: that is a DTM. A notice written the day after
    is a day out for any reader that takes one for the other.
    """

    def test_written(self):
        bsn = one(self.despatch, "BSN")
        self.assertEqual(bsn.get(3), WRITTEN)
        self.assertEqual(bsn.get(4), "1030")
        self.assertNotEqual(bsn.get(3), SHIPPED)
        self.assertIn(("011", SHIPPED), [(item.get(1), item.get(2))
                                         for item in find(self.despatch, "DTM")])

    def read(self, *dated):
        return transactions.read_despatch(message("856", [
            seg("BSN", "00", "SHP-1", WRITTEN, "1030", "0004"),
            seg("HL", "1", "", "S", "1"), *dated,
            seg("HL", "2", "1", "O", "1"),
            seg("PRF", "PO-1", "", "", ORDERED),
            seg("HL", "3", "2", "I", "0"),
            seg("LIN", "10", "VP", "W"),
            seg("SN1", "10", "8", "EA"),
            seg("CTT", "3")]), "X12")

    def test_the_ship_date_is_read_from_the_date_that_says_shipped(self):
        self.assertEqual(self.read(seg("DTM", "011", SHIPPED)).shipped_on,
                         day(SHIPPED))

    def test_the_date_the_notice_was_written_is_not_a_ship_date(self):
        despatch = self.read()
        self.assertIsNone(despatch.shipped_on)
        self.assertEqual(despatch.written_on, day(WRITTEN))


class PRF(Written):
    """PRF04 is the date the purchaser assigned to the purchase order."""

    def test_written(self):
        prf = one(self.despatch, "PRF")
        self.assertEqual((prf.get(1), prf.get(4)), ("PO-189", ORDERED))

    def test_read(self):
        despatch = transactions.read_despatch(message("856", [
            seg("BSN", "00", "SHP-1", WRITTEN, "1030", "0004"),
            seg("HL", "1", "", "S", "1"),
            seg("HL", "2", "1", "O", "1"),
            seg("PRF", "PO-1", "", "", ORDERED),
            seg("CTT", "2")]), "X12")
        self.assertEqual(despatch.ordered_on, day(ORDERED))


class BIG(Written):
    """BIG01 is the date the invoice was issued, BIG03 the purchaser's date
    for the order."""

    def test_written(self):
        big = one(self.invoice, "BIG")
        self.assertEqual((big.get(1), big.get(3)), (INVOICED, ORDERED))

    def test_read(self):
        invoice = transactions.read_invoice(message("810", [
            seg("BIG", INVOICED, "INV-1", ORDERED, "PO-1"),
            seg("IT1", "10", "1", "EA", "1.00", "", "VP", "W"),
            seg("TDS", "100"), seg("CTT", "1")]), "X12")
        self.assertEqual(invoice.invoiced_on, day(INVOICED))
        self.assertEqual(invoice.ordered_on, day(ORDERED))


class TheLineIdentification(Written):
    """POC01 and IT101 are the purchase order's line item identification,
    SN101 the ship notice's, LIN01 the line item's. PO101 is given the same
    meaning by a comment rather than a semantic note.

    The lines are numbered 10 and 20 so that a writer counting them 1, 2
    instead of carrying the order's own numbers is caught.
    """

    def test_written(self):
        for name, body, tag in (("850", self.po, "PO1"),
                                ("855", self.response, "PO1"),
                                ("856", self.despatch, "LIN"),
                                ("856", self.despatch, "SN1"),
                                ("810", self.invoice, "IT1")):
            with self.subTest(set=name, segment=tag):
                self.assertEqual([item.get(1) for item in find(body, tag)],
                                 ["10", "20"])
        for name, body in (("860", self.change), ("865", self.change_response)):
            with self.subTest(set=name, segment="POC"):
                self.assertEqual([item.get(1) for item in find(body, "POC")],
                                 ["10"])

    def test_read(self):
        order = transactions.read_order(message("850", self.po), "X12")
        self.assertEqual([line.number for line in order.lines], ["10", "20"])
        change = transactions.read_change(message("860", self.change), "X12")
        self.assertEqual([line.number for line in change.lines], ["10"])
        response = transactions.read_response(
            message("855", self.response), "X12")
        self.assertEqual([line.number for line in response.lines], ["10", "20"])
        despatch = transactions.read_despatch(
            message("856", self.despatch), "X12")
        self.assertEqual([item.line for item in despatch.items], ["10", "20"])
        invoice = transactions.read_invoice(message("810", self.invoice), "X12")
        self.assertEqual([line.number for line in invoice.lines], ["10", "20"])


class CTT(Written):
    """From each set's own notes (comments, not segment notes): CTT01 counts
    the PO1 segments in an 850 and 855, the POC segments in an 860 and 865,
    and the HL segments in an 856."""

    def test_written(self):
        for name, body, counted in (("850", self.po, "PO1"),
                                    ("855", self.response, "PO1"),
                                    ("860", self.change, "POC"),
                                    ("865", self.change_response, "POC"),
                                    ("856", self.despatch, "HL")):
            with self.subTest(set=name):
                self.assertEqual(one(body, "CTT").get(1),
                                 str(len(find(body, counted))))

    def test_an_856_does_not_count_its_lines(self):
        # Two items under a shipment and an order: four HLs, not two lines.
        self.assertEqual(one(self.despatch, "CTT").get(1), "4")


class TDS(Written):
    """TDS01 is the invoice total, including charges and less allowances,
    before any terms discount; TDS02 the amount the terms discount is worked
    out on. Both carry two implied decimals."""

    def test_written(self):
        # 236.50, tax included: tax is a charge on the invoice.
        self.assertEqual(one(self.invoice, "TDS").get(1), "23650")

    def test_read(self):
        invoice = transactions.read_invoice(message("810", [
            seg("BIG", INVOICED, "INV-1", ORDERED, "PO-1"),
            seg("IT1", "10", "1", "EA", "200.00", "", "VP", "W"),
            seg("TDS", "23650", "20000"), seg("CTT", "1")]), "X12")
        self.assertEqual(invoice.total, Decimal("236.50"))
        # The amount subject to discount, kept apart from the total.
        self.assertEqual(invoice.subtotal, Decimal("200.00"))


class TXI(Written):
    """TXI02 is the monetary amount of the tax."""

    def test_written(self):
        self.assertEqual(one(self.invoice, "TXI").get(2), "16.50")

    def test_read(self):
        invoice = transactions.read_invoice(message("810", [
            seg("BIG", INVOICED, "INV-1", ORDERED, "PO-1"),
            seg("IT1", "10", "1", "EA", "200.00", "", "VP", "W"),
            seg("TDS", "21650"), seg("TXI", "ST", "16.50", "8.25"),
            seg("CTT", "1")]), "X12")
        self.assertEqual(invoice.tax, Decimal("16.50"))


class SAC(unittest.TestCase):
    """SAC05 is the total amount of the allowance or charge, and SAC01 says
    which of the two it is."""

    def test_read(self):
        invoice = transactions.read_invoice(message("810", [
            seg("BIG", INVOICED, "INV-1", ORDERED, "PO-1"),
            seg("IT1", "10", "1", "EA", "200.00", "", "VP", "W"),
            seg("TDS", "20750"),
            seg("SAC", "C", "D240", "", "", "1250"),
            seg("SAC", "A", "C310", "", "", "500"),
            seg("CTT", "1")]), "X12")
        self.assertEqual(invoice.charges, Decimal("7.50"))


class TheRemittanceAdvice(unittest.TestCase):
    """BPR02 is the payment amount and BPR16 the date the payer intends it to
    settle; TRN02 identifies the transaction; RMR04 is the amount paid against
    the item RMR02 names; ADX01 is an adjustment, signed, a negative one
    reducing the payment."""

    def advice(self, paid="900.00"):
        return message("820", [
            seg("BPR", "I", paid, "C", "ACH", "", "", "", "", "", "", "", "",
                "", "", "", SETTLES),
            seg("TRN", "1", "TRACE-42", "1234567890"),
            seg("ENT", "1"),
            # At the entity's level, before its items: an adjustment to the
            # payment, not a detail of one invoice.
            seg("ADX", "-50.00", "01"),
            seg("RMR", "IV", "INV-1", "", "600.00", "640.00", "40.00"),
            seg("RMR", "IV", "INV-2", "", "350.00")])

    def test_read(self):
        advice = remittance.read(self.advice(), "X12")
        self.assertEqual(advice.total, Decimal("900.00"))
        self.assertEqual(advice.settles, day(SETTLES))
        self.assertEqual(advice.trace, "TRACE-42")
        # The amount paid, RMR04 - not the invoice's own amount in RMR05.
        self.assertEqual(advice.invoices, [("INV-1", Decimal("600.00")),
                                           ("INV-2", Decimal("350.00"))])

    def test_a_negative_adjustment_reduces_the_payment(self):
        # 600 + 350 - 50 is the 900 that BPR02 says: nothing to report.
        self.assertEqual(remittance.findings(self.advice(), "X12",
                                             "remittance", "1"), [])
        # And without the adjustment counted, it would not add up.
        found = remittance.findings(self.advice(paid="950.00"), "X12",
                                    "remittance", "1")
        self.assertEqual([item.expected for item in found], ["900.00"])


class The997(unittest.TestCase):
    """AK101 is the functional ID from GS01 and AK102 the group control
    number from the GS; AK201 is the set ID from ST01 and AK202 the control
    number from the ST. AK404 must never hold a value that would itself be a
    syntax error."""

    def acknowledge(self, quantity="8"):
        interchange = x12.parse(x12.render(x12.wrap(
            [x12.message("850", "0042", [
                seg("BEG", "00", "SA", "PO-1", "", ORDERED),
                seg("PO1", "1", quantity, "EA", "1.00", "", "VP", "W"),
                seg("CTT", "1")])],
            "ACME", "MOCKEDI", "000000007", "613", "PO")))
        report = validate.validate(interchange)
        body = []
        for functional_id, control, version, messages in ack.group_reports(
                interchange, report):
            body.extend(ack.functional_acknowledgment(
                functional_id, control, version, messages))
        return body

    def test_written(self):
        body = self.acknowledge()
        self.assertEqual(one(body, "AK1").elements, ["PO", "613"])
        self.assertEqual(one(body, "AK2").elements, ["850", "0042"])

    def test_ak404_copies_the_bad_data_when_it_can_be_carried(self):
        self.assertEqual(one(self.acknowledge("eight"), "AK4").get(4), "eight")

    @unittest.expectedFailure       # #231: the invalid character is copied
    def test_ak404_does_not_repeat_an_invalid_character(self):
        ak4 = one(self.acknowledge("1\x012"), "AK4")
        self.assertEqual(ak4.get(3), "6")
        self.assertNotIn("\x01", ak4.get(4))


class PID(Written):
    """From the standard's comment: when PID01 is F the description is free
    form, and it is PID05 that carries it."""

    def test_written(self):
        for name, body in (("850", self.po), ("855", self.response),
                           ("810", self.invoice)):
            with self.subTest(set=name):
                pid = one(body, "PID")
                self.assertEqual((pid.get(1), pid.get(5)),
                                 ("F", "Widget, blue, 40mm"))

    def test_read(self):
        order = transactions.read_order(message("850", self.po), "X12")
        self.assertEqual(order.lines[0].description, "Widget, blue, 40mm")


class PositionsWithANoteTheMockDoesNotUse(Written):
    """The standard gives each of these a meaning the mock has nothing to put
    there for. They stay empty, so that whoever first writes one is sent to
    the note for it and not to the element's name.

    REF04 (data relating to REF02), ACK29 (an industry reason code), ITD15 (a
    late-payment percentage), TD515 (the country of the service), BSN06 (a
    shipment-related code), BIG10 (a consolidated invoice number), TXI03 (the
    tax percent) and TXI07, PID03, PID04, PID08 and PID09.
    """
    UNUSED = {"REF": (4,), "ACK": (29,), "ITD": (15,), "TD5": (15,),
              "BSN": (6,), "BIG": (10,), "TXI": (3, 7), "PID": (3, 4, 8, 9)}

    def test_they_are_empty_in_everything_the_mock_writes(self):
        documents = (self.po, self.change, self.response, self.change_response,
                     self.despatch, self.invoice)
        seen = set()
        for body in documents:
            for item in body:
                for position in self.UNUSED.get(item.tag, ()):
                    seen.add(item.tag)
                    with self.subTest(segment=item.tag, position=position):
                        self.assertEqual(item.get(position), "")
        # Each of them was actually written somewhere, or nothing was checked.
        self.assertEqual(seen, set(self.UNUSED))


if __name__ == "__main__":
    unittest.main()
