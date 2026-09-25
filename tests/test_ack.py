"""The acknowledgments: a 997 and a CONTRL built from the same report."""
import dataclasses
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, edifact, schema, validate, x12
from mockedi.envelope import seg

from support import EURODIS, MockServerCase, edifact_order, parse

GOOD = [seg("BEG", "00", "SA", "PO4711", "", "20260924"),
        seg("PO1", "1", "10", "EA", "12.50", "", "VP", "WIDGET-001"),
        seg("CTT", "1")]
# Findings that are noted but do not reject: an unknown code in BEG01 and an
# unknown unit in PO103. A receiver can still read the document.
BAD = [seg("BEG", "ZZ", "SA", "PO4711", "", "20260924"),
       seg("PO1", "1", "10", "XX", "12.50", "", "VP", "WIDGET-001"),
       seg("CTT", "1")]
# A finding that rejects: a quantity that is not a number has no reading to
# carry forward, so there is nothing to accept.
UNREADABLE = [seg("BEG", "00", "SA", "PO4711", "", "20260924"),
              seg("PO1", "1", "ten", "EA", "12.50", "", "VP", "WIDGET-001"),
              seg("CTT", "1")]


def x12_997(body, control="0001", group="88", carries_version=False):
    interchange = x12.wrap([x12.message("850", control, body)], "ACME",
                           "MOCKEDI", "1", group, "PO")
    report = validate.validate(interchange)
    segments = []
    for functional_id, group_control, version, messages in ack.group_reports(interchange, report):
        segments.extend(ack.functional_acknowledgment(
            functional_id, group_control, version, messages,
            carries_version=carries_version))
    return segments, report


def by_tag(segments, tag):
    return [item for item in segments if item.tag == tag]


class FunctionalAcknowledgment(unittest.TestCase):
    def test_a_clean_document_is_accepted_with_no_detail(self):
        segments, _report = x12_997(GOOD)
        self.assertEqual([s.tag for s in segments], ["AK1", "AK2", "AK5", "AK9"])
        self.assertEqual(by_tag(segments, "AK5")[0].get(1), "A")

    def test_ak1_names_the_group_being_acknowledged(self):
        # A 997 at 004010: AK1 has two elements, and no AK103 to put a
        # version in.
        segments, _report = x12_997(GOOD, group="88")
        ak1 = by_tag(segments, "AK1")[0]
        self.assertEqual(ak1.elements, ["PO", "88"])

    def test_a_005010_997_also_names_the_groups_version(self):
        segments, _report = x12_997(GOOD, group="88", carries_version=True)
        ak1 = by_tag(segments, "AK1")[0]
        self.assertEqual((ak1.get(1), ak1.get(2), ak1.get(3)), ("PO", "88", "004010"))

    def test_ak9_counts_included_received_and_accepted(self):
        segments, _report = x12_997(GOOD)
        ak9 = by_tag(segments, "AK9")[0]
        self.assertEqual((ak9.get(1), ak9.get(2), ak9.get(3), ak9.get(4)),
                         ("A", "1", "1", "1"))

    def test_a_finding_becomes_an_ak3_and_an_ak4(self):
        segments, _report = x12_997(BAD)
        ak3 = by_tag(segments, "AK3")
        ak4 = by_tag(segments, "AK4")
        self.assertEqual(ak3[0].get(1), "BEG")
        self.assertEqual(ak3[0].get(4), "8")        # has data element errors
        self.assertEqual(ak4[0].get(3), "7")        # invalid code value
        self.assertEqual(ak4[0].get(4), "ZZ")       # a copy of the bad data

    def test_a_quantity_that_is_not_a_number_rejects_the_set(self):
        """There is no reading of `ten` to carry forward, so nothing to accept."""
        segments, _report = x12_997(UNREADABLE)
        self.assertEqual(by_tag(segments, "AK5")[0].get(1), "R")
        ak4 = [s for s in by_tag(segments, "AK4") if s.get(4) == "ten"]
        self.assertEqual(ak4[0].get(3), "6")        # invalid character

    def test_ak3_carries_the_loop_identifier(self):
        segments, _report = x12_997(BAD)
        po1 = [s for s in by_tag(segments, "AK3") if s.get(1) == "PO1"][0]
        self.assertEqual(po1.get(3), "PO1")

    def test_errors_that_do_not_reject_give_ak5_e(self):
        segments, _report = x12_997(BAD)
        self.assertEqual(by_tag(segments, "AK5")[0].get(1), "E")
        self.assertEqual(by_tag(segments, "AK9")[0].get(1), "E")

    def test_a_fatal_finding_rejects_the_set(self):
        segments, _report = x12_997(GOOD[1:])
        self.assertEqual(by_tag(segments, "AK5")[0].get(1), "R")
        self.assertEqual(by_tag(segments, "AK9")[0].get(1), "R")
        self.assertEqual(by_tag(segments, "AK9")[0].get(4), "0")

    def test_the_bad_data_copy_is_clipped_to_ninety_nine_characters(self):
        body = [seg("BEG", "00", "SA", "P" * 200, "", "20260924")] + GOOD[1:]
        segments, _report = x12_997(body)
        for ak4 in by_tag(segments, "AK4"):
            self.assertLessEqual(len(ak4.get(4)), 99)

    def test_one_997_per_functional_group(self):
        messages = [x12.message("850", "0001", GOOD)]
        interchange = x12.wrap(messages, "ACME", "MOCKEDI", "1", "5", "PO")
        interchange.groups.append(
            x12.wrap([x12.message("810", "0009", [
                seg("BIG", "20260924", "INV1"), seg("TDS", "1000")])],
                "ACME", "MOCKEDI", "1", "6", "IN").groups[0])
        report = validate.validate(interchange)
        groups = ack.group_reports(interchange, report)
        self.assertEqual([g[0] for g in groups], ["PO", "IN"])


class SyntaxReport(unittest.TestCase):
    BODY = [seg("BGM", ["220"], ["PO1"], "9"),
            seg("LIN", "1", "", ["WIDGET-001", "VP"]),
            seg("QTY", ["21", "10", "PCE"]),
            seg("UNS", "S")]

    def build(self, body):
        interchange = edifact.wrap(
            [edifact.message("ORDERS", "7", body)], "EURODIS", "MOCKEDI", "9001")
        report = validate.validate(interchange)
        return ack.syntax_report(interchange, report, report.messages), report

    def test_uci_names_the_interchange_it_acknowledges(self):
        segments, _report = self.build(self.BODY)
        uci = segments[0]
        self.assertEqual(uci.tag, "UCI")
        self.assertEqual(uci.get(1), "9001")
        # The sender and recipient are the acknowledged interchange's, which
        # is why they look reversed.
        self.assertEqual(uci.comp(2, 1), "EURODIS")
        self.assertEqual(uci.comp(3, 1), "MOCKEDI")
        self.assertEqual(uci.get(4), ack.ACKNOWLEDGED)

    def test_ucm_echoes_the_senders_own_message_version(self):
        interchange = edifact.wrap(
            [edifact.message("ORDERS", "7", self.BODY, version="D:01B:UN")],
            "EURODIS", "MOCKEDI", "9001")
        report = validate.validate(interchange)
        segments = ack.syntax_report(interchange, report, report.messages)
        ucm = [s for s in segments if s.tag == "UCM"][0]
        self.assertEqual(ucm.raw(2), ["ORDERS", "D", "01B", "UN"])

    def test_a_finding_becomes_a_ucs_and_a_ucd(self):
        body = list(self.BODY)
        body[2] = seg("QTY", ["999", "10", "PCE"])
        segments, _report = self.build(body)
        ucs = [s for s in segments if s.tag == "UCS"][0]
        ucd = [s for s in segments if s.tag == "UCD"][0]
        self.assertEqual(ucs.get(1), "4")           # QTY is the fourth segment
        self.assertEqual(ucd.get(1), "12")          # invalid value
        self.assertEqual(ucd.comp(2, 1), "1")       # element 1
        self.assertEqual(ucd.comp(2, 2), "1")       # component 1

    def test_a_rejected_message_is_action_code_four(self):
        segments, _report = self.build(self.BODY[1:])   # no BGM
        ucm = [s for s in segments if s.tag == "UCM"][0]
        self.assertEqual(ucm.get(3), ack.REJECTED)


class ContrlCodesOnTheWire(MockServerCase):
    """0085 has its own word for most faults; the CONTRL uses it.

    Each case sends an ORDERS broken one way, reads the CONTRL that comes
    back, and checks it against the mock's own dictionary too.
    """

    EDIFACT = {"Content-Type": "application/edifact"}

    def contrl(self, payload):
        self.send(payload, headers=self.EDIFACT)
        rows = [r for r in self.mailbox(EURODIS, leave=False)
                if r["code"] == "CONTRL"]
        self.assertEqual(len(rows), 1)
        message = parse(rows[0]["payload"]).groups[0].messages[0]
        report = validate.validate_message(message, "EDIFACT")
        self.assertTrue(report.clean, report.summary())
        return message

    def ucm(self, message):
        return message.find("UCM")

    def test_an_element_too_long_is_39(self):
        order = edifact_order("P" * 40)            # BGM 1004 is at most 35
        message = self.contrl(order)
        self.assertEqual(self.ucm(message).get(4), "39")
        self.assertEqual(message.find("UCD").get(1), "39")

    def test_an_element_too_short_is_40(self):
        message = self.contrl(edifact_order("PO-SHORT").replace("CUX+2:EUR:9",
                                                                "CUX+2:EU:9"))
        self.assertEqual(self.ucm(message).get(4), "40")

    def test_a_letter_in_a_number_is_37(self):
        message = self.contrl(edifact_order("PO-TYPE").replace("QTY+21:100:",
                                                               "QTY+21:ten:", 1))
        self.assertEqual(self.ucm(message).get(4), "37")

    def test_a_unt_that_miscounts_is_29_on_unt(self):
        order = edifact_order("PO-COUNT")
        order = order.replace("UNT+16+1", "UNT+99+1")
        ucm = self.ucm(self.contrl(order))
        self.assertEqual((ucm.get(3), ucm.get(4), ucm.get(5)), ("4", "29", "UNT"))

    def test_a_unt_that_names_another_message_is_28_on_unt(self):
        order = edifact_order("PO-REF").replace("UNT+16+1", "UNT+16+2")
        ucm = self.ucm(self.contrl(order))
        self.assertEqual((ucm.get(4), ucm.get(5)), ("28", "UNT"))

    def test_a_message_type_the_mock_does_not_know_is_14_on_unh(self):
        order = edifact_order("PO-TYPE-X").replace("UNH+1+ORDERS:", "UNH+1+IFTSTA:")
        ucm = self.ucm(self.contrl(order))
        self.assertEqual((ucm.comp(2, 1), ucm.get(4), ucm.get(5)),
                         ("IFTSTA", "14", "UNH"))

    def test_a_sound_interchange_with_a_refused_message_is_uci_7(self):
        # UCI 4 means the interchange itself was at fault; this one was not.
        order = edifact_order("PO-UCI7").replace("UNT+16+1", "UNT+99+1")
        message = self.contrl(order)
        self.assertEqual(message.find("UCI").get(4), "7")
        self.assertEqual(self.ucm(message).get(3), "4")


class ContrlCodeGaps(MockServerCase):
    """Three the wire tests above leave open.

    A mapping with no test, the precedence between a set-level fault and the
    segments under it, and a guard that nothing maps to a code the table does
    not define.
    """

    EDIFACT = {"Content-Type": "application/edifact"}

    def ucm_code(self, payload):
        self.send(payload, headers=self.EDIFACT)
        rows = [r for r in self.mailbox(EURODIS, leave=False)
                if r["code"] == "CONTRL"]
        self.assertEqual(len(rows), 1)
        message = parse(rows[0]["payload"]).groups[0].messages[0]
        report = validate.validate_message(message, "EDIFACT")
        self.assertTrue(report.clean, report.summary())
        return message.find("UCM").get(4)

    def test_a_message_with_no_unt_at_all_is_13(self):
        # The fourth mapping, and the only one nothing was reading back off
        # the wire.
        order = re.sub(r"UNT\+\d+\+\d+'", "", edifact_order("NO-TRAILER"))
        self.assertEqual(self.ucm_code(order), "13")

    def test_a_set_level_fault_outranks_the_segments_beneath_it(self):
        # A UNT that miscounts on a message that also has a bad code in it.
        # The miscount is the fact about the message; 12 would describe the
        # element and say nothing about the count.
        order = re.sub(r"UNT\+\d+\+", "UNT+99+",
                       edifact_order("BOTH").replace("CUX+2:EUR:9", "CUX+2:ZZZ:9"))
        self.assertEqual(self.ucm_code(order), "29")

    def test_nothing_maps_to_a_code_the_table_does_not_define(self):
        # A mapping to a code that is not in 0085 would put a value in UCM04
        # that the mock's own dictionary rejects, and only a document that
        # happened to trigger that mapping would show it.
        mapped = {code for code, _segment in validate.SET_ERRORS_AS_0085.values()}
        element = validate.ElementFinding(position=1, component=0, ref="",
                                          code="", value="", note="")
        for x12_code in "123456789":
            mapped.add(
                dataclasses.replace(element, code=x12_code).edifact_code)
        self.assertEqual(mapped - set(schema.EDIFACT_SYNTAX_ERRORS), set())


class Explaining(unittest.TestCase):
    def test_findings_are_readable_prose(self):
        interchange = x12.wrap([x12.message("850", "0001", BAD)], "ACME",
                               "MOCKEDI", "1", "1", "PO")
        lines = ack.explain(validate.validate(interchange))
        self.assertIn("850/0001: accepted, with findings", lines)
        self.assertTrue(any("BEG at segment 2" in line for line in lines))

    def test_a_rejected_set_says_so(self):
        interchange = x12.wrap([x12.message("850", "0001", UNREADABLE)], "ACME",
                               "MOCKEDI", "1", "1", "PO")
        lines = ack.explain(validate.validate(interchange))
        self.assertIn("850/0001: rejected", lines)

    def test_a_clean_document_says_so_and_no_more(self):
        interchange = x12.wrap([x12.message("850", "0001", GOOD)], "ACME",
                               "MOCKEDI", "1", "1", "PO")
        self.assertEqual(ack.explain(validate.validate(interchange)),
                         ["850/0001: accepted"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
