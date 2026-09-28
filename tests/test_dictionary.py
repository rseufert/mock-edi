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

from decimal import Decimal

from mockedi import ack, edifact, schema, validate, x12
from mockedi.testing import Document, Mock
from support import (ACME, EURODIS, GLOBEX, INITECH, MockServerCase,
                     edifact_change, edifact_order, parse, x12_change,
                     x12_order)


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
    pin: IMD's second position. TDS was too, until #163 checked it against
    a published 004010 table: 610 in all four positions.
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
        "TDS": ("610", "610", "610", "610"),
        "PID": ("349", "750", "559", "751", "352"),
        "CUR": ("98", "100", "280"),
        "CTT": ("354", "347"),
        "N1": ("98", "93", "66", "67"), "N3": ("166", "166"),
        "N4": ("19", "156", "116", "26"), "REF": ("128", "127", "352"),
        "DTM": ("374", "373", "337", "623"), "PER": ("366", "93", "365", "364"),
        "AK2": ("143", "329"), "AK3": ("721", "719", "447", "720"),
        "AK5": ("717", "718", "718", "718", "718", "718"),
        "AK9": ("715", "97", "123", "2", "716", "716", "716", "716", "716"),
        # The 820's remittance-advice segments (#149).
        "BPR": ("305", "782", "478", "591", "812", "506", "507", "569", "508",
                "509", "510", "506", "507", "569", "508", "373"),
        "TRN": ("481", "127", "509", "127"),
        "ENT": ("554", "98", "66", "67"),
        "RMR": ("128", "127", "482", "782", "782", "782", "426", "782"),
        "ADX": ("782", "426", "128", "127"),
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
        # REMADV's (#149).
        "DOC": ("C002", "C503", "3153", "1220", "1218"),
        "AJT": ("4465", "1082"),
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



def _wrap(dialect, code, body, group, when):
    """One message in an interchange, so it can be validated as one arrives."""
    if dialect == "X12":
        return x12.wrap([x12.message(code, "0001", body)], "MOCKEDI",
                        "NORTHWIND", "1", "1", group, moment=when)
    return edifact.wrap([edifact.message(code, "1", body, version="D:96A:UN")],
                        "MOCKEDI", "NORTHWIND", "1", moment=when)


class EachElementNumberMeansOneThing(unittest.TestCase):
    """An element number is declared the same way wherever it is used (#163).

    A dictionary serves one version per dialect, and in one version a data
    element has one representation. When two segments disagreed - 1131 was
    an..3 in ALC and AN 1..17 in nine composites - the same value was an
    error in one place and fine in another, and `GET /_mock/dictionary`
    told a reader two things.
    """

    def declarations(self, dialect):
        found = {}

        def walk(elements, tag):
            for element in elements:
                if element.composite:
                    walk(element.components, tag)
                else:
                    found.setdefault(element.ref, {}).setdefault(
                        (element.type, element.min_len, element.max_len),
                        set()).add(tag)

        def segments(children):
            for child in children:
                if isinstance(child, schema.Use):
                    yield child.segment
                else:
                    yield from segments(child.children)

        for (set_dialect, _code), definition in schema.SETS.items():
            if set_dialect == dialect:
                for segment in segments(definition.children):
                    walk(segment.elements, segment.tag)
        return found

    def test_x12(self):
        self.assertEqual({ref: ways for ref, ways in self.declarations("X12").items()
                          if len(ways) > 1}, {})

    def test_edifact(self):
        self.assertEqual({ref: ways for ref, ways in
                          self.declarations("EDIFACT").items() if len(ways) > 1}, {})

    def test_what_163_corrected_against_the_directories(self):
        # D.96A: 1131 "Code list qualifier" an..3; 3055 an..3; 3453
        # "Language, coded" an..3; DOC's 1366 an..35. 004010: ITD08, 362,
        # N2 1/10.
        edifact_ways = self.declarations("EDIFACT")
        self.assertEqual(set(edifact_ways["1131"]), {("AN", 1, 3)})
        self.assertEqual(set(edifact_ways["3055"]), {("AN", 1, 3)})
        self.assertEqual(set(edifact_ways["3453"]), {("ID", 1, 3)})
        self.assertEqual(set(edifact_ways["1366"]), {("AN", 1, 35)})
        self.assertEqual(set(self.declarations("X12")["362"]), {("N2", 1, 10)})


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


    def test_an_order_the_mock_writes_validates(self):
        """The two writers the mock does not use yet.

        `write_order` and `write_change` are the buyer's side, so no pipeline
        produces them and `_run` cannot reach them. They are held to the same
        standard here: what they write passes the dictionary.
        """
        from mockedi import transactions
        from mockedi.transactions import Party
        us = Party(name="Mock EDI Buying Co", identifier="MOCKEDI",
                   street="500 Seaport Boulevard", city="Boston", region="MA",
                   postal="02210", country="US")
        partner = {"id": "NORTHWIND", "name": "Northwind Traders",
                   "street": "12 Harbour Way", "city": "Seattle",
                   "region": "WA", "postal": "98101", "country": "US"}
        order = {"po_number": "4500009001", "ordered_on": "2026-10-01",
                 "requested_on": "2026-10-15", "currency": "USD"}
        lines = [{"line": "1", "sku": "WIDGET-001", "upc": "076123400003",
                  "quantity": "100", "uom": "EA", "price": "12.50",
                  "description": "Widget, blue, 40mm"},
                 {"line": "2", "sku": "BRKT-050", "quantity": "40",
                  "uom": "CA", "price": "4.15"}]
        when = datetime.datetime(2026, 10, 1, 9, 30)

        for dialect, code, group in (("X12", "850", "PO"),
                                     ("EDIFACT", "ORDERS", "")):
            with self.subTest(dialect=dialect, code=code):
                body = transactions.write_order(dialect, us, partner, order,
                                                lines, when)
                interchange = _wrap(dialect, code, body, group, when)
                report = validate.validate(interchange)
                self.assertTrue(report.clean, "\n".join(ack.explain(report)))

    def test_a_change_the_mock_writes_validates(self):
        from decimal import Decimal as D

        from mockedi import transactions
        from mockedi.transactions import Change, ChangeLine, Party
        us = Party(name="Mock EDI Buying Co", identifier="MOCKEDI")
        partner = {"id": "NORTHWIND", "name": "Northwind Traders"}
        order = {"po_number": "4500009002", "ordered_on": "2026-10-01",
                 "currency": "USD"}
        when = datetime.datetime(2026, 10, 5, 11, 0)
        for purpose in ("04", "01"):
            change = Change(po_number="4500009002", purpose=purpose,
                            sequence="2",
                            changed_on=datetime.date(2026, 10, 5),
                            ordered_on=datetime.date(2026, 10, 1),
                            currency="USD",
                            lines=[ChangeLine(number="1", sku="WIDGET-001",
                                              quantity=D("60"), uom="EA",
                                              price=D("12.50"), action="QD"),
                                   ChangeLine(number="2", sku="BRKT-050",
                                              quantity=D("0"), uom="EA",
                                              price=D("0.00"), action="DI")])
            for dialect, code, group in (("X12", "860", "PC"),
                                         ("EDIFACT", "ORDCHG", "")):
                with self.subTest(dialect=dialect, purpose=purpose):
                    body = transactions.write_change(dialect, us, partner,
                                                     order, change, when)
                    interchange = _wrap(dialect, code, body, group, when)
                    report = validate.validate(interchange)
                    self.assertTrue(report.clean,
                                    "\n".join(ack.explain(report)))

    def test_every_partner_and_every_behaviour(self):
        """Every seeded partner, under every behaviour: what it writes validates.

        ACME is 004010, GLOBEX 005010 (ISA11 carries a repetition separator
        and GS08 says 005010), INITECH has qualifier 01, EURODIS is EDIFACT.
        `no-ack` is checked for saying nothing at all.

        Every partner here is a customer, so the behaviours that belong only
        to a supplier are skipped: `db.BEHAVIOUR_ROLES` is the record, and
        setting one of those here would be refused rather than tested (#127).
        What the mock writes under them is held to the same property by
        `test_buyer_behaviours.WhatTheyWriteIsValid`.
        """
        from mockedi import db, partners
        from support import GLOBEX, INITECH
        originals = {pid: partners.get(self.conn, pid)["behaviour"]
                     for pid in (ACME, GLOBEX, INITECH, EURODIS)}
        try:
            for partner_id in originals:
                for index, behaviour in enumerate(db.BEHAVIOURS):
                    if "customer" not in db.BEHAVIOUR_ROLES[behaviour]:
                        continue
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
            if behaviour == "corrupt" and row["kind"] != "acknowledgment":
                # Corrupt on purpose, and in exactly one way: the trailer
                # miscounts (718 code 4), and nothing else is wrong.
                self.assertEqual([code for m in report.messages
                                  for code, _note in m.set_errors], ["4"],
                                 row["code"])
                self.assertEqual([m.segments for m in report.messages], [[]])
                continue
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



class ReadersInvertTheWriters(unittest.TestCase):
    """What the writers were given, read back out of what they wrote.

    `GeneratedDocumentsAreValid`'s sibling: that one asks whether the mock's
    output satisfies its own dictionary, and this asks whether the values
    survive the trip. Between them a writer cannot go wrong quietly - a
    segment in the wrong position passes validation and comes back as the
    wrong number here.

    Each document is compared on what it actually carries, and no more. A
    DESADV has no ordered quantity in it and no order date, so asserting
    either would be testing the reader against the wrong standard rather than
    against the wire.
    """

    @classmethod
    def setUpClass(cls):
        cls.mock = Mock.start(despatch_delay_ms=0, invoice_delay_ms=0)

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def setUp(self):
        self.mock.reset()

    def documents(self, partner, payload):
        self.mock.send(payload)
        return {row["kind"]: Document(row["payload"])
                for row in self.mock.mailbox(partner=partner, leave=False)}

    def check(self, partner, payload, po_number, currency):
        found = self.documents(partner, payload)
        state = self.mock.order(po_number)
        lines = {row["line"]: row for row in state["lines"]}

        response = found["response"].as_response()
        self.assertEqual(response.po_number, po_number)
        self.assertEqual(response.currency, currency)
        self.assertEqual(len(response.lines), len(lines))
        for line in response.lines:
            row = lines[line.number]
            self.assertEqual(line.sku, row["sku"])
            self.assertEqual(line.quantity, Decimal(str(row["quantity"])))
            self.assertEqual(line.confirmed, Decimal(str(row["confirmed"])))
            self.assertEqual(line.status, row["status"])
            self.assertEqual(line.price, Decimal(str(row["price"])))

        despatch = found["despatch"].as_despatch()
        shipment = state["shipments"][0]
        self.assertEqual(despatch.shipment_id, shipment["shipment_id"])
        self.assertEqual(despatch.po_number, po_number)
        self.assertEqual(despatch.carrier, shipment["carrier"])
        self.assertEqual(despatch.tracking, shipment["tracking"])
        self.assertEqual(despatch.cartons, shipment["cartons"])
        for item in despatch.items:
            self.assertEqual(item.quantity,
                             Decimal(str(lines[item.line]["shipped"])))

        invoice = found["invoice"].as_invoice()
        billed = state["invoices"][0]
        self.assertEqual(invoice.invoice_number, billed["invoice_number"])
        self.assertEqual(invoice.po_number, po_number)
        self.assertEqual(invoice.currency, currency)
        self.assertEqual(invoice.total, Decimal(str(billed["total"])))
        self.assertEqual(invoice.line_total, invoice.total)
        self.assertEqual(invoice.terms_days, billed["terms_days"])

    def test_x12_round_trips(self):
        self.check(ACME, x12_order("RT-X12"), "RT-X12", "USD")

    def test_edifact_round_trips(self):
        self.check(EURODIS, edifact_order("RT-EDI"), "RT-EDI", "EUR")

    def test_every_partner_and_every_behaviour_round_trips(self):
        """The behaviours that produce all three documents, for every partner.

        The interesting ones are `short-ship` and `reject-line`: a confirmed
        quantity that differs from the ordered one is exactly where a reader
        that took the wrong element would pass a happy-path test and fail
        here.
        """
        builders = {ACME: (x12_order, "USD"), GLOBEX: (x12_order, "USD"),
                    INITECH: (x12_order, "USD"),
                    EURODIS: (edifact_order, "EUR")}
        for partner, (builder, currency) in builders.items():
            for behaviour in ("accept", "short-ship", "reject-line"):
                with self.subTest(partner=partner, behaviour=behaviour):
                    self.mock.reset()
                    self.mock.behaviour(partner, behaviour)
                    po_number = "RT-%s-%s" % (partner[:4], behaviour[:5])
                    self.check(partner, builder(po_number, sender=partner),
                               po_number, currency)

    def test_a_change_response_round_trips(self):
        with Mock.start(despatch_delay_ms=3600000,
                        invoice_delay_ms=3600000) as mock:
            mock.send(x12_order("RT-CHANGE"))
            mock.send(x12_change("RT-CHANGE", [("1", "QD", 60, "12.50")]))
            rows = [row for row in mock.mailbox(partner=ACME)
                    if row["code"] == "865"]
            self.assertEqual(len(rows), 1)
            answer = Document(rows[0]["payload"]).as_change_response()
            self.assertEqual(answer.po_number, "RT-CHANGE")
            self.assertEqual(answer.sequence, "1")
            self.assertEqual(answer.lines[0].action, "QD")
            self.assertEqual(answer.lines[0].confirmed, Decimal("60"))

    def test_an_edifact_change_response_carries_its_action_per_line(self):
        # EDIFACT has no change acknowledgment message and no change
        # reference in the mock's profile, so `sequence` is empty and the
        # action on the line is what says which change was taken. The action
        # comes back as CA whatever it went out as, because 1229 has one code
        # for every kind of change.
        with Mock.start(despatch_delay_ms=3600000,
                        invoice_delay_ms=3600000) as mock:
            mock.send(edifact_order("RT-CHANGE-E"))
            mock.send(edifact_change("RT-CHANGE-E", [("1", "QD", 60, "12.50")]))
            rows = [row for row in mock.mailbox(partner=EURODIS)
                    if row["code"] == "ORDRSP"]
            answer = Document(rows[-1]["payload"]).as_change_response()
            self.assertEqual(answer.po_number, "RT-CHANGE-E")
            self.assertEqual(answer.sequence, "")
            self.assertEqual(answer.lines[0].action, "CA")
            self.assertEqual(answer.lines[0].confirmed, Decimal("60"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
