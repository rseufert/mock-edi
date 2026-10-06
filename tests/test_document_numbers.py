"""Which invoice, shipment or order a document on the timeline carries (#273).

A `packed` event names its shipment and an `invoiced` event its invoice. The
`sent` 856 and 810 beside them named neither, so a reader could say which
invoice an 810 was only by where it sat in the list, or by parsing it. These
hold the document events to the same fields the business events use, both
ways across the wire, and to the number the mock recorded when it wrote or
read the document.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import schema

from support import ACME, MockServerCase, parse, x12_change, x12_order
from test_three_way_match import MatchCase

# Despatched at once and invoiced an hour later: the window in which a change
# can still add a second consignment.
WINDOW = {"invoice_delay_ms": 3600 * 1000}
NUMBERS = ("order", "shipment", "invoice")


def events(found, event, code=None):
    return [item for item in found["events"] if item["event"] == event
            and (code is None or item.get("code") == code)]


def numbers(event):
    return {name: event[name] for name in NUMBERS if name in event}


class TimelineCase(MockServerCase):
    def timeline(self, po_number, partner=ACME):
        status, _headers, found = self.get(
            "/_mock/orders/%s/timeline?partner=%s" % (po_number, partner))
        self.assertEqual(status, 200, found)
        return found


class ASentDocumentSaysWhatItCarries(TimelineCase):
    po = "PO-NUM"

    def setUp(self):
        super().setUp()
        self.send(x12_order(self.po))
        self.found = self.timeline(self.po)

    def test_an_invoice_names_the_invoice_and_the_shipment_it_bills(self):
        (invoiced,) = events(self.found, "invoiced")
        (sent,) = events(self.found, "sent", "810")
        self.assertTrue(invoiced["invoice"] and invoiced["shipment"])
        self.assertEqual(numbers(sent), {"order": self.po,
                                         "invoice": invoiced["invoice"],
                                         "shipment": invoiced["shipment"]})

    def test_a_despatch_advice_names_the_shipment(self):
        (packed,) = events(self.found, "packed")
        (sent,) = events(self.found, "sent", "856")
        self.assertEqual(numbers(sent), {"order": self.po,
                                         "shipment": packed["shipment"]})

    def test_a_response_names_the_order(self):
        (sent,) = events(self.found, "sent", "855")
        self.assertEqual(numbers(sent), {"order": self.po})

    def test_the_order_received_names_itself(self):
        (received,) = events(self.found, "received", "850")
        self.assertEqual(numbers(received), {"order": self.po})

    def test_an_acknowledgment_names_none_of_them(self):
        (sent,) = events(self.found, "sent", "997")
        self.assertEqual(numbers(sent), {})
        self.assertIn("answers", sent)

    def test_the_number_is_the_one_the_document_itself_says(self):
        """Recorded from the row it was written from, so the two cannot differ."""
        (sent,) = events(self.found, "sent", "810")
        payload = self.mailbox(ACME, "invoice")[0]["payload"]
        big = [s for s in list(parse(payload).messages())[0][1].body
               if s.tag == "BIG"][0]
        self.assertEqual(big.get(2), sent["invoice"])

    def test_the_summary_is_as_it_was(self):
        (sent,) = events(self.found, "sent", "810")
        self.assertNotIn(sent["invoice"], sent["summary"])

    def test_the_documents_listing_has_them_too(self):
        (invoiced,) = events(self.found, "invoiced")
        _status, _headers, rows = self.get(
            "/_mock/documents?reference=%s&code=810" % self.po)
        self.assertEqual([(row["invoice_number"], row["shipment_id"])
                          for row in rows],
                         [(invoiced["invoice"], invoiced["shipment"])])
        _status, _headers, rows = self.get(
            "/_mock/documents?reference=%s&code=855" % self.po)
        self.assertEqual([(row["invoice_number"], row["shipment_id"])
                          for row in rows], [("", "")])


class ADocumentCollectedFromTheMailbox(TimelineCase):
    """The mailbox serves the queue's rows whole, so it has the numbers too.

    Kept on purpose, and held here so it stays a decision: the collector of
    an 810 learns which invoice it is without parsing it.
    """

    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-BOX"))
        (self.invoiced,) = events(self.timeline("PO-BOX"), "invoiced")

    def numbers(self, kind):
        (row,) = self.mailbox(ACME, kind)
        return row["shipment_id"], row["invoice_number"]

    def test_an_invoice_says_which_invoice_and_shipment(self):
        self.assertEqual(self.numbers("invoice"), (self.invoiced["shipment"],
                                                   self.invoiced["invoice"]))

    def test_a_despatch_advice_says_which_shipment(self):
        self.assertEqual(self.numbers("despatch"),
                         (self.invoiced["shipment"], ""))

    def test_a_response_has_the_fields_empty(self):
        self.assertEqual(self.numbers("response"), ("", ""))


class AnOrderInTwoConsignments(TimelineCase):
    """The case position cannot settle: two 856s and two 810s for one order."""
    config_kwargs = WINDOW
    po = "PO-NUM-2C"

    def setUp(self):
        super().setUp()
        self.send(x12_order(self.po, lines=(("WIDGET-001", 100, "12.50"),)))
        self.send(x12_change(self.po, [("1", "QI", 150, "12.50")]))
        self.post("/_mock/advance?all")
        self.found = self.timeline(self.po)

    def test_each_despatch_advice_names_its_own_shipment(self):
        packed = [event["shipment"] for event in events(self.found, "packed")]
        sent = [event["shipment"] for event in events(self.found, "sent", "856")]
        self.assertEqual(len(set(packed)), 2)
        self.assertEqual(sorted(sent), sorted(packed))

    def test_each_invoice_names_its_own_invoice_and_shipment(self):
        invoiced = {(event["invoice"], event["shipment"])
                    for event in events(self.found, "invoiced")}
        sent = {(event["invoice"], event["shipment"])
                for event in events(self.found, "sent", "810")}
        self.assertEqual(len(invoiced), 2)
        self.assertEqual(sent, invoiced)

    def test_the_change_and_its_answer_name_the_order(self):
        for event, code in (("received", "860"), ("sent", "865")):
            with self.subTest(code=code):
                (found,) = events(self.found, event, code)
                self.assertEqual(numbers(found), {"order": self.po})

    def test_a_document_sent_again_names_the_consignment_it_was_asked_for(self):
        first = events(self.found, "packed")[0]["shipment"]
        for kind, code in ((schema.DESPATCH, "856"), (schema.INVOICE, "810")):
            with self.subTest(kind=kind):
                before = len(events(self.timeline(self.po), "sent", code))
                status, _headers, data = self.post("/_mock/send", {
                    "partner": ACME, "kind": kind, "order": self.po,
                    "shipment": first})
                self.assertEqual(status, 201, data)
                again = events(self.timeline(self.po), "sent", code)
                self.assertEqual(len(again), before + 1)
                self.assertEqual(again[-1]["shipment"], first)
                if kind == schema.INVOICE:
                    billed = {event["shipment"]: event["invoice"]
                              for event in events(self.found, "invoiced")}
                    self.assertEqual(again[-1]["invoice"], billed[first])


class ASuppliersDocumentReceived(MatchCase):
    """The other way: the supplier's 856 and 810 for an order the mock placed."""

    def received(self, code):
        status, _headers, found = self.get(
            "/_mock/orders/PO-B/timeline?partner=%s" % self.partner)
        self.assertEqual(status, 200, found)
        (event,) = events(found, "received", code)
        return event

    def said(self, kind, tag, position):
        _status, _headers, rows = self.get(
            "/_mock/documents?reference=PO-B&kind=%s" % kind)
        _status, _headers, row = self.get("/_mock/documents/%s" % rows[0]["id"])
        body = list(parse(row["payload"]).messages())[0][1].body
        return [s for s in body if s.tag == tag][0].get(position)

    def test_a_despatch_advice_names_the_suppliers_shipment(self):
        self.confirmed()
        self.shipped()
        event = self.received("856")
        self.assertTrue(event["shipment"].startswith("SH-"), event)
        self.assertEqual(numbers(event), {
            "order": "PO-B",
            "shipment": self.said(schema.DESPATCH, "BSN", 2)})

    def test_an_invoice_names_the_suppliers_invoice(self):
        self.confirmed()
        self.shipped()
        self.billed(number="INV-77")
        event = self.received("810")
        self.assertEqual((event["order"], event["invoice"]), ("PO-B", "INV-77"))
        self.assertIn("shipment", event)

    def test_a_response_names_the_order(self):
        self.confirmed()
        self.assertEqual(numbers(self.received("855")), {"order": "PO-B"})


class ARowFromBeforeTheNumbersWereKept(TimelineCase):
    """An upgraded file's rows have the columns, and nothing in them."""

    def test_it_says_so_with_an_empty_field(self):
        self.send(x12_order("PO-OLD"))
        with self.httpd.mock.lock:
            self.httpd.mock.conn.execute(
                "UPDATE transaction_set SET shipment_id = '', invoice_number = ''")
            self.httpd.mock.conn.commit()
        found = self.timeline("PO-OLD")
        (sent,) = events(found, "sent", "810")
        self.assertEqual(numbers(sent),
                         {"order": "PO-OLD", "invoice": "", "shipment": ""})
        (sent,) = events(found, "sent", "856")
        self.assertEqual(numbers(sent), {"order": "PO-OLD", "shipment": ""})


if __name__ == "__main__":
    unittest.main()
