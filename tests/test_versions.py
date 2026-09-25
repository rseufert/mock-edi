"""Which versions the mock speaks, and reading each by its own rules.

X12 sets are declared at 004010, and 005010 is described by the segments that
differ in it - ST03, AK103, AK203, and REF02 widened from 30 to 50. A set is
read against the version its group's GS08 names; a version the dictionary
does not have is refused in the 997 with the standard's own code for it; and a
997 at 004010 no longer carries AK103, which 004010 does not have.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import schema, validate, x12
from mockedi.envelope import seg

from support import ACME, GLOBEX, MockServerCase, parse, x12_order

BODY = [seg("BEG", "00", "SA", "PO4711", "", "20260924"),
        seg("PO1", "1", "10", "EA", "12.50", "", "VP", "WIDGET-001"),
        seg("CTT", "1")]


def check(body, version, control="0001", header=None):
    message = x12.message("850", control, body, version)
    if header is not None:
        message.segments[0] = header
    interchange = x12.parse(x12.render(x12.wrap(
        [message], "ACME", "MOCKEDI", "1", "1", "PO", version=version)))
    return validate.validate(interchange)


def notes(report):
    return [e.note for m in report.messages for f in m.segments for e in f.elements]


class TheDictionaryByVersion(unittest.TestCase):
    def test_ref02_is_thirty_characters_at_004010_and_fifty_at_005010(self):
        body = BODY[:1] + [seg("REF", "ZZ", "R" * 40)] + BODY[1:]
        self.assertTrue(any("maximum is 30" in n for n in notes(check(body, "004010"))))
        self.assertTrue(check(body, "005010").clean)

    def test_st03_exists_from_005010_on(self):
        header = seg("ST", "850", "0001", "005010X220A1")
        self.assertFalse(check(BODY, "004010", header=header).clean)
        self.assertTrue(check(BODY, "005010", header=header).clean)

    def test_an_industry_suffix_on_gs08_is_the_same_version(self):
        self.assertTrue(check(BODY, "004010VICS").clean)
        self.assertEqual(schema.base_version("X12", "005010X222A1"), "005010")

    def test_a_version_the_dictionary_lacks_is_refused_in_the_997_terms(self):
        report = check(BODY, "003040")
        self.assertEqual([code for code, _note in report.group_errors[("PO", "1")]],
                         ["2"])
        self.assertFalse(report.messages[0].accepted)


class WhatTheMockWrites(MockServerCase):
    def test_a_997_at_004010_has_no_ak103(self):
        self.send(x12_order("PO-V4010"))
        ak1 = self.document(ACME, "acknowledgment").groups[0].messages[0].find("AK1")
        self.assertEqual(ak1.elements, ["PO", ak1.get(2)])

    def test_everything_written_for_a_005010_partner_passes_005010(self):
        self.send(x12_order("PO-V5010", sender=GLOBEX))
        rows = self.mailbox(GLOBEX)
        self.assertTrue(rows)
        for row in rows:
            interchange = parse(row["payload"])
            self.assertEqual(interchange.groups[0].version, "005010")
            report = validate.validate(interchange)
            self.assertTrue(report.clean, (row["code"], [m.summary()
                                                         for m in report.messages]))
        ack = [parse(r["payload"]) for r in rows if r["code"] == "997"][0]
        self.assertEqual(ack.groups[0].messages[0].find("AK1").get(3), "004010")

    def test_an_order_in_an_unspoken_version_is_refused_and_not_acted_on(self):
        order = x12_order("PO-V3040").replace("*X*004010~", "*X*003040~")
        summary = self.send(order)
        self.assertEqual(summary["orders"], [])
        ak9 = self.document(ACME, "acknowledgment").groups[0].messages[0].find("AK9")
        self.assertEqual((ak9.get(1), ak9.get(5)), ("R", "2"))


class TheDictionaryEndpoint(MockServerCase):
    def test_it_says_which_versions_there_are(self):
        _s, _h, data = self.get("/_mock/dictionary")
        self.assertEqual(data["versions"], {"X12": ["004010", "005010"],
                                            "EDIFACT": ["D:96A:UN"]})

    def test_a_set_is_served_as_the_version_asked_for(self):
        def st(query):
            _s, _h, data = self.get("/_mock/dictionary/X12/850" + query)
            segment = [s for s in data["segments"] if s["tag"] == "ST"][0]
            return data["version"], [e["ref"] for e in segment["elements"]]
        self.assertEqual(st(""), ("004010", ["143", "329"]))
        self.assertEqual(st("?version=005010"), ("005010", ["143", "329", "1705"]))

    def test_a_version_it_does_not_have_is_said_so(self):
        _s, _h, data = self.get("/_mock/dictionary/X12/850?version=003040")
        self.assertIn("004010 and 005010", data["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
