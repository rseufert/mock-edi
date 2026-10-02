"""The envelope: trailers that do not add up, files cut short, and the TA1."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import validate, x12

from mockedi.envelope import seg

from support import (ACME, EURODIS, MockServerCase, _next_control,
                     edifact_order, x12_order)

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
        # One more than GS06, rather than a fixed 999: GS06 comes from a counter
        # shared by the whole run, and on the run where it reached 999 a fixed
        # value agreed with it and the group went through.
        count, control = ge.strip().split("*")[1:3]
        summary = self.send(text.replace(ge, "GE*%s*%d" % (count, int(control) + 1)))
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


def order_body(po_number, purpose="00"):
    return [seg("BEG", purpose, "SA", po_number, "", "20260924"),
            seg("N1", "ST", "Acme DC 4", "92", "ACME-DC4"),
            seg("PO1", "1", "10", "EA", "12.50", "", "VP", "WIDGET-001"),
            seg("CTT", "1")]


def two_orders(second_purpose="00"):
    """Two 850s in one group; the second can be given a BEG01 that is no code."""
    control = _next_control(9)
    return x12.render(x12.wrap(
        [x12.message("850", "0001", order_body("PO-ONE")),
         x12.message("850", "0002", order_body("PO-TWO", second_purpose))],
        ACME, "MOCKEDI", control, control.lstrip("0") or "1", "PO"), newline=True)


class The997ForARejectedGroup(MockServerCase):
    """No set inside a rejected group is acknowledged as accepted (#205).

    A group whose GE miscounts is rejected whole, however clean its sets. The
    997 used to say `AK5*A` for each clean set beside `AK9*R`, and a reader
    that takes its verdict from AK5 - the usual kind - saw an accepted order
    the mock had dropped. Checked on the bytes, since that is what is read.
    """

    def body(self, payload):
        """The 997's own segments, AK1 to AK9, as the text that was sent."""
        self.send(payload)
        sent = self.mailbox(ACME, "acknowledgment")[0]["payload"]
        lines = [line.strip() for line in sent.split("~")]
        return [line for line in lines if line.startswith("AK")]

    def test_a_clean_set_under_a_miscounting_ge_is_rejected_not_accepted(self):
        text = x12_order("GE-COUNT-BYTES")
        group = [line.strip() for line in text.split("~")
                 if line.strip().startswith("GS*")][0].split("*")[6]
        self.assertEqual(self.body(text.replace("GE*1*", "GE*9*")),
                         ["AK1*PO*%s" % group, "AK2*850*0001", "AK5*R",
                          "AK9*R*1*1*0*5"])

    def test_the_same_when_the_ge_control_number_is_wrong(self):
        text = x12_order("GE-CONTROL-BYTES")
        ge = [line for line in text.split("~") if line.strip().startswith("GE*")][0]
        count, control = ge.strip().split("*")[1:3]
        body = self.body(text.replace(ge, "GE*%s*%d" % (count, int(control) + 1)))
        self.assertEqual(body[1:], ["AK2*850*0001", "AK5*R", "AK9*R*1*1*0*4"])

    def test_every_set_is_named_and_none_says_accepted(self):
        body = self.body(two_orders().replace("GE*2*", "GE*9*"))
        self.assertEqual(body[1:], ["AK2*850*0001", "AK5*R",
                                    "AK2*850*0002", "AK5*R", "AK9*R*2*2*0*5"])

    def test_a_set_with_an_error_of_its_own_still_says_what_it_was(self):
        body = self.body(two_orders(second_purpose="ZZ").replace("GE*2*", "GE*9*"))
        self.assertEqual(body[1:3], ["AK2*850*0001", "AK5*R"])
        self.assertEqual(body[3], "AK2*850*0002")
        self.assertTrue(body[4].startswith("AK3*BEG*"), body)
        self.assertTrue(body[5].startswith("AK4*1*353*7"), body)
        self.assertEqual(body[6:], ["AK5*R*5", "AK9*R*2*2*0*5"])

    def test_nothing_in_any_rejected_group_says_a_or_e(self):
        for payload in (x12_order("GE-A").replace("GE*1*", "GE*9*"),
                        two_orders("ZZ").replace("GE*2*", "GE*9*")):
            for line in self.body(payload):
                if line.startswith("AK5"):
                    self.assertEqual(line.split("*")[1], "R", line)
            self.post("/_mock/reset")

    def test_an_accepted_group_is_acknowledged_as_it_was(self):
        self.assertEqual(self.body(two_orders())[1:],
                         ["AK2*850*0001", "AK5*A", "AK2*850*0002", "AK5*A",
                          "AK9*A*2*2*2"])

    def test_the_same_flawed_set_in_an_accepted_group_is_still_e(self):
        # The set that is `AK5*R*5` above, when its group is sound: accepted
        # with errors, as before. Only the group's rejection makes it R.
        body = self.body(two_orders(second_purpose="ZZ"))
        self.assertEqual(body[1:3], ["AK2*850*0001", "AK5*A"])
        self.assertEqual(body[-2:], ["AK5*E*5", "AK9*E*2*2*2"])


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
        text = x12_order("IEA-CONTROL", control="000000077")
        self.refused(text.replace("IEA*1*000000077", "IEA*1*000000001"), "001")

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
        # Built once: each order carries a fresh control number, so a second
        # call would not be the interchange that was sent.
        sent = x12_order("TA1-ASKED")
        summary = self.send(isa_with(sent, I14="1"))
        self.assertEqual([q["code"] for q in summary["queued"]][:2], ["TA1", "997"])
        payload = self.mailbox(ACME, "interchange-acknowledgment")[0]["payload"]
        segment = ta1(payload)
        # TA101-TA103 quote the ISA being answered: its control, date, time.
        isa = sent.split("~")[0].split("*")
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
        text = edifact_order("UNZ-REF", control="9001")
        self.refused(text.replace("UNZ+1+9001", "UNZ+1+9002"), "28")

    def test_a_file_cut_off_before_unz_is_13(self):
        text = edifact_order("UNZ-CUT")
        self.refused(text[:text.index("UNZ")], "13")


if __name__ == "__main__":
    unittest.main(verbosity=2)
