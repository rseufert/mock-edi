"""Reading an acknowledgment for something the mock sent."""
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import (ACME, EURODIS, MockServerCase, acknowledge, edifact_order,
                     x12_order)


class AcknowledgingAnX12Document(MockServerCase):
    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-ACK"))
        self.documents = {row["code"]: row for row in self.mailbox(ACME)}

    def ack(self, code, verdict="A", errors=()):
        payload = acknowledge(self.documents[code]["payload"], verdict, errors)
        return self.send(payload)

    def sent(self, code):
        _status, _headers, rows = self.get("/_mock/documents?direction=out&code=" + code)
        return rows[0]

    def test_a_997_marks_the_document_it_names(self):
        summary = self.ack("855")
        self.assertEqual(len(summary["acknowledged"]), 1)
        entry = summary["acknowledged"][0]
        self.assertTrue(entry["matched"])
        self.assertEqual(entry["code"], "855")
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(entry["reference"], "PO-ACK")

    def test_the_document_itself_carries_the_verdict(self):
        self.ack("810")
        row = self.sent("810")
        self.assertEqual(row["ack_status"], "accepted")
        self.assertEqual(row["ack_code"], "A")
        self.assertTrue(row["ack_at"])

    def test_a_rejection_is_recorded_as_one_with_the_reason(self):
        self.ack("810", "R", errors=[("BIG", 2, "8", 1, "7", "BADCODE")])
        row = self.sent("810")
        self.assertEqual(row["ack_status"], "rejected")
        self.assertEqual(row["ack_code"], "R")
        self.assertIn("BIG at segment 2", row["ack_note"])
        self.assertIn("invalid code value", row["ack_note"].lower())
        self.assertIn("BADCODE", row["ack_note"])

    def test_accepted_with_errors_is_neither_accepted_nor_rejected(self):
        self.ack("856", "E", errors=[("HL", 5, "8", 3, "5", "toolong")])
        self.assertEqual(self.sent("856")["ack_status"], "accepted-with-errors")

    def test_matching_needs_both_control_numbers(self):
        """ST02 is only unique within its functional group."""
        payload = self.documents["855"]["payload"]
        broken = payload.replace("GS*PR*MOCKEDI*ACME", "GS*PR*MOCKEDI*ACME")
        ack = acknowledge(broken, "A")
        # Quote a group control number the mock never used.
        ack = ack.replace("AK1*PR*2*", "AK1*PR*999*").replace("AK1*PR*3*", "AK1*PR*999*")
        summary = self.send(ack)
        self.assertFalse(summary["acknowledged"][0]["matched"])
        self.assertEqual(self.sent("855")["ack_status"], "")

    def test_an_acknowledgment_for_a_set_we_never_sent_is_recorded_not_dropped(self):
        payload = self.documents["855"]["payload"]
        ack = acknowledge(payload, "A").replace("AK2*855*", "AK2*855*9999~AK5*A~AK2*855*")
        summary = self.send(ack)
        self.assertTrue(any(not entry["matched"] for entry in summary["acknowledged"]))

    def test_the_997_itself_is_filed_like_any_other_document(self):
        self.ack("855")
        _status, _headers, rows = self.get("/_mock/documents?direction=in&code=997")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "acknowledgment")


class AcknowledgingAnEdifactDocument(MockServerCase):
    def setUp(self):
        super().setUp()
        self.send(edifact_order("PO-ACK-E"),
                  headers={"Content-Type": "application/edifact"})
        self.documents = {row["code"]: row for row in self.mailbox(EURODIS)}

    def sent(self, code):
        _status, _headers, rows = self.get("/_mock/documents?direction=out&code=" + code)
        return rows[0]

    def test_a_contrl_marks_the_message_it_names(self):
        summary = self.send(acknowledge(self.documents["ORDRSP"]["payload"], "A"),
                            headers={"Content-Type": "application/edifact"})
        entry = summary["acknowledged"][0]
        self.assertTrue(entry["matched"])
        self.assertEqual(entry["code"], "ORDRSP")
        self.assertEqual(entry["status"], "accepted")
        self.assertEqual(self.sent("ORDRSP")["ack_status"], "accepted")

    def test_a_rejecting_contrl_is_recorded_as_a_rejection(self):
        summary = self.send(
            acknowledge(self.documents["INVOIC"]["payload"], "R",
                        errors=[("MOA", 12, "12", 1, "12", "")]),
            headers={"Content-Type": "application/edifact"})
        self.assertTrue(summary["acknowledged"][0]["matched"])
        row = self.sent("INVOIC")
        self.assertEqual(row["ack_status"], "rejected")
        self.assertIn("segment 12", row["ack_note"])

    def test_it_is_matched_on_the_interchange_uci_quotes(self):
        payload = self.documents["DESADV"]["payload"]
        ack = acknowledge(payload, "A")
        wrong = ack.replace("UCI+", "UCI+9999999+", 1).replace("+9999999++", "+9999999+")
        summary = self.send(wrong, headers={"Content-Type": "application/edifact"})
        self.assertFalse(summary["acknowledged"][0]["matched"])


class WhatIsOutstanding(MockServerCase):
    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-OUT"))
        self.documents = {row["code"]: row for row in self.mailbox(ACME)}

    def outstanding(self, query=""):
        _status, _headers, rows = self.get("/_mock/unacknowledged" + query)
        return [row["code"] for row in rows]

    def test_everything_we_sent_starts_outstanding(self):
        self.assertEqual(sorted(self.outstanding()), ["810", "855", "856"])

    def test_an_acknowledgment_is_not_itself_expected_to_be_acknowledged(self):
        self.assertNotIn("997", self.outstanding())

    def test_acknowledging_one_removes_it(self):
        self.send(acknowledge(self.documents["855"]["payload"], "A"))
        self.assertEqual(sorted(self.outstanding()), ["810", "856"])

    def test_a_rejection_also_counts_as_answered(self):
        self.send(acknowledge(self.documents["810"]["payload"], "R"))
        self.assertNotIn("810", self.outstanding())

    def test_acknowledging_all_of_them_empties_it(self):
        for code in ("855", "856", "810"):
            self.send(acknowledge(self.documents[code]["payload"], "A"))
        self.assertEqual(self.outstanding(), [])

    def test_older_than_asks_the_question_an_operations_team_asks(self):
        self.assertEqual(self.outstanding("?older-than=3600"), [])
        self.assertEqual(sorted(self.outstanding("?older-than=0")),
                         ["810", "855", "856"])

    def test_it_can_be_narrowed_to_one_partner(self):
        self.send(edifact_order("PO-OUT-E"),
                  headers={"Content-Type": "application/edifact"})
        self.assertEqual(sorted(self.outstanding("?partner=EURODIS")),
                         ["DESADV", "INVOIC", "ORDRSP"])

    def test_documents_can_be_filtered_by_whether_they_were_acknowledged(self):
        self.send(acknowledge(self.documents["855"]["payload"], "A"))
        _s, _h, answered = self.get("/_mock/documents?acknowledged=true")
        self.assertEqual([row["code"] for row in answered], ["855"])
        _s, _h, silent = self.get("/_mock/documents?acknowledged=false&direction=out")
        self.assertNotIn("855", [row["code"] for row in silent])


class TheSilentPartner(MockServerCase):
    """`no-ack` is now observable rather than merely inferred from silence."""

    def test_a_partner_that_never_answers_leaves_everything_outstanding(self):
        self.send(x12_order("PO-QUIET"))
        _status, _headers, rows = self.get("/_mock/unacknowledged")
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(row["partner"], ACME)
            self.assertTrue(row["reference"])


class ControlNumbersOnTheWire(MockServerCase):
    """The recorded control numbers must be the ones in the document."""

    def test_an_outbound_document_records_its_own_st02_not_the_isa13(self):
        self.send(x12_order("PO-NUMBERS"))
        row = [r for r in self.mailbox(ACME) if r["code"] == "855"][0]
        from support import parse
        message = parse(row["payload"]).groups[0].messages[0]
        _status, _headers, sent = self.get("/_mock/documents?direction=out&code=855")
        self.assertEqual(sent[0]["control"], message.find("ST").get(2))
        self.assertEqual(sent[0]["group_control"],
                         parse(row["payload"]).groups[0].control)

    def test_an_edifact_document_records_its_unh01(self):
        self.send(edifact_order("PO-NUMBERS-E"),
                  headers={"Content-Type": "application/edifact"})
        row = [r for r in self.mailbox(EURODIS) if r["code"] == "ORDRSP"][0]
        from support import parse
        message = parse(row["payload"]).groups[0].messages[0]
        _status, _headers, sent = self.get("/_mock/documents?direction=out&code=ORDRSP")
        self.assertEqual(sent[0]["control"], message.find("UNH").get(1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
