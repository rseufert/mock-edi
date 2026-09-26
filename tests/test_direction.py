"""A seller refuses the documents a seller sends (#117).

Every partner the mock has is a buyer, and a buyer sends orders, changes and
acknowledgments. An 855, 856, 810 or 865 - or an ORDRSP, DESADV or INVOIC -
is what the *mock* sends, so one arriving from a partner is a document its
relationship with that partner does not process. A real translator rejects it
in the words it uses for a set it has never heard of: 718 code 1, "Transaction
Set Not Supported", or 0085 = 14 in a CONTRL. A supplier-side integration
pointed at the mock by mistake must not be told its ASN went through.

The documents sent here are the mock's own, re-enveloped as if the partner
had sent them: valid in every other way, so the direction is the only fault.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, schema, validate, x12

from support import (ACME, EURODIS, MockServerCase, _next_control, edifact_order,
                     x12_change, x12_order)

EDIFACT_TYPE = {"Content-Type": "application/edifact"}
X12_TYPE = {"Content-Type": "application/edi-x12"}


def from_partner_x12(document, partner=ACME):
    """The mock's X12 document, as though `partner` had sent it."""
    group = document.groups[0]
    message = group.messages[0]
    control = _next_control(9)
    return x12.render(x12.wrap(
        [x12.message(message.code, "0001", message.segments[1:-1])],
        partner, "MOCKEDI", control, str(int(control)), group.functional_id,
        version=group.version or "004010"), newline=True)


def from_partner_edifact(document, partner=EURODIS):
    """The mock's EDIFACT message, as though `partner` had sent it."""
    message = document.groups[0].messages[0]
    return edifact.render(edifact.wrap(
        [edifact.message(message.code, "1", message.segments[1:-1],
                         version=message.version or "D:96A:UN")],
        partner, "MOCKEDI", _next_control(4)), newline=True)


class TheX12DocumentsASellerSends(MockServerCase):
    # A window, so that a change can still be made and answered with an 865.
    config_kwargs = {"despatch_delay_ms": 3600000, "invoice_delay_ms": 3600000}

    def the_mocks(self, kind):
        """One of the mock's own documents of `kind`, for ACME."""
        self.send(x12_order("PO-OURS"))
        if kind == schema.CHANGE_RESPONSE:
            self.send(x12_change("PO-OURS", [("1", "CA", 60, "12.50")]))
        elif kind in (schema.DESPATCH, schema.INVOICE):
            self.post("/_mock/advance?all")
        document = self.document(ACME, kind)
        self.mailbox(ACME, leave=False)
        return document

    def send_back(self, kind):
        status, _h, data = self.post(
            "/edi", from_partner_x12(self.the_mocks(kind)), headers=X12_TYPE)
        self.assertEqual(status, 200, data)
        return data

    def check(self, kind, code):
        data = self.send_back(kind)
        self.assertFalse(data["accepted"])
        self.assertEqual([s["code"] for s in data["transactionSets"]], [code])
        self.assertIn("a document the seller sends, not one it receives",
                      data["transactionSets"][0]["findings"][0])

        # The 997 rejects it as a set this relationship does not support.
        ack = self.document(ACME, "acknowledgment").groups[0].messages[0]
        ak5 = ack.find("AK5")
        self.assertEqual((ak5.get(1), ak5.get(2)), ("R", "1"))
        self.assertEqual(ack.find("AK2").get(1), code)

        # Archived, as everything received is - rejected, and not filed under
        # the PO number it names.
        _s, _h, rows = self.get("/_mock/documents?direction=in&code=" + code)
        self.assertEqual(len(rows), 1, rows)
        self.assertFalse(rows[0]["accepted"])
        self.assertEqual(rows[0]["reference"], "")
        _s, _h, rows = self.get("/_mock/documents?direction=in&reference=PO-OURS")
        self.assertNotIn(code, [row["code"] for row in rows])

    def test_an_855(self):
        self.check(schema.RESPONSE, "855")

    def test_an_856(self):
        self.check(schema.DESPATCH, "856")

    def test_an_810(self):
        self.check(schema.INVOICE, "810")

    def test_an_865(self):
        self.check(schema.CHANGE_RESPONSE, "865")

    def test_nothing_is_acted_on(self):
        document = self.the_mocks(schema.INVOICE)
        before = self.order("PO-OURS")
        _s, _h, orders_before = self.get("/_mock/orders")
        self.post("/edi", from_partner_x12(document), headers=X12_TYPE)
        self.assertEqual(self.order("PO-OURS"), before)
        _s, _h, orders_after = self.get("/_mock/orders")
        self.assertEqual(orders_after, orders_before)
        # Only the 997 answers it: no response, despatch or invoice.
        self.assertEqual([r["code"] for r in self.mailbox(ACME)], ["997"])


class TheEdifactDocumentsASellerSends(MockServerCase):
    def the_mocks(self, kind):
        self.send(edifact_order("PO-OURS-E"), headers=EDIFACT_TYPE)
        document = self.document(EURODIS, kind)
        self.mailbox(EURODIS, leave=False)
        return document

    def check(self, kind, code):
        status, _h, data = self.post(
            "/edi", from_partner_edifact(self.the_mocks(kind)),
            headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["accepted"])
        self.assertIn("a document the seller sends, not one it receives",
                      data["transactionSets"][0]["findings"][0])

        # A UCM naming the message, rejected (4) as not supported (14).
        contrl = self.document(EURODIS, "acknowledgment").groups[0].messages[0]
        ucm = contrl.find("UCM")
        self.assertIsNotNone(ucm, contrl.segments)
        self.assertEqual(ucm.comp(2, 1), code)
        self.assertEqual((ucm.get(3), ucm.get(4)), ("4", "14"))

        _s, _h, rows = self.get("/_mock/documents?direction=in&code=" + code)
        self.assertEqual(len(rows), 1, rows)
        self.assertFalse(rows[0]["accepted"])
        self.assertEqual(rows[0]["reference"], "")

    def test_an_ordrsp(self):
        self.check(schema.RESPONSE, "ORDRSP")

    def test_a_desadv(self):
        self.check(schema.DESPATCH, "DESADV")

    def test_an_invoic(self):
        self.check(schema.INVOICE, "INVOIC")


class WhatASellerStillReceives(MockServerCase):
    def test_an_order_is_accepted(self):
        self.assertTrue(self.send(x12_order("PO-STILL"))["accepted"])

    def test_an_acknowledgment_is_still_read(self):
        from support import acknowledge
        self.send(x12_order("PO-ACKED"))
        invoice = self.mailbox(ACME, "invoice", leave=False)[0]
        data = self.send(acknowledge(invoice["payload"]))
        self.assertTrue(data["acknowledged"], data)


class TheRoleDecides(unittest.TestCase):
    """Keyed on the role, not on "the seller": flip it and the sets flip."""

    def verdicts(self, role):
        out = {}
        for code in ("850", "855", "856", "810", "860", "865"):
            message = x12.message(code, "0001", [])
            report = validate.validate_message(message, "X12", role=role)
            out[code] = not report.misdirected
        return out

    def test_a_seller_takes_orders_and_changes(self):
        self.assertEqual(self.verdicts(schema.SELLER), {
            "850": True, "860": True,
            "855": False, "856": False, "810": False, "865": False})

    def test_a_buyer_takes_what_a_seller_sends(self):
        self.assertEqual(self.verdicts(schema.BUYER), {
            "850": False, "860": False,
            "855": True, "856": True, "810": True, "865": True})

    def test_acknowledgments_go_both_ways(self):
        for role in (schema.SELLER, schema.BUYER):
            report = validate.validate_message(
                x12.message("997", "0001", []), "X12", role=role)
            self.assertFalse(report.misdirected, role)

    def test_a_document_read_on_its_own_has_no_direction(self):
        report = validate.validate_message(x12.message("855", "0001", []), "X12")
        self.assertFalse(report.misdirected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
