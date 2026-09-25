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


def declared(dialect, tag):
    """The dictionary's definition of a segment, wherever a set uses it."""
    for (set_dialect, _code), definition in schema.SETS.items():
        if set_dialect == dialect:
            segment = definition.segment_for(tag)
            if segment is not None:
                return segment
    return None


class EveryWrittenSegmentAgainstTheStandard(unittest.TestCase):
    """Element numbers, position by position, as the standards give them.

    Stated here and not read from `schema.py`: the writer and the validator
    share the dictionary, so a declaration that is wrong against the standard
    is invisible to every test that uses it. #49 was three of those, and SN1
    - which put its line status at SN107 instead of SN108 - was a fourth.
    Each list is compared as far as both it and the dictionary go.

    Left out on purpose, because the right answer is not certain enough to
    pin: TDS (whose element numbers look older than 004010's) and IMD's
    second position.
    """
    X12 = {
        "ST": ("143", "329"), "SE": ("96", "329"),
        "BEG": ("353", "92", "324", "328", "373", "367", "587"),
        "BSN": ("353", "396", "373", "337", "1005"),
        "BIG": ("373", "76", "373", "324", "328", "327", "640", "353"),
        "ACK": ("668", "380", "355", "374", "373", "326", "235", "234"),
        "PO1": ("350", "330", "355", "212", "639", "235", "234"),
        "IT1": ("350", "358", "355", "212", "639", "235", "234"),
        "POC": ("350", "670", "330", "671", "355", "212", "639", "235", "234"),
        "SN1": ("350", "382", "355", "646", "330", "355", "728", "668"),
        "LIN": ("350", "235", "234"),
        "HL": ("628", "734", "735", "736"),
        "PRF": ("324", "328", "327", "373"),
        "TD1": ("103", "80", "23", "22", "79", "187", "81", "355"),
        "TD5": ("133", "66", "67", "91", "387"),
        "ITD": ("336", "333", "338", "370", "351", "446", "386", "362"),
        "TXI": ("963", "782", "954"),
        "PID": ("349", "750", "559", "751", "352"),
        "CUR": ("98", "100", "280"),
        "CTT": ("354", "347"),
        "N1": ("98", "93", "66", "67"), "N3": ("166", "166"),
        "N4": ("19", "156", "116", "26"), "REF": ("128", "127", "352"),
        "DTM": ("374", "373", "337", "623"), "PER": ("366", "93", "365", "364"),
        "AK2": ("143", "329"), "AK3": ("721", "719", "447", "720"),
        "AK5": ("717", "718", "718", "718", "718", "718"),
        "AK9": ("715", "97", "123", "2", "716", "716", "716", "716", "716"),
    }
    EDIFACT = {
        "UNH": ("0062", "S009"), "UNT": ("0074", "0062"), "UNS": ("0081",),
        "BGM": ("C002", "C106", "1225", "4343"),
        "DTM": ("C507",), "RFF": ("C506",), "QTY": ("C186",), "MOA": ("C516",),
        "CNT": ("C270",), "PRI": ("C509", "5213"),
        "NAD": ("3035", "C082", "C058", "C080", "C059", "3164", "3229", "3251",
                "3207"),
        "LIN": ("1082", "1229", "C212", "C829", "1222", "7083"),
        "PIA": ("4347", "C212"),
        "FTX": ("4451", "4453", "C107", "C108", "3453"),
        "CUX": ("C504", "C504", "5402", "6341"),
        "PAT": ("4279", "C110", "C112"),
        "CPS": ("7164", "7166", "7075"),
        "PAC": ("7224", "C531", "C202"),
        "TDT": ("8051", "8028", "C220", "C228", "C040"),
        "UCI": ("0020", "S002", "S003", "0083", "0085", "0013", "S011"),
        "UCM": ("0062", "S009", "0083", "0085", "0013", "S011"),
        "UCS": ("0096", "0085"), "UCD": ("0085", "S011"),
    }

    def check(self, dialect, table):
        for tag, standard in table.items():
            with self.subTest(dialect=dialect, segment=tag):
                segment = declared(dialect, tag)
                self.assertIsNotNone(segment, "%s is not declared" % tag)
                refs = tuple(e.ref for e in segment.elements)
                width = min(len(refs), len(standard))
                self.assertEqual(refs[:width], standard[:width],
                                 "%s differs from the standard" % tag)

    def test_x12(self):
        self.check("X12", self.X12)

    def test_edifact(self):
        self.check("EDIFACT", self.EDIFACT)


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

    def test_every_partner_and_every_behaviour(self):
        """Every seeded partner, under every behaviour: what it writes validates.

        ACME is 004010, GLOBEX 005010 (ISA11 carries a repetition separator
        and GS08 says 005010), INITECH has qualifier 01, EURODIS is EDIFACT.
        `no-ack` is checked for saying nothing at all.
        """
        from mockedi import db, partners
        from support import GLOBEX, INITECH
        originals = {pid: partners.get(self.conn, pid)["behaviour"]
                     for pid in (ACME, GLOBEX, INITECH, EURODIS)}
        try:
            for partner_id in originals:
                for index, behaviour in enumerate(db.BEHAVIOURS):
                    with self.subTest(partner=partner_id, behaviour=behaviour):
                        self._behaviour_run(partner_id, behaviour, index)
        finally:
            for partner_id, behaviour in originals.items():
                partners.update(self.conn, partner_id, behaviour=behaviour)

    def _behaviour_run(self, partner_id, behaviour, index):
        from mockedi import partners
        partners.update(self.conn, partner_id, behaviour=behaviour)
        # Short enough for BEG03's 22 characters, which the answers echo.
        po_number = "ALL-%s-%d" % (partner_id, index)
        lines = (("WIDGET-001", 100, "12.50"), ("BRKT-050", 40, "4.15"))
        payload = (edifact_order(po_number, lines=lines) if partner_id == EURODIS
                   else x12_order(po_number, lines=lines, sender=partner_id))
        receipt = self.pipeline.receive(payload.encode())[0]
        self.assertTrue(receipt.ok, receipt.error)
        self.pipeline.advance(everything=True)
        rows = self.pipeline.collect(partner_id=partner_id, leave=False)
        if behaviour == "no-ack":
            self.assertEqual(rows, [])
            return
        self.assertTrue(rows, "nothing was produced")
        for row in rows:
            report = validate.validate(parse(row["payload"]))
            self.assertTrue(
                report.clean,
                "the %s does not validate:\n%s\n%s"
                % (row["code"], row["payload"], "\n".join(ack.explain(report))))

    def test_the_997_for_a_fatally_broken_set_validates(self):
        # No BEG at all: the set is rejected, AK3/AK4/AK5*R carry why.
        broken = "\n".join(line for line in
                           x12_order("VALID-FATAL").splitlines()
                           if not line.startswith("BEG")).replace("SE*11*", "SE*10*")
        receipt = self.pipeline.receive(broken.encode())[0]
        self.assertTrue(receipt.ok, receipt.error)
        rows = self.pipeline.collect(partner_id=ACME, leave=False)
        ack997 = [parse(r["payload"]) for r in rows if r["code"] == "997"][0]
        message = ack997.groups[0].messages[0]
        self.assertEqual(message.find("AK5").get(1), "R")
        report = validate.validate(ack997)
        self.assertTrue(report.clean, "\n".join(ack.explain(report)))

    def test_the_contrl_for_a_broken_edifact_message_validates(self):
        # A QTY qualifier outside the code list: UCS and UCD say where.
        broken = edifact_order("VALID-UCD").replace("QTY+21:", "QTY+999:", 1)
        receipt = self.pipeline.receive(broken.encode())[0]
        self.assertTrue(receipt.ok, receipt.error)
        rows = self.pipeline.collect(partner_id=EURODIS, leave=False)
        contrl = [parse(r["payload"]) for r in rows if r["code"] == "CONTRL"][0]
        message = contrl.groups[0].messages[0]
        self.assertIsNotNone(message.find("UCS"))
        self.assertIsNotNone(message.find("UCD"))
        report = validate.validate(contrl)
        self.assertTrue(report.clean, "\n".join(ack.explain(report)))

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
