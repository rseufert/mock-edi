"""The envelope: trailers that do not add up, files cut short, and the TA1."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import validate, x12

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order

EDIFACT = {"Content-Type": "application/edifact"}


def isa_with(text, **changes):
    """The interchange with some ISA elements replaced, by 1-based position."""
    isa = text.split("~")[0]
    parts = isa.split("*")
    for position, value in changes.items():
        parts[int(position[1:])] = value
    return text.replace(isa, "*".join(parts), 1)


def ta1(payload):
    """The TA1 segment of an interchange, as a list of its elements."""
    for line in payload.replace("\n", "").split("~"):
        if line.startswith("TA1*"):
            return line.split("*")
    return None


class X12GroupTrailers(MockServerCase):
    """GE is checked against GS and against what the group holds."""

    def test_a_ge_that_miscounts_rejects_the_group_with_ak905_5(self):
        summary = self.send(x12_order("GE-COUNT").replace("GE*1*", "GE*9*"))
        self.assertEqual([q["code"] for q in summary["queued"]], ["997"])
        ak9 = self.document(ACME, "acknowledgment").groups[0].messages[0].find("AK9")
        self.assertEqual(ak9.elements, ["R", "1", "1", "0", "5"])

    def test_a_ge_control_number_that_disagrees_with_gs06_is_ak905_4(self):
        text = x12_order("GE-CONTROL")
        ge = [line for line in text.split("~") if line.strip().startswith("GE*")][0]
        summary = self.send(text.replace(ge, ge.strip().rsplit("*", 1)[0] + "*999"))
        self.assertEqual([q["code"] for q in summary["queued"]], ["997"])
        ak9 = self.document(ACME, "acknowledgment").groups[0].messages[0].find("AK9")
        self.assertEqual((ak9.get(1), ak9.get(5)), ("R", "4"))

    def test_nothing_in_a_rejected_group_is_acted_on(self):
        self.send(x12_order("GE-IGNORED").replace("GE*1*", "GE*9*"))
        status, _headers, _data = self.get("/_mock/orders/GE-IGNORED")
        self.assertEqual(status, 404)
        self.assertEqual(self.mailbox(ACME, "response"), [])

    def test_the_997_that_says_so_passes_the_mocks_own_dictionary(self):
        self.send(x12_order("GE-VALID").replace("GE*1*", "GE*9*"))
        message = self.document(ACME, "acknowledgment").groups[0].messages[0]
        report = validate.validate_message(message, "X12")
        self.assertTrue(report.clean, report.summary())


class X12InterchangeTrailers(MockServerCase):
    """IEA against ISA, and a file that simply stops. The answer is a TA1."""

    def refused(self, text, note):
        summary = self.send(text)
        # Nothing inside a refused envelope was read: a TA1, and no 997.
        self.assertEqual([q["code"] for q in summary["queued"]], ["TA1"])
        rows = self.mailbox(ACME, "interchange-acknowledgment")
        self.assertEqual(len(rows), 1)
        self.assertEqual(ta1(rows[0]["payload"])[4:6], ["R", note])
        self.assertEqual(self.mailbox(ACME, "response"), [])

    def test_a_file_cut_off_after_se_is_premature_end_of_file(self):
        text = x12_order("CUT-SHORT")
        self.refused(text[:text.index("GE*")], "023")

    def test_an_iea_that_renumbers_the_interchange_is_001(self):
        self.refused(x12_order("IEA-CONTROL").replace("IEA*1*000000077",
                                                      "IEA*1*000000001"), "001")

    def test_an_iea_that_miscounts_the_groups_is_021(self):
        self.refused(x12_order("IEA-COUNT").replace("IEA*1*", "IEA*7*"), "021")

    def test_a_truncated_file_is_not_clean_on_validate(self):
        text = x12_order("CUT-VALIDATE")
        status, _headers, data = self.post("/_mock/validate", text[:text.index("GE*")])
        self.assertEqual(status, 200, data)
        self.assertFalse(data["clean"])
        self.assertEqual(data["groupCode"], "R")
        self.assertTrue(any("without an IEA" in line for line in data["explain"]),
                        data["explain"])


class TheTA1(MockServerCase):
    def test_isa14_asks_for_one_and_a_clean_envelope_gets_an_a(self):
        summary = self.send(isa_with(x12_order("TA1-ASKED"), I14="1"))
        self.assertEqual([q["code"] for q in summary["queued"]][:2], ["TA1", "997"])
        payload = self.mailbox(ACME, "interchange-acknowledgment")[0]["payload"]
        segment = ta1(payload)
        # TA101-TA103 quote the ISA being answered: its control, date, time.
        isa = x12_order("TA1-ASKED").split("~")[0].split("*")
        self.assertEqual(segment, ["TA1", isa[13], isa[9], isa[10], "A", "000"])
        # It travels in an envelope of its own, with no functional group.
        interchange = x12.parse(payload)
        self.assertEqual(interchange.groups, [])
        self.assertEqual(interchange.trailer.get(1), "0")
        self.assertEqual(interchange.header.get(13), interchange.trailer.get(2))

    def test_no_ta1_when_nobody_asked_and_nothing_is_wrong(self):
        summary = self.send(x12_order("TA1-QUIET"))
        self.assertNotIn("TA1", [q["code"] for q in summary["queued"]])

    def test_an_isa_off_its_fixed_widths_is_noted_and_still_read(self):
        text = isa_with(x12_order("ISA-WIDTH"), I06="ACME")
        summary = self.send(text)
        self.assertEqual([q["code"] for q in summary["queued"]][:3],
                         ["TA1", "997", "855"])
        payload = self.mailbox(ACME, "interchange-acknowledgment")[0]["payload"]
        self.assertEqual(ta1(payload)[4:6], ["E", "006"])


class EdifactTrailers(MockServerCase):
    """UNZ against UNB. The CONTRL says so in UCI and gives no UCM."""

    def refused(self, text, code):
        summary = self.send(text, headers=EDIFACT)
        self.assertEqual([q["code"] for q in summary["queued"]], ["CONTRL"])
        message = self.document(EURODIS, "acknowledgment").groups[0].messages[0]
        uci = message.find("UCI")
        self.assertEqual((uci.get(4), uci.get(5), uci.get(6)), ("4", code, "UNZ"))
        self.assertIsNone(message.find("UCM"))
        self.assertTrue(validate.validate_message(message, "EDIFACT").clean)
        return uci

    def test_a_unz_that_miscounts_is_29(self):
        uci = self.refused(edifact_order("UNZ-COUNT").replace("UNZ+1+", "UNZ+3+"),
                           "29")
        self.assertEqual(uci.comp(7, 1), "1")        # UNZ element 1

    def test_a_unz_that_names_another_interchange_is_28(self):
        self.refused(edifact_order("UNZ-REF").replace("UNZ+1+9001", "UNZ+1+9002"),
                     "28")

    def test_a_file_cut_off_before_unz_is_13(self):
        text = edifact_order("UNZ-CUT")
        self.refused(text[:text.index("UNZ")], "13")


if __name__ == "__main__":
    unittest.main(verbosity=2)
