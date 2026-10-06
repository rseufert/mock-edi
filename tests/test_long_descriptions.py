"""A description longer than one element holds, written and read whole (#291).

A line's description is whatever the buyer sent, and the mock writes it back
into every document that answers the order, in the partner's dialect.
`PID05` holds 80 characters and D.96A's `7008` holds 35, so the mock could
accept a description and then write documents its own dictionary reports:
an 850 with a 100-character `PID05` came back as an 855 and an 810 each
over length, and since #286 a 40-character description does the same in
EDIFACT.

Both standards say a long description in pieces - X12 repeats `PID`, and an
`IMD` has two `7008`s and repeats - so that is what the mock writes, and
what it reads back as one text.

What arrives over length is still reported as over length. This is about
what the mock does next.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, schema, transactions, x12
from mockedi.envelope import seg

from support import (ACME, EURODIS, MockServerCase, edifact_order, x12_order)

SHORT = "Widget, blue, 40mm"
WORDS = ("Stainless steel mounting bracket with zinc plating, slotted for "
         "M6 fasteners, supplied in cartons of fifty with fitting notes")
OVER = "over length"


def words(count):
    """A description of exactly `count` characters, made of short words."""
    text = ("lorem ipsum dolor sit amet " * 20)[:count]
    return text[:-1] + "x" if text.endswith(" ") else text


class Pieces(unittest.TestCase):

    def test_a_text_that_fits_is_one_piece_and_untouched(self):
        for text in (SHORT, "two  spaces", " led by a space", "x" * 80):
            with self.subTest(text=text):
                self.assertEqual(transactions.pieces(text, 80), [text])

    def test_nothing_is_no_pieces(self):
        self.assertEqual(transactions.pieces("", 80), [])

    def test_every_piece_fits_and_they_join_back(self):
        for width in (35, 80):
            for count in (width + 1, 2 * width, 2 * width + 1, 200):
                with self.subTest(width=width, count=count):
                    text = words(count)
                    parts = transactions.pieces(text, width)
                    self.assertTrue(all(len(part) <= width for part in parts))
                    self.assertEqual(transactions.joined(parts), text)

    def test_a_cut_falls_at_a_space_where_there_is_one(self):
        parts = transactions.pieces(WORDS, 35)
        self.assertGreater(len(parts), 2)
        for part in parts:
            self.assertFalse(part.startswith(" ") or part.endswith(" "))
        self.assertEqual(" ".join(parts), WORDS)

    def test_a_word_longer_than_the_element_is_cut_where_it_must_be(self):
        parts = transactions.pieces("x" * 90, 35)
        self.assertEqual([len(part) for part in parts], [35, 35, 20])
        # The one thing lost: it reads back with a space at each cut.
        self.assertEqual(transactions.joined(parts).replace(" ", ""), "x" * 90)


class WhatIsWritten(unittest.TestCase):

    def test_x12_repeats_the_free_form_pid(self):
        segments = transactions.description_x12(words(200))
        self.assertEqual(len(segments), 3)
        for item in segments:
            self.assertEqual((item.tag, item.get(1)), ("PID", "F"))
            self.assertLessEqual(len(item.get(5)), 80)

    def test_edifact_fills_both_7008s_and_then_another_imd(self):
        segments = transactions.description_edifact(words(100))
        self.assertEqual(len(segments), 2)
        first, second = segments
        self.assertEqual((first.tag, first.get(1)), ("IMD", "F"))
        self.assertTrue(first.comp(3, 4) and first.comp(3, 5))
        self.assertTrue(second.comp(3, 4))
        for item in segments:
            for index in (4, 5):
                self.assertLessEqual(len(item.comp(3, index)), 35)

    def test_a_description_that_fits_is_the_segment_it_always_was(self):
        self.assertEqual(
            [item.elements for item in transactions.description_x12(SHORT)],
            [seg("PID", "F", "", "", "", SHORT).elements])
        self.assertEqual(
            [item.elements for item in transactions.description_edifact(SHORT)],
            [seg("IMD", "F", "", ["", "", "", SHORT]).elements])

    def test_there_are_never_more_segments_than_a_line_may_hold(self):
        # Found by Rusty: 100 free-form PIDs on one line are one description
        # of 8,099 characters, which is 116 IMDs, and a LIN group takes 99.
        endless = " ".join(["x" * 80] * 300)
        self.assertEqual(len(transactions.description_x12(endless)),
                         transactions.PID_MOST)
        self.assertEqual(len(transactions.description_edifact(endless)),
                         transactions.IMD_MOST)
        for width, most, declared in (
                (transactions.PID_MOST, "PID", schema.X12_855),
                (transactions.IMD_MOST, "IMD", schema.EDIFACT_ORDRSP),
                (transactions.IMD_MOST, "IMD", schema.EDIFACT_INVOIC),
                (transactions.IMD_MOST, "IMD", schema.EDIFACT_ORDERS)):
            allowed = [use.max_use for use, _loop in declared.uses()
                       if use.tag == most]
            self.assertEqual(min(allowed), width, declared.code)

    def test_no_description_is_no_segment(self):
        self.assertEqual(transactions.description_x12(""), [])
        self.assertEqual(transactions.description_edifact(""), [])


class WhatIsRead(unittest.TestCase):

    def order(self, *extra):
        body = [seg("BEG", "00", "SA", "PO-1", "", "20260101"),
                seg("PO1", "1", "10", "EA", "1.00", "", "VP", "A")] + list(extra)
        return transactions.read_order(x12.message("850", "0001", body), "X12")

    def test_two_free_form_pids_are_one_description(self):
        order = self.order(seg("PID", "F", "", "", "", "Bracket, zinc plated,"),
                           seg("PID", "F", "", "", "", "slotted for M6"))
        self.assertEqual(order.lines[0].description,
                         "Bracket, zinc plated, slotted for M6")

    def test_a_structured_pid_is_not_folded_into_it(self):
        order = self.order(seg("PID", "F", "", "", "", "Bracket"),
                           seg("PID", "S", "08", "VI", "BLU", "Blue"))
        self.assertEqual(order.lines[0].description, "Bracket")

    def test_with_no_free_form_pid_the_first_with_text_is_taken_as_before(self):
        order = self.order(seg("PID", "S", "08", "VI", "BLU", "Blue"),
                           seg("PID", "S", "91", "VI", "L", "Large"))
        self.assertEqual(order.lines[0].description, "Blue")

    def test_both_7008s_of_every_imd_are_one_description(self):
        body = [seg("BGM", ["220"], "PO-2", "9"),
                seg("LIN", "1", "", ["A", "VP"]),
                seg("IMD", "F", "", ["", "", "", "Bracket, zinc plated,",
                                     "slotted for M6,"]),
                seg("IMD", "F", "", ["", "", "", "cartons of fifty"]),
                seg("QTY", ["21", "10", "PCE"])]
        order = transactions.read_order(
            edifact.message("ORDERS", "1", body), "EDIFACT")
        self.assertEqual(
            order.lines[0].description,
            "Bracket, zinc plated, slotted for M6, cartons of fifty")


class RoundTrips(MockServerCase):
    """An order with a long description, answered in each dialect."""

    def descriptions(self, partner, kind, parse, reader):
        payload = self.mailbox(partner, kind)[0]["payload"]
        self.assertEqual([line for line in self.mock.findings(payload)
                          if OVER in line or "the maximum is" in line], [])
        message = next(parse(payload).messages())[1]
        return [line.description for line in reader(message).lines]

    def test_x12_at_and_past_the_width_of_pid05(self):
        for count in (80, 81, 200):
            with self.subTest(count=count):
                text = words(count)
                po_number = "LONG-X12-%d" % count
                summary = self.send(x12_order(
                    po_number, lines=[("WIDGET-001", 10, "12.50")],
                    extra=[seg("PID", "F", "", "", "", text)]))
                self.assertTrue(summary["accepted"])
                self.assertEqual(self.order(po_number)["lines"][0]["description"],
                                 text)
                payload = [row["payload"] for row in self.mailbox(ACME, "response")
                           if po_number in row["payload"]][0]
                said = self.mock.findings(payload)
                self.assertEqual(len(said), 1, said)
                response = transactions.read_response(
                    next(x12.parse(payload).messages())[1], "X12")
                self.assertEqual(response.lines[0].description, text)

    def test_edifact_at_and_past_one_7008_and_past_two(self):
        for count in (35, 36, 70, 71, 200):
            with self.subTest(count=count):
                text = words(count)
                po_number = "LONG-EDI-%d" % count
                self.send(edifact_order(
                    po_number, lines=[("PANEL-A4", 2, "89.00")],
                    extra=transactions.description_edifact(text)))
                self.assertEqual(self.order(po_number)["lines"][0]["description"],
                                 text)
                for kind, reader in (("response", transactions.read_response),
                                     ("invoice", transactions.read_invoice)):
                    payload = [row["payload"]
                               for row in self.mailbox(EURODIS, kind)
                               if po_number in row["payload"]][0]
                    said = self.mock.findings(payload)
                    self.assertEqual(len(said), 1, said)
                    read = reader(next(edifact.parse(payload).messages())[1],
                                  "EDIFACT")
                    self.assertEqual(read.lines[0].description, text)

    def test_the_case_that_found_it_an_x12_order_from_an_edifact_partner(self):
        text = "Stainless widget with a description forty-six chars"
        summary = self.send(x12_order(
            "LONG-CROSS", sender=EURODIS, lines=[("WIDGET-001", 10, "12.50")],
            extra=[seg("PID", "F", "", "", "", text)]))
        self.assertTrue(summary["accepted"])
        for kind in ("response", "invoice"):
            with self.subTest(kind=kind):
                payload = self.mailbox(EURODIS, kind)[0]["payload"]
                self.assertIn("UNB+", payload)
                said = self.mock.findings(payload)
                self.assertEqual(len(said), 1, said)
                self.assertTrue(said[0].endswith(": accepted"), said)

    def test_a_hundred_pids_from_an_edifact_partner_are_answered_legally(self):
        summary = self.send(x12_order(
            "LONG-MOST", sender=EURODIS, lines=[("WIDGET-001", 10, "12.50")],
            extra=[seg("PID", "F", "", "", "", "y" * 80) for _ in range(100)]))
        self.assertTrue(summary["accepted"])
        for kind in ("response", "invoice"):
            with self.subTest(kind=kind):
                payload = self.mailbox(EURODIS, kind)[0]["payload"]
                self.assertEqual(payload.count("IMD+F+"), 99)
                said = self.mock.findings(payload)
                self.assertEqual(len(said), 1, said)
                self.assertTrue(said[0].endswith(": accepted"), said)

    def test_what_arrives_over_length_is_still_said_to_be(self):
        summary = self.send(x12_order(
            "LONG-IN", lines=[("WIDGET-001", 10, "12.50")],
            extra=[seg("PID", "F", "", "", "", "x" * 100)]))
        self.assertTrue(summary["accepted"])
        findings = summary["transactionSets"][0]["findings"]
        self.assertTrue(any("PID05 is 100 characters, the maximum is 80" in line
                            for line in findings), findings)
        # And what the mock then writes is not: two PIDs, each one that fits.
        payload = self.mailbox(ACME, "response")[0]["payload"]
        said = self.mock.findings(payload)
        self.assertEqual(len(said), 1, said)

    def test_an_ordinary_order_is_answered_byte_for_byte_as_before(self):
        self.send(x12_order("LONG-NOT"))
        payload = self.mailbox(ACME, "response")[0]["payload"]
        pids = [line for line in payload.replace("~", "\n").splitlines()
                if line.startswith("PID*")]
        self.assertEqual(pids, ["PID*F****Widget, blue, 40mm",
                                "PID*F****Mounting bracket, 50mm"])


if __name__ == "__main__":
    unittest.main()
