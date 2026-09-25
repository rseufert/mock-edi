"""Validation: what the dictionary catches, and how badly it takes it."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mockedi import edifact, validate, x12
from mockedi.envelope import seg

GOOD = [seg("BEG", "00", "SA", "PO4711", "", "20260924"),
        seg("PO1", "1", "10", "EA", "12.50", "", "VP", "WIDGET-001"),
        seg("CTT", "1")]


def check(body, code="850", control="0001", strict=False, dialect="X12"):
    if dialect == "X12":
        interchange = x12.wrap([x12.message(code, control, body)],
                               "ACME", "MOCKEDI", "1", "1", "PO")
    else:
        interchange = edifact.wrap([edifact.message(code, control, body)],
                                   "EURODIS", "MOCKEDI", "1")
    return validate.validate(interchange, strict=strict)


def notes(report):
    out = []
    for message in report.messages:
        for finding in message.segments:
            out.extend(element.note for element in finding.elements)
            if not finding.elements:
                out.append(finding.note)
        out.extend(note for _code, note in message.set_errors)
    return out


class AGoodDocument(unittest.TestCase):
    def test_passes_with_nothing_to_say(self):
        report = check(GOOD)
        self.assertTrue(report.clean, notes(report))
        self.assertTrue(report.messages[0].accepted)
        self.assertEqual(report.group_code, "A")


class Elements(unittest.TestCase):
    def test_a_code_outside_the_list_is_reported_with_the_alternatives(self):
        report = check([seg("BEG", "ZZ", "SA", "P", "", "20260924")] + GOOD[1:])
        self.assertIn("ZZ is not a code BEG01 accepts (00, 01, 04, 05, 06, 07, ...)",
                      notes(report))

    def test_a_non_numeric_quantity_is_reported(self):
        report = check([GOOD[0], seg("PO1", "1", "ten", "EA", "1.00", "", "VP", "W")])
        self.assertIn("PO102 must be a number, got 'ten'", notes(report))

    def test_a_malformed_date_is_reported_as_a_date(self):
        report = check([seg("BEG", "00", "SA", "P", "", "2026-09-24")] + GOOD[1:])
        self.assertTrue(any("not a valid date" in note for note in notes(report)))

    def test_an_over_long_element_names_both_lengths(self):
        report = check([seg("BEG", "00", "SA", "P" * 30, "", "20260924")] + GOOD[1:])
        self.assertTrue(any("the maximum is 22" in note for note in notes(report)))

    def test_a_missing_mandatory_element_is_fatal(self):
        report = check([seg("BEG", "00", "SA", "", "", "20260924")] + GOOD[1:])
        self.assertFalse(report.messages[0].accepted)
        self.assertTrue(any("mandatory and empty" in note for note in notes(report)))

    def test_an_element_beyond_the_segments_definition_is_reported(self):
        report = check([GOOD[0], seg("CTT", "1", "10", "extra", "more")])
        self.assertTrue(any("has no element at position" in note
                            for note in notes(report)))


class Segments(unittest.TestCase):
    def test_an_unknown_segment_is_named(self):
        report = check(GOOD + [seg("ZZZ", "junk")])
        self.assertIn("ZZZ is not a segment this transaction set defines",
                      notes(report))

    def test_a_missing_mandatory_segment_is_fatal_and_rejects_the_set(self):
        report = check(GOOD[1:])
        self.assertFalse(report.messages[0].accepted)
        self.assertEqual(report.group_code, "R")
        self.assertTrue(any("BEG is mandatory" in note for note in notes(report)))

    def test_a_segment_used_more_often_than_allowed_is_reported(self):
        report = check([GOOD[0], GOOD[1], seg("CTT", "1"), seg("CTT", "1")])
        self.assertTrue(any("this is use 2" in note for note in notes(report)))

    def test_an_unknown_transaction_set_is_refused_by_number(self):
        report = check(GOOD, code="999")
        self.assertFalse(report.messages[0].accepted)
        self.assertIn(("1", "transaction set 999 is not one this mock implements"),
                      report.messages[0].set_errors)


class Loops(unittest.TestCase):
    """A loop's trigger repeating starts the next repetition, not a second use."""

    def test_a_repeated_trigger_is_a_new_repetition(self):
        body = [GOOD[0],
                seg("N1", "ST", "One", "92", "DOCK-1"), seg("N3", "1 Road"),
                seg("N1", "BT", "Two", "92", "HEAD-1"), seg("N3", "2 Road"),
                GOOD[1], seg("CTT", "1")]
        report = check(body)
        self.assertTrue(report.clean, notes(report))

    def test_a_nested_loop_returns_to_its_parent(self):
        body = [GOOD[0],
                seg("PO1", "1", "10", "EA", "1.00", "", "VP", "A"),
                seg("N1", "ST", "Dock", "92", "DOCK-1"),
                seg("PO1", "2", "20", "EA", "2.00", "", "VP", "B"),
                seg("N1", "ST", "Dock", "92", "DOCK-2"),
                seg("CTT", "2")]
        report = check(body)
        self.assertTrue(report.clean, notes(report))

    def test_a_segment_in_the_wrong_place_is_distinguished_from_an_unknown_one(self):
        # PID belongs to the 850, but only inside the PO1 loop. Arriving
        # before BEG it is misplaced, not unrecognised, and the 997 says so
        # with a different code.
        report = check([seg("PID", "F", "", "", "", "too early")] + GOOD)
        self.assertTrue(any("not at this point" in note for note in notes(report)),
                        notes(report))
        self.assertNotIn("PID is not a segment this transaction set defines",
                         notes(report))


class Trailers(unittest.TestCase):
    def test_a_segment_count_that_does_not_add_up_is_fatal(self):
        interchange = x12.wrap([x12.message("850", "0001", GOOD)],
                               "ACME", "MOCKEDI", "1", "1", "PO")
        interchange.groups[0].messages[0].find("SE").elements[0] = "99"
        report = validate.validate(interchange)
        self.assertFalse(report.messages[0].accepted)
        self.assertTrue(any("counts 99 segments" in note for note in notes(report)))

    def test_a_control_number_mismatch_is_reported(self):
        interchange = x12.wrap([x12.message("850", "0001", GOOD)],
                               "ACME", "MOCKEDI", "1", "1", "PO")
        interchange.groups[0].messages[0].find("SE").elements[1] = "0002"
        report = validate.validate(interchange)
        self.assertTrue(any("the header says 0001" in note
                            for note in notes(report)))


class Severity(unittest.TestCase):
    def test_a_soft_finding_is_accepted_with_errors(self):
        report = check([seg("BEG", "ZZ", "SA", "P", "", "20260924")] + GOOD[1:])
        self.assertTrue(report.messages[0].accepted)
        self.assertEqual(report.group_code, "E")

    def test_strict_refuses_the_same_document(self):
        report = check([seg("BEG", "ZZ", "SA", "P", "", "20260924")] + GOOD[1:],
                       strict=True)
        self.assertFalse(report.messages[0].accepted)
        self.assertEqual(report.group_code, "R")

    def test_a_mixture_is_partially_accepted(self):
        interchange = x12.wrap(
            [x12.message("850", "0001", GOOD), x12.message("850", "0002", GOOD[1:])],
            "ACME", "MOCKEDI", "1", "1", "PO")
        report = validate.validate(interchange)
        self.assertEqual(report.group_code, "P")
        self.assertEqual((report.accepted, report.received), (1, 2))


class EdifactComposites(unittest.TestCase):
    BODY = [seg("BGM", ["220"], ["PO1"], "9"),
            seg("LIN", "1", "", ["WIDGET-001", "VP"]),
            seg("QTY", ["21", "10", "PCE"]),
            seg("UNS", "S")]

    def test_a_good_message_passes(self):
        report = check(self.BODY, code="ORDERS", control="1", dialect="EDIFACT")
        self.assertTrue(report.clean, notes(report))

    def test_a_bad_component_is_reported_by_its_own_number(self):
        body = list(self.BODY)
        body[2] = seg("QTY", ["999", "10", "PCE"])
        report = check(body, code="ORDERS", control="1", dialect="EDIFACT")
        self.assertTrue(any("QTY01/6063" in note for note in notes(report)))

    def test_an_absent_optional_composite_does_not_require_its_components(self):
        body = list(self.BODY)
        # NAD's C058 is optional; the mandatory element inside it is only
        # mandatory when the composite is there at all.
        body.insert(1, seg("NAD", "BY", ["ACME", "", "92"]))
        report = check(body, code="ORDERS", control="1", dialect="EDIFACT")
        self.assertTrue(report.clean, notes(report))


class EdifactVersions(unittest.TestCase):
    BODY = EdifactComposites.BODY

    def report(self, code, version, body=None):
        message = edifact.message(code, "1", body or self.BODY, version=version)
        return validate.validate_message(message, "EDIFACT")

    def test_a_message_in_another_directory_is_an_error_on_unh(self):
        report = self.report("ORDERS", "D:01B:UN")
        finding = report.segments[0]
        self.assertEqual((finding.tag, finding.position), ("UNH", 1))
        self.assertEqual([(e.position, e.component, e.ref, e.value)
                          for e in finding.elements], [(2, 3, "0054", "01B")])
        self.assertTrue(report.accepted)       # an error, not fatal

    def test_a_contrl_that_names_a_business_directory_is_caught(self):
        body = [seg("UCI", "1", ["EURODIS", "ZZ"], ["MOCKEDI", "ZZ"], "7")]
        report = self.report("CONTRL", "D:96A:UN", body)
        self.assertIn("UNH02 names CONTRL:D:96A:UN, the dictionary defines "
                      "CONTRL:D:3:UN", report.summary())
        self.assertTrue(self.report("CONTRL", "D:3:UN", body).clean)


if __name__ == "__main__":
    unittest.main(verbosity=2)
