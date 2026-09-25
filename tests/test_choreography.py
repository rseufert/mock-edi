"""The whole point: an order in, four documents back, in both dialects."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from decimal import Decimal

from mockedi import validate
from mockedi.envelope import Delimiters

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order


class X12OrderToCash(MockServerCase):
    def setUp(self):
        super().setUp()
        self.summary = self.send(x12_order("4500000042"))

    def test_the_order_is_accepted_and_recorded(self):
        self.assertTrue(self.summary["accepted"])
        self.assertEqual(self.summary["partner"], ACME)
        self.assertEqual(self.summary["orders"], ["4500000042"])

    def test_four_documents_come_back_in_order(self):
        self.assertEqual([q["code"] for q in self.summary["queued"]],
                         ["997", "855", "856", "810"])
        self.assertEqual([row["code"] for row in self.mailbox(ACME)],
                         ["997", "855", "856", "810"])

    def test_the_997_says_the_syntax_parsed_and_nothing_more(self):
        message = self.document(ACME, "acknowledgment").groups[0].messages[0]
        self.assertEqual(message.code, "997")
        self.assertEqual(message.find("AK5").get(1), "A")
        self.assertEqual(message.find("AK9").get(1), "A")
        self.assertEqual(message.find("AK1").get(1), "PO")

    def test_the_855_confirms_every_line(self):
        message = self.document(ACME, "response").groups[0].messages[0]
        self.assertEqual(message.find("BAK").get(2), "AD")   # detail, no change
        self.assertEqual(message.find("BAK").get(3), "4500000042")
        acks = message.find_all("ACK")
        self.assertEqual([a.get(1) for a in acks], ["IA", "IA"])
        self.assertEqual([a.get(2) for a in acks], ["100", "40"])

    def test_the_856_is_a_tree_of_shipment_order_and_item(self):
        message = self.document(ACME, "despatch").groups[0].messages[0]
        self.assertEqual(message.find("BSN").get(5), "0004")
        levels = [(h.get(1), h.get(2), h.get(3)) for h in message.find_all("HL")]
        self.assertEqual(levels, [("1", "", "S"), ("2", "1", "O"),
                                  ("3", "2", "I"), ("4", "2", "I")])
        self.assertEqual(message.find("PRF").get(1), "4500000042")
        # CTT01 counts HL segments in an 856, not line items.
        self.assertEqual(message.find("CTT").get(1), "4")

    def test_the_810_bills_what_shipped_with_two_implied_decimals(self):
        message = self.document(ACME, "invoice").groups[0].messages[0]
        self.assertEqual(message.find("BIG").get(4), "4500000042")
        # 100 x 12.50 + 40 x 4.15 = 1416.00
        self.assertEqual(message.find("TDS").get(1), "141600")
        self.assertEqual([i.get(2) for i in message.find_all("IT1")], ["100", "40"])

    def test_the_documents_reference_each_other(self):
        despatch = self.document(ACME, "despatch").groups[0].messages[0]
        invoice = self.document(ACME, "invoice").groups[0].messages[0]
        shipment_id = despatch.find("BSN").get(2)
        references = {r.get(1): r.get(2) for r in invoice.find_all("REF")}
        self.assertEqual(references["SI"], shipment_id)

    def test_the_order_ends_up_invoiced(self):
        order = self.order("4500000042")
        self.assertEqual(order["status"], "invoiced")
        self.assertEqual(order["total"], "1416.00")
        self.assertEqual(len(order["shipments"]), 1)
        self.assertEqual(len(order["invoices"]), 1)

    def test_every_line_is_confirmed_shipped_and_invoiced(self):
        for line in self.order("4500000042")["lines"]:
            self.assertEqual(line["confirmed"], line["quantity"])
            self.assertEqual(line["shipped"], line["quantity"])
            self.assertEqual(line["invoiced"], line["quantity"])


class EdifactOrderToCash(MockServerCase):
    def setUp(self):
        super().setUp()
        self.summary = self.send(edifact_order("PO-2026-00042"),
                                 headers={"Content-Type": "application/edifact"})

    def test_the_same_choreography_in_the_other_dialect(self):
        self.assertEqual([q["code"] for q in self.summary["queued"]],
                         ["CONTRL", "ORDRSP", "DESADV", "INVOIC"])

    def test_the_ordrsp_carries_the_verdict_in_bgm_and_the_quantities_per_line(self):
        message = self.document(EURODIS, "response").groups[0].messages[0]
        bgm = message.find("BGM")
        self.assertEqual(bgm.comp(1, 1), "231")     # purchase order response
        self.assertEqual(bgm.get(4), "AP")          # accepted
        self.assertEqual(message.find("RFF").comp(1, 2), "PO-2026-00042")
        ordered = [q for q in message.find_all("QTY") if q.comp(1, 1) == "21"]
        confirmed = [q for q in message.find_all("QTY") if q.comp(1, 1) == "113"]
        self.assertEqual([q.comp(1, 2) for q in ordered], ["100", "40"])
        self.assertEqual([q.comp(1, 2) for q in confirmed], ["100", "40"])

    def test_units_are_translated_to_the_un_ece_codes(self):
        message = self.document(EURODIS, "response").groups[0].messages[0]
        self.assertEqual(message.find("QTY").comp(1, 3), "PCE")   # EA -> PCE

    def test_the_desadv_uses_cps_where_x12_uses_hl(self):
        message = self.document(EURODIS, "despatch").groups[0].messages[0]
        self.assertEqual(message.find("BGM").comp(1, 1), "351")
        self.assertIsNotNone(message.find("CPS"))
        self.assertIsNotNone(message.find("PAC"))
        despatched = [q for q in message.find_all("QTY") if q.comp(1, 1) == "12"]
        self.assertEqual([q.comp(1, 2) for q in despatched], ["100", "40"])

    def test_every_invoice_total_is_named_rather_than_positional(self):
        message = self.document(EURODIS, "invoice").groups[0].messages[0]
        amounts = {m.comp(1, 1): m.comp(1, 2) for m in message.find_all("MOA")}
        self.assertEqual(amounts["79"], "1416.00")    # total line items
        self.assertEqual(amounts["124"], "0.00")      # tax
        self.assertEqual(amounts["139"], "1416.00")   # total payable

    def test_the_contrl_acknowledges_the_interchange(self):
        message = self.document(EURODIS, "acknowledgment").groups[0].messages[0]
        self.assertEqual(message.code, "CONTRL")
        self.assertEqual(message.find("UCI").get(1), "9001")
        self.assertEqual(message.find("UCM").get(3), "7")   # acknowledged

    def test_the_contrl_names_the_syntax_version_not_the_partners_directory(self):
        # EURODIS trades D:96A, which has no CONTRL; CONTRL is a service
        # message and its UNH names syntax version 3.
        message = self.document(EURODIS, "acknowledgment").groups[0].messages[0]
        self.assertEqual(message.find("UNH").raw(2), ["CONTRL", "D", "3", "UN"])
        report = validate.validate_message(message, "EDIFACT")
        self.assertTrue(report.clean, report.summary())


class CommaDecimalMark(MockServerCase):
    """UNA:+,? ' - a comma decimal mark, as German and Nordic partners send."""

    def test_the_order_is_read_clean_and_billed_at_the_same_prices(self):
        comma = Delimiters(segment="'", element="+", component=":", release="?",
                           decimal=",")
        payload = edifact_order("PO-COMMA", delimiters=comma)
        self.assertIn("PRI+AAA:12,50'", payload)
        self.send(payload, headers={"Content-Type": "application/edifact"})
        contrl = self.document(EURODIS, "acknowledgment").groups[0].messages[0]
        self.assertEqual(contrl.find("UCM").get(3), "7")
        self.assertEqual([s.tag for s in contrl.segments if s.tag == "UCS"], [])
        # 100 x 12.50 + 40 x 4.15, the same as the point-decimal order.
        self.assertEqual(Decimal(self.order("PO-COMMA")["total"]), Decimal("1416.00"))


class BothDialectsAgree(MockServerCase):
    """The same order in two dialects produces the same business answer."""

    def test_the_totals_match(self):
        self.send(x12_order("SAME-X12"))
        self.send(edifact_order("SAME-EDI"),
                  headers={"Content-Type": "application/edifact"})
        self.assertEqual(self.order("SAME-X12")["total"],
                         self.order("SAME-EDI")["total"])

    def test_the_confirmed_quantities_match(self):
        self.send(x12_order("SAME-X12"))
        self.send(edifact_order("SAME-EDI"),
                  headers={"Content-Type": "application/edifact"})
        x12_lines = [l["confirmed"] for l in self.order("SAME-X12")["lines"]]
        edi_lines = [l["confirmed"] for l in self.order("SAME-EDI")["lines"]]
        self.assertEqual(x12_lines, edi_lines)


class ControlNumbers(MockServerCase):
    def test_they_advance_per_partner(self):
        self.send(x12_order("PO-1", control="000000001"))
        self.send(x12_order("PO-2", control="000000002"))
        controls = [row["control"] for row in self.mailbox(ACME, "acknowledgment")]
        self.assertEqual(sorted(controls), ["1", "5"])

    def test_each_partner_has_its_own_series(self):
        self.send(x12_order("PO-1"))
        self.send(edifact_order("PO-2"),
                  headers={"Content-Type": "application/edifact"})
        acme = [r["control"] for r in self.mailbox(ACME, "acknowledgment")]
        euro = [r["control"] for r in self.mailbox(EURODIS, "acknowledgment")]
        self.assertEqual(acme, ["1"])
        self.assertEqual(euro, ["1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
