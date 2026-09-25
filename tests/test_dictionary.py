"""The dictionary, and the property that keeps it honest.

The most valuable test in the suite is `GeneratedDocumentsAreValid`: every
document the mock writes is validated against the same dictionary it validates
incoming documents with.  A mock that cannot read its own output is not a
trading partner, it is a random segment generator - and this catches the day
somebody adds a segment to a writer and forgets to add it to the definition.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, edifact, schema, validate, x12
from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order, parse


class TheModel(unittest.TestCase):
    def test_every_business_document_exists_in_both_dialects(self):
        for kind in schema.KINDS:
            for dialect in schema.DIALECTS:
                code = schema.set_code(dialect, kind)
                self.assertIsNotNone(schema.lookup(dialect, code),
                                     "%s/%s is not defined" % (dialect, code))

    def test_kind_and_set_code_are_inverses(self):
        for (dialect, code) in schema.SETS:
            kind = schema.kind_of(dialect, code)
            self.assertEqual(schema.set_code(dialect, kind), code)

    def test_a_loop_is_triggered_by_its_first_segment(self):
        self.assertEqual(schema.X12_850.loop_for("PO1").trigger, "PO1")
        self.assertEqual(schema.X12_856.loop_for("HL").id, "HL")

    def test_element_positions_are_labelled_the_way_a_spec_writes_them(self):
        self.assertEqual(schema.BEG.label(3), "BEG03")
        self.assertEqual(schema.BEG.element(3).name, "Purchase Order Number")

    def test_composites_carry_their_components(self):
        s009 = schema.UNH.element(2)
        self.assertTrue(s009.composite)
        self.assertEqual([c.ref for c in s009.components],
                         ["0065", "0052", "0054", "0051", "0057"])

    def test_every_code_list_is_a_mapping_of_code_to_meaning(self):
        for (dialect, code), definition in schema.SETS.items():
            for use, _loop in definition.uses():
                for element in use.segment.elements:
                    for candidate in [element] + list(element.components):
                        if candidate.codes:
                            for key, meaning in candidate.codes.items():
                                self.assertTrue(key and meaning,
                                                "%s %s" % (use.tag, candidate.ref))


class LaidOutAs004010(unittest.TestCase):
    """The beginning segments, element by element, as ASC X12 004010 has them.

    Written out here rather than read from `schema.py`, because the point is
    to check the dictionary against something it did not produce.
    """
    STANDARD = {
        "BAK": ("353", "587", "324", "373", "328", "326", "367", "127", "373"),
        "BCH": ("353", "92", "324", "328", "327", "373", "326", "367", "127",
                "373", "373"),
        "BCA": ("353", "587", "324", "328", "327", "373", "326", "367", "127",
                "373", "373"),
    }

    def test_the_declarations_follow_the_standard(self):
        for tag, refs in self.STANDARD.items():
            segment = getattr(schema, tag)
            self.assertEqual(tuple(e.ref for e in segment.elements), refs, tag)

    def check(self, code, group, body):
        interchange = x12.parse(x12.render(x12.wrap(
            [x12.message(code, "0001", body)], "ACME", "MOCKEDI", "1", "1", group)))
        report = validate.validate(interchange)
        self.assertTrue(report.clean, [m.summary() for m in report.messages])

    def test_an_860_with_a_contract_number_in_bch08_is_clean(self):
        from mockedi.envelope import seg
        self.check("860", "PC", [
            seg("BCH", "04", "SA", "S1", "", "1", "20260925", "REQ-7",
                "CTR-2026-01", "", "20260924"),
            seg("POC", "1", "QI", "5", "", "EA", "12.50", "", "VP", "WIDGET-001"),
            seg("CTT", "1")])

    def test_an_855_with_a_contract_number_in_bak07_is_clean(self):
        from mockedi.envelope import seg
        self.check("855", "PR", [
            seg("BAK", "00", "AD", "S1", "20260924", "", "REQ-1", "CTR-9", "",
                "20260925"),
            seg("CTT", "0")])


class GeneratedDocumentsAreValid(unittest.TestCase):
    """Everything the mock writes passes the checks it applies to what it reads."""

    @classmethod
    def setUpClass(cls):
        from mockedi import db, documents, partners, pipeline, transactions
        from mockedi.server import Config
        cls.config = Config(db_path=":memory:", pretty=False)
        cls.conn = db.connect(":memory:")
        db.seed(cls.conn)
        cls.pipeline = pipeline.Pipeline(cls.conn, cls.config)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _run(self, partner_id, payload):
        receipt = self.pipeline.receive(payload.encode())[0]
        self.assertTrue(receipt.ok, receipt.error)
        rows = self.pipeline.collect(partner_id=partner_id, leave=False)
        self.assertTrue(rows, "nothing was produced for %s" % partner_id)
        for row in rows:
            interchange = parse(row["payload"])
            report = validate.validate(interchange)
            self.assertTrue(
                report.clean,
                "the %s the mock generated does not validate:\n%s\n%s"
                % (row["code"], row["payload"], "\n".join(ack.explain(report))))

    def test_every_x12_document_validates(self):
        self._run(ACME, x12_order("VALID-X12"))

    def test_every_edifact_document_validates(self):
        self._run(EURODIS, edifact_order("VALID-EDI"))

    def test_they_still_validate_when_lines_are_short_and_rejected(self):
        from mockedi import partners
        partners.update(self.conn, ACME, behaviour="short-ship")
        self._run(ACME, x12_order("VALID-SHORT",
                                  lines=(("WIDGET-001", 100, "12.50"),
                                         ("NO-SUCH-SKU", 5, "1.00"),
                                         ("GEAR-200", 500, "14.25"))))
        partners.update(self.conn, ACME, behaviour="accept")

    def test_a_change_response_validates(self):
        from mockedi import documents, partners
        from support import x12_change, edifact_change
        # A change needs an order that has not shipped, so this pipeline is
        # configured with a window and the order is left unfulfilled.
        self.config.despatch_delay_ms = 3600000
        self.config.invoice_delay_ms = 3600000
        try:
            self._run(ACME, x12_order("VALID-CHANGE"))
            self._run(ACME, x12_change("VALID-CHANGE",
                                       [("1", "CA", 60, "12.50"),
                                        ("2", "DI", 0, "0"),
                                        ("3", "AI", 25, "8.90")],
                                       skus={"3": "GEAR-100"}))
            self._run(EURODIS, edifact_order("VALID-CHANGE-E"))
            self._run(EURODIS, edifact_change("VALID-CHANGE-E",
                                              [("1", "3", 60, "12.50")]))
        finally:
            self.config.despatch_delay_ms = 0
            self.config.invoice_delay_ms = 0

    def test_an_acknowledgment_of_a_broken_document_validates(self):
        broken = x12_order("VALID-BROKEN", purpose="ZZ",
                           lines=(("WIDGET-001", 100, "12.50"),))
        self._run(ACME, broken)


class TheEndpoint(MockServerCase):
    def test_it_lists_every_transaction_set(self):
        _s, _h, data = self.get("/_mock/dictionary")
        codes = {(t["dialect"], t["code"]) for t in data["transactionSets"]}
        self.assertIn(("X12", "850"), codes)
        self.assertIn(("EDIFACT", "DESADV"), codes)
        # Against the registry rather than a number: adding a transaction set
        # should not need this test edited, only the registry.
        self.assertEqual(codes, set(schema.SETS))

    def test_one_set_comes_with_its_segments_and_elements(self):
        _s, _h, data = self.get("/_mock/dictionary/X12/850")
        self.assertEqual(data["name"], "Purchase Order")
        self.assertEqual(data["group"], "PO")
        beg = [s for s in data["segments"] if s["tag"] == "BEG"][0]
        self.assertEqual(beg["requirement"], "M")
        self.assertEqual(beg["elements"][0]["ref"], "353")
        self.assertIn("00", beg["elements"][0]["codes"])

    def test_edifact_segments_publish_their_components(self):
        _s, _h, data = self.get("/_mock/dictionary/EDIFACT/ORDERS")
        bgm = [s for s in data["segments"] if s["tag"] == "BGM"][0]
        self.assertEqual(bgm["elements"][0]["components"][0]["ref"], "1001")

    def test_an_unknown_set_says_so(self):
        _s, _h, data = self.get("/_mock/dictionary/X12/999")
        self.assertIn("error", data)


class ValidateOnly(MockServerCase):
    def test_a_good_document_is_clean_and_changes_nothing(self):
        _s, _h, data = self.post("/_mock/validate", x12_order("PO-CHECK"))
        self.assertTrue(data["clean"])
        self.assertEqual(data["groupCode"], "A")
        status, _h, _data = self.get("/_mock/orders/PO-CHECK")
        self.assertEqual(status, 404)
        self.assertEqual(self.mailbox(ACME), [])

    def test_findings_come_back_as_prose_and_as_structure(self):
        _s, _h, data = self.post("/_mock/validate",
                                 x12_order("PO-CHECK-BAD", purpose="ZZ"))
        self.assertFalse(data["clean"])
        self.assertTrue(any("BEG01" in line for line in data["explain"]))
        self.assertTrue(data["messages"][0]["findings"])

    def test_something_unparseable_is_a_422_with_a_reason(self):
        status, _h, data = self.post("/_mock/validate", "not edi at all")
        self.assertEqual(status, 422)
        self.assertFalse(data["parsed"])

    def test_it_works_for_a_sender_that_is_not_a_partner(self):
        _s, _h, data = self.post("/_mock/validate",
                                 x12_order("PO-ANON", sender="STRANGER"))
        self.assertTrue(data["parsed"])
        self.assertEqual(data["sender"], "STRANGER")


if __name__ == "__main__":
    unittest.main(verbosity=2)
