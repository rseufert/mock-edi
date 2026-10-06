"""The X12 date qualifiers the mock names, writes and reads (#229).

Element 374 says what kind of date a `DTM` or an `ACK` carries. The
dictionary had two of them wrong: 068 carried 067's name, and 137 carried
the meaning it has in EDIFACT, where it is the date of a message. In X12 it
is a supplier's delivery rating. The 855 and 865 wrote `DTM*137` as their
own date, and read it back the same way, so the mock agreed with itself and
with nobody else.

The names here are the standard's, from four published copies that agree:
Stedi's 004010 reference for element 374, and the code lists in
tcd/eddy, mjpete3/x12 and ediflow-lib/core. They are written out, not read
from `schema`, so that a name cannot drift from its code without a test
saying so.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import schema, transactions, x12
from mockedi.envelope import seg

from support import ACME, MockServerCase, x12_change, x12_order

STANDARD = {
    "002": "Delivery Requested", "003": "Invoice", "010": "Requested Ship",
    "011": "Shipped", "017": "Estimated Delivery", "035": "Delivered",
    "037": "Ship Not Before", "038": "Ship No Later",
    "067": "Current Schedule Delivery", "068": "Current Schedule Ship",
    "097": "Transaction Creation", "118": "Requested Pick-up",
    "137": "Delivery Rating",
}

# Every date qualifier the mock itself writes in an X12 document, and what
# the date beside it is.
WRITTEN = {
    "002": "Delivery Requested",          # an 850 the mock places
    "011": "Shipped",                     # the 856, and the 810's ship date
    "067": "Current Schedule Delivery",   # a line's date in an 855 or 865
}


def qualifiers(messages):
    """Every date qualifier written in a DTM01 or an ACK04 of these messages."""
    written = set()
    for body in messages:
        for item in body.segments:
            if item.tag == "DTM":
                written.add(item.get(1))
            elif item.tag == "ACK" and item.get(4):
                written.add(item.get(4))
    return written


def message(code, body):
    return x12.message(code, "0001", body)


class TheNamesAreTheStandards(unittest.TestCase):

    def test_every_code_the_dictionary_lists_has_the_standards_name(self):
        self.assertEqual(schema.DATE_QUALIFIER_CODES, STANDARD)

    def test_the_two_that_were_wrong(self):
        codes = schema.DATE_QUALIFIER_CODES
        self.assertEqual(codes["068"], "Current Schedule Ship")
        self.assertEqual(codes["067"], "Current Schedule Delivery")
        self.assertEqual(codes["137"], "Delivery Rating")
        self.assertEqual(codes["097"], "Transaction Creation")

    def test_the_dictionary_endpoint_serves_them(self):
        definition = schema.lookup("X12", "855")
        dtm = definition.segment_for("DTM")
        self.assertEqual(dtm.element(1).codes, STANDARD)
        ack = definition.segment_for("ACK")
        self.assertEqual(ack.element(4).codes, STANDARD)


class WhatTheMockWrites(MockServerCase):

    def setUp(self):
        super().setUp()
        self.send(x12_order("DATE-229"))

    def body(self, kind):
        return self.document(ACME, kind).groups[0].messages[0]

    def header(self, kind, stop):
        out = []
        for item in self.body(kind).segments:
            if item.tag == stop:
                break
            out.append(item)
        return out

    def test_the_855_carries_its_date_in_bak09_and_in_no_dtm(self):
        header = self.header("response", "PO1")
        self.assertEqual([item.tag for item in header if item.tag == "DTM"], [])
        bak = self.body("response").find("BAK")
        isa = self.mailbox(ACME, "response")[0]["payload"].split("*")
        self.assertEqual(bak.get(9), "20" + isa[9])      # the day it was sent

    def test_an_acknowledged_line_is_dated_with_067(self):
        acks = [item for item in self.body("response").segments
                if item.tag == "ACK"]
        self.assertTrue(acks)
        self.assertEqual({item.get(4) for item in acks}, {"067"})
        self.assertTrue(all(len(item.get(5)) == 8 for item in acks))

    def test_every_qualifier_it_writes_means_what_the_date_is(self):
        written = qualifiers(self.body(kind)
                             for kind in ("response", "despatch", "invoice"))
        self.assertEqual(written, {"011", "067"})
        for code in written:
            self.assertEqual(schema.DATE_QUALIFIER_CODES[code], WRITTEN[code])

    def test_it_writes_neither_of_the_codes_it_had_wrong(self):
        for row in self.mailbox(ACME):
            with self.subTest(code=row["code"]):
                self.assertNotIn("DTM*137", row["payload"])
                self.assertNotIn("*068*", row["payload"])

    def test_its_own_855_reads_back_with_the_date_it_was_given(self):
        body = self.body("response")
        response = transactions.read_response(body, "X12")
        self.assertEqual(response.responded_on.strftime("%Y%m%d"),
                         body.find("BAK").get(9))
        self.assertIsNotNone(response.lines[0].scheduled_on)

    def test_the_segment_count_is_right_without_the_dtm(self):
        for kind in ("response",):
            payload = self.mailbox(ACME, kind)[0]["payload"]
            said = self.mock.findings(payload)
            self.assertEqual(len(said), 1, said)
            self.assertTrue(said[0].endswith(": accepted"), said)


class TheChangeAcknowledgment(MockServerCase):
    """An 865, for an order changed before it shipped."""
    config_kwargs = {"despatch_delay_ms": 3600 * 1000,
                     "invoice_delay_ms": 7200 * 1000}

    def setUp(self):
        super().setUp()
        self.send(x12_order("DATE-865"))
        self.send(x12_change("DATE-865", [("1", "CA", 60, "12.50")]))
        self.body = self.document(ACME, "change-response").groups[0].messages[0]

    def test_it_carries_its_date_in_bca10_and_in_no_dtm(self):
        header = []
        for item in self.body.segments:
            if item.tag == "POC":
                break
            header.append(item)
        self.assertEqual([item.tag for item in header if item.tag == "DTM"], [])
        isa = self.mailbox(ACME, "change-response")[0]["payload"].split("*")
        self.assertEqual(self.body.find("BCA").get(10), "20" + isa[9])

    def test_its_lines_are_dated_with_067(self):
        self.assertEqual(qualifiers([self.body]), {"067"})

    def test_it_writes_neither_of_the_codes_it_had_wrong(self):
        payload = self.mailbox(ACME, "change-response")[0]["payload"]
        self.assertNotIn("DTM*137", payload)
        self.assertNotIn("*068*", payload)

    def test_it_reads_back_with_the_date_it_was_given(self):
        response = transactions.read_change_response(self.body, "X12")
        self.assertEqual(response.responded_on.strftime("%Y%m%d"),
                         self.body.find("BCA").get(10))

    def test_it_is_clean_by_the_mocks_own_dictionary(self):
        said = self.mock.findings(
            self.mailbox(ACME, "change-response")[0]["payload"])
        self.assertEqual(len(said), 1, said)
        self.assertTrue(said[0].endswith(": accepted"), said)


class WhatThePurchaseOrderItPlacesSays(MockServerCase):

    def test_the_requested_date_is_002(self):
        self.post("/_mock/partners", {"id": "SELLCO", "name": "Sell Co",
                                      "role": "supplier"})
        self.post("/_mock/purchase", {
            "partner": "SELLCO", "po_number": "DATE-BUY",
            "requested_on": "2026-12-01",
            "lines": [{"sku": "WIDGET-001", "quantity": "5", "uom": "EA",
                       "price": "12.50"}]})
        payload = self.mailbox("SELLCO")[0]["payload"]
        dtm = [line for line in payload.replace("~", "\n").splitlines()
               if line.startswith("DTM*")]
        self.assertEqual(dtm, ["DTM*002*20261201"])


class WhatItStillReads(unittest.TestCase):
    """Strict about what it writes, and not about what a partner sends."""

    def response(self, body):
        return transactions.read_response(message("855", body), "X12")

    LINE = [seg("PO1", "1", "10", "EA", "1.00", "", "VP", "A")]

    def test_bak09_is_the_date_when_it_is_there(self):
        response = self.response([
            seg("BAK", "00", "AD", "PO-1", "20260101", "", "", "", "", "20260107"),
            seg("DTM", "097", "20260105")] + self.LINE)
        self.assertEqual(response.responded_on, datetime.date(2026, 1, 7))

    def test_a_header_dtm_097_is_the_date_when_bak09_is_empty(self):
        response = self.response([
            seg("BAK", "00", "AD", "PO-2", "20260101"),
            seg("DTM", "097", "20260105")] + self.LINE)
        self.assertEqual(response.responded_on, datetime.date(2026, 1, 5))

    def test_so_is_137_which_this_mock_wrote_before(self):
        response = self.response([
            seg("BAK", "00", "AD", "PO-3", "20260101"),
            seg("DTM", "137", "20260105")] + self.LINE)
        self.assertEqual(response.responded_on, datetime.date(2026, 1, 5))

    def test_another_dtm_is_not_mistaken_for_the_documents_date(self):
        response = self.response([
            seg("BAK", "00", "AD", "PO-4", "20260101"),
            seg("DTM", "002", "20260220")] + self.LINE)
        self.assertIsNone(response.responded_on)

    def test_a_line_dated_067_or_068_is_scheduled_then(self):
        for code in ("067", "068"):
            with self.subTest(code=code):
                response = self.response([
                    seg("BAK", "00", "AD", "PO-5", "20260101")] + self.LINE + [
                    seg("ACK", "IA", "10", "EA", code, "20260210")])
                self.assertEqual(response.lines[0].scheduled_on,
                                 datetime.date(2026, 2, 10))

    def test_an_850_asking_for_delivery_by_067_is_read_as_requesting_it(self):
        for code in ("002", "067", "068"):
            with self.subTest(code=code):
                order = transactions.read_order(message("850", [
                    seg("BEG", "00", "SA", "PO-6", "", "20260101"),
                    seg("DTM", code, "20260301"),
                    seg("PO1", "1", "10", "EA", "1.00", "", "VP", "A")]), "X12")
                self.assertEqual(order.requested_on, datetime.date(2026, 3, 1))


class TheEdifactSideIsNotTouched(MockServerCase):
    """137 is the right code there: 2005's document/message date."""

    def test_an_ordrsp_is_still_dated_with_137(self):
        from support import EURODIS, edifact_order
        self.send(edifact_order("DATE-EDI"))
        payload = self.mailbox(EURODIS, "response")[0]["payload"]
        self.assertIn("DTM+137:", payload)


if __name__ == "__main__":
    unittest.main()
