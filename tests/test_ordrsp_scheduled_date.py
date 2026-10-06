"""The date an ORDRSP puts on a confirmed line, and the code beside it (#305).

The seller's response carried the delivery date it had scheduled under 2005's
qualifier 2, "Date on which buyer requests goods to be delivered". So it said
"this is the date you asked for" where it meant "this is the date I have
scheduled", and a buyer reading it could not tell the seller's commitment
from its own request. It is the EDIFACT twin of #229, where the 855 wrote
the ship date's code for a delivery date.

67 is "Delivery date/time, current schedule: Delivery Date deriving from
actual schedule", in two published copies of D.96A's 2005 (edifactory.de and
Stylus Studio's), and the counterpart of X12's 067, which the 855 writes.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, schema, transactions
from mockedi.envelope import seg

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order


class TheDictionary(unittest.TestCase):

    def test_67_is_declared_with_the_directorys_name(self):
        self.assertEqual(schema.EDIFACT_DATE_QUALIFIERS["67"],
                         "Delivery date/time, current schedule")

    def test_2_still_means_what_the_buyer_asked_for(self):
        self.assertEqual(schema.EDIFACT_DATE_QUALIFIERS["2"],
                         "Delivery date/time, requested")


class WhatTheResponseSays(MockServerCase):

    def setUp(self):
        super().setUp()
        self.send(edifact_order("SCHED-EDI"))
        self.payload = self.mailbox(EURODIS, "response")[0]["payload"]
        self.message = next(edifact.parse(self.payload).messages())[1]

    def line_dates(self):
        out, in_lines = [], False
        for item in self.message.segments:
            if item.tag == "LIN":
                in_lines = True
            elif item.tag == "DTM" and in_lines:
                out.append((item.comp(1, 1), item.comp(1, 2)))
        return out

    def test_a_confirmed_lines_date_is_qualified_67(self):
        dates = self.line_dates()
        self.assertTrue(dates)
        self.assertEqual({qualifier for qualifier, _date in dates}, {"67"})
        self.assertTrue(all(len(date) == 8 for _qualifier, date in dates))

    def test_no_line_date_claims_the_buyer_requested_it(self):
        self.assertNotIn("DTM+2:", self.payload.split("LIN+", 1)[1])

    def test_it_is_the_date_the_order_line_was_scheduled_for(self):
        scheduled = self.order("SCHED-EDI")["lines"][0]["scheduled_on"]
        self.assertEqual(self.line_dates()[0][1], scheduled.replace("-", ""))

    def test_the_response_is_clean_by_the_mocks_own_dictionary(self):
        said = self.mock.findings(self.payload)
        self.assertEqual(len(said), 1, said)
        self.assertTrue(said[0].endswith(": accepted"), said)

    def test_it_reads_back_with_the_date(self):
        response = transactions.read_response(self.message, "EDIFACT")
        scheduled = self.order("SCHED-EDI")["lines"][0]["scheduled_on"]
        self.assertEqual(response.lines[0].scheduled_on.isoformat(), scheduled)

    def test_the_two_dialects_now_say_the_same_thing_about_that_date(self):
        self.send(x12_order("SCHED-X12"))
        x12_payload = self.mailbox(ACME, "response")[0]["payload"]
        ack04 = [line.split("*")[4] for line in
                 x12_payload.replace("~", "\n").splitlines()
                 if line.startswith("ACK*")]
        named = {schema.DATE_QUALIFIER_CODES[code].lower() for code in ack04}
        # X12's name for what 2005 calls "Delivery date/time, current schedule".
        self.assertEqual(named & {"current schedule delivery"}, named)


class WhatIsStillRead(unittest.TestCase):
    """A buyer, or an older mock, that dates the line another way."""

    def scheduled(self, *dates):
        body = [seg("BGM", ["231"], "RSP-1", "29"),
                seg("RFF", ["ON", "PO-1"]),
                seg("LIN", "1", "", ["A", "VP"]),
                seg("QTY", ["21", "10", "PCE"])]
        body += [seg("DTM", [code, value, "102"]) for code, value in dates]
        response = transactions.read_response(
            edifact.message("ORDRSP", "1", body), "EDIFACT")
        return response.lines[0].scheduled_on

    def test_each_of_67_2_and_17_is_the_lines_date(self):
        for code in ("67", "2", "17"):
            with self.subTest(code=code):
                self.assertEqual(self.scheduled((code, "20261201")),
                                 datetime.date(2026, 12, 1))

    def test_67_is_preferred_when_a_line_carries_the_request_as_well(self):
        self.assertEqual(
            self.scheduled(("2", "20261125"), ("67", "20261201")),
            datetime.date(2026, 12, 1))


class TheOrderTheMockPlaces(MockServerCase):
    """There it is the buyer, and the date really is what it requests."""

    def test_its_requested_date_is_still_qualified_2(self):
        self.post("/_mock/partners", {
            "id": "SELLE", "name": "Sell E", "role": "supplier",
            "dialect": "EDIFACT", "version": "D:96A:UN"})
        self.post("/_mock/purchase", {
            "partner": "SELLE", "po_number": "SCHED-BUY",
            "requested_on": "2026-12-01",
            "lines": [{"sku": "WIDGET-001", "quantity": "5", "uom": "EA",
                       "price": "12.50"}]})
        payload = self.mailbox("SELLE")[0]["payload"]
        self.assertIn("DTM+2:20261201:102'", payload)
        self.assertNotIn("DTM+67:", payload)


if __name__ == "__main__":
    unittest.main()
