"""The `IMD` free-form codes: all eight of them, and which hold a description.

D.96A's `7077` has eight codes. The mock listed five, and had two of them the
wrong way round: `A` is the **long** description and `E` the **short** one,
where the mock said `A` was short and `E` was simply "Free-form" (#317).

Two things followed. A partner sending `IMD+D`, `IMD+S` or `IMD+X` - all
real codes - drew a finding against a correct document, which is the
expensive direction: a document that should pass turning red. And a long
description sent as `IMD+A`, the code whose name is literally "long
description", was read back cut short, which is the inbound half of #291.

Reading needed more than a wider filter. `IMD` repetition *continues* a
description, which is why #291 joins several `IMD+F` - but a different
`7077` code starts a *different* description, so `A` and `E` together are a
long rendering and a short one, not two halves. Joining those would give
the long description with the short one stuck on the end, which is worse
than the cut-short string. So: join within a code, choose between codes.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, schema, transactions, validate
from mockedi.envelope import seg

from support import EURODIS, MockServerCase, edifact_order

# Long enough to need two `IMD`s, so the join is doing work.
LONG = ("Stainless steel mounting bracket with zinc plating, slotted for M6 "
        "fasteners, supplied in cartons of fifty")
SHORT = "Bracket 40mm"


def imd(code, *parts):
    """An `IMD` of `code` carrying `parts` in its two `7008` components."""
    return seg("IMD", code, "", ["", "", ""] + list(parts))


def line(*extra):
    """A one-line ORDERS carrying `extra` in that line's block."""
    body = [seg("BGM", ["220"], "PO-2", "9"),
            seg("LIN", "1", "", ["A", "VP"])] + list(extra) + [
            seg("QTY", ["21", "10", "PCE"])]
    return transactions.read_order(
        edifact.message("ORDERS", "1", body), "EDIFACT")


class TheCodeTable(unittest.TestCase):

    def codes(self):
        return schema.IMD.elements[0].codes

    def test_all_eight_codes_the_directory_has(self):
        self.assertEqual(sorted(self.codes()),
                         ["A", "B", "C", "D", "E", "F", "S", "X"])

    def test_a_is_the_long_description_and_e_the_short_one(self):
        # The pair that was the wrong way round, which is why the reader's
        # filter looked complete when it was not.
        self.assertEqual(self.codes()["A"], "Free-form long description")
        self.assertEqual(self.codes()["E"], "Free-form short description")

    def test_the_three_that_were_missing_are_named_as_the_directory_names_them(self):
        self.assertEqual(self.codes()["D"], "Free-form price look up")
        self.assertEqual(self.codes()["S"],
                         "Structured (from industry code list)")
        self.assertEqual(self.codes()["X"], "Semi-structured (code + text)")


class WhatValidateSaysAboutThem(unittest.TestCase):
    """The over-refusing half: a real code must not report a good document."""

    def findings(self, code):
        message = edifact.message("ORDERS", "1", [
            seg("BGM", ["220"], "PO-9", "9"),
            seg("LIN", "1", "", ["WIDGET-1", "VP"]),
            imd(code, "Blue widget"),
            seg("QTY", ["21", "5", "PCE"])])
        report = validate.validate_message(message, "EDIFACT")
        return [(element.code, element.note)
                for segment in report.segments if segment.tag == "IMD"
                for element in segment.elements]

    def test_every_code_the_directory_has_is_clean(self):
        for code in ("A", "B", "C", "D", "E", "F", "S", "X"):
            with self.subTest(code=code):
                self.assertEqual(self.findings(code), [])

    def test_a_code_the_directory_does_not_have_is_still_reported(self):
        # The control: the check works, and it was the table that was short.
        (finding,) = self.findings("Z")
        self.assertEqual(finding[0], "7")
        self.assertIn("Z", finding[1])


class JoiningWithinOneCode(unittest.TestCase):

    def test_several_imds_of_one_code_are_one_description(self):
        read = line(imd("A", "Stainless steel mounting bracket with",
                        "zinc plating, slotted for M6"),
                    imd("A", "fasteners, supplied in cartons of fifty"))
        self.assertEqual(read.lines[0].description, LONG)

    def test_both_7008s_of_a_coded_imd_are_read(self):
        read = line(imd("E", "Bracket", "40mm"))
        self.assertEqual(read.lines[0].description, "Bracket 40mm")

    def test_a_blank_code_still_joins_with_f(self):
        # #291's behaviour, kept: a sender that omits 7077 on a continuation
        # segment is saying nothing, not saying something else.
        read = line(imd("F", "Bracket, zinc plated,"),
                    imd("", "slotted for M6"))
        self.assertEqual(read.lines[0].description,
                         "Bracket, zinc plated, slotted for M6")

    def test_the_mocks_own_long_description_still_reads_back_whole(self):
        # Not this issue's case - `description_edifact` writes `F`, which
        # #291 already joined - but the one this change could break, since
        # it rewrites the reader #291 added. Passes with `mockedi/` at
        # `main` too, and that is the point of it.
        read = line(*transactions.description_edifact(LONG))
        self.assertEqual(read.lines[0].description, LONG)


class ChoosingBetweenCodes(unittest.TestCase):

    def test_a_long_description_and_a_short_one_are_not_run_together(self):
        read = line(imd("A", "Stainless steel mounting bracket with",
                        "zinc plating, slotted for M6"),
                    imd("A", "fasteners, supplied in cartons of fifty"),
                    imd("E", SHORT))
        self.assertEqual(read.lines[0].description, LONG)
        self.assertNotIn(SHORT, read.lines[0].description)

    def test_f_is_preferred_over_a(self):
        read = line(imd("A", "The long one"), imd("F", "The plain one"))
        self.assertEqual(read.lines[0].description, "The plain one")

    def test_a_is_preferred_over_e(self):
        read = line(imd("E", SHORT), imd("A", "The long one"))
        self.assertEqual(read.lines[0].description, "The long one")

    def test_the_order_in_the_document_does_not_decide_it(self):
        # The preference is over codes, not over position: F last in the
        # document still wins.
        for segments in ((imd("F", "Plain"), imd("A", "Long")),
                         (imd("A", "Long"), imd("F", "Plain"))):
            with self.subTest(first=segments[0].get(1)):
                self.assertEqual(line(*segments).lines[0].description, "Plain")

    def test_d_is_read_when_it_is_all_there_is(self):
        # Passes with `mockedi/` at `main` as well, by the older fallback
        # rather than by `D` being a description. Here it is both.
        read = line(imd("D", "BRKT BLU"))
        self.assertEqual(read.lines[0].description, "BRKT BLU")

    def test_but_d_loses_to_every_other_free_form_code(self):
        for better in ("F", "A", "E"):
            with self.subTest(better=better):
                read = line(imd("D", "BRKT BLU"), imd(better, "A real one"))
                self.assertEqual(read.lines[0].description, "A real one")


class TheCodesThatAreNotDescriptions(unittest.TestCase):
    """`B` and `X` pair a code with a gloss; `C` and `S` are codes alone."""

    def test_a_structured_imd_is_not_folded_into_a_free_form_one(self):
        read = line(imd("F", "Bracket"), imd("S", "Blue"))
        self.assertEqual(read.lines[0].description, "Bracket")

    def test_with_no_free_form_imd_the_first_with_text_is_taken_as_before(self):
        # The older fallback, unchanged: these are not descriptions, so
        # something is better than nothing and joining them is not right.
        read = line(imd("S", "Blue"), imd("X", "Large"))
        self.assertEqual(read.lines[0].description, "Blue")

    def test_a_line_with_no_imd_at_all_has_no_description(self):
        self.assertEqual(line().lines[0].description, "")


class RoundTrips(MockServerCase):
    """Through a running mock, which is where the fault was reachable."""

    def answers(self, po_number):
        out = []
        for kind, reader in (("response", transactions.read_response),
                             ("invoice", transactions.read_invoice)):
            payload = self.mailbox(EURODIS, kind)[-1]["payload"]
            message = next(edifact.parse(payload).messages())[1]
            out.append([row.description for row in reader(message, "EDIFACT").lines])
        return out

    def test_a_long_description_sent_as_imd_a_is_answered_with_it(self):
        po_number = "IMD-A-LONG"
        parts = transactions.description_edifact(LONG)
        self.assertGreater(len(parts), 1, "the join must be doing work")
        summary = self.send(edifact_order(
            po_number, lines=[("PANEL-A4", 2, "89.00")],
            extra=[imd("A", *[p.comp(3, index) for index in (4, 5)])
                   for p in parts]))
        self.assertTrue(summary["accepted"], summary)
        self.assertEqual(self.order(po_number)["lines"][0]["description"], LONG)
        for descriptions in self.answers(po_number):
            self.assertEqual(descriptions, [LONG])

    def test_an_order_offering_both_renderings_records_the_long_one(self):
        po_number = "IMD-A-AND-E"
        self.send(edifact_order(
            po_number, lines=[("PANEL-A4", 2, "89.00")],
            extra=[imd("A", "Stainless steel mounting bracket with",
                       "zinc plating, slotted for M6"),
                   imd("A", "fasteners, supplied in cartons of fifty"),
                   imd("E", SHORT)]))
        self.assertEqual(self.order(po_number)["lines"][0]["description"], LONG)

    def test_an_order_whose_imd_is_coded_s_is_accepted_clean(self):
        # The over-refusing half, through the door rather than through
        # `validate`: this drew a finding before.
        po_number = "IMD-S-CLEAN"
        summary = self.send(edifact_order(
            po_number, lines=[("PANEL-A4", 2, "89.00")],
            extra=[imd("S", "Blue")]))
        self.assertTrue(summary["accepted"], summary)
        self.assertEqual([note for note in self.mock.findings(
            edifact_order(po_number + "-X", lines=[("PANEL-A4", 2, "89.00")],
                          extra=[imd("S", "Blue")])) if "IMD" in note], [])

    def test_the_mock_still_writes_f(self):
        po_number = "IMD-WRITES-F"
        self.send(edifact_order(po_number, lines=[("PANEL-A4", 2, "89.00")],
                                extra=[imd("F", SHORT)]))
        payload = self.mailbox(EURODIS, "response")[-1]["payload"]
        self.assertIn("IMD+F+", payload)


if __name__ == "__main__":
    unittest.main()
