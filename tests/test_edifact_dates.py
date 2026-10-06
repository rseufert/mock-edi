"""An EDIFACT date says what format it is in, and the mock reads it that way (#209).

`DTM`'s third component, 2379, is the format of the second: 102 is CCYYMMDD
and 203 is CCYYMMDDHHMM. Only eight-digit values were read, so an ORDERS
dated `DTM+137:202609241030:203` was accepted with no findings and stored
with no dates at all, and the ORDRSP then promised delivery as though none
had been asked for.

Two things, then: every format the dictionary lists is read for the date it
carries, and a value that cannot be read is a finding rather than a silence.
"""
import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, transactions, validate
from mockedi.envelope import parse_edifact_date, seg

from support import EURODIS, MockServerCase, edifact_order

EDIFACT = {"Content-Type": "application/edifact"}
ORDERED, WANTED = datetime.date(2026, 9, 24), datetime.date(2026, 10, 10)


def orders(ordered, wanted):
    """An ORDERS with its two dates written as given: (value, format)."""
    text = edifact_order("PO-DATES")
    for old, (value, form) in (("DTM+137:20260924:102", ordered),
                               ("DTM+2:20261010:102", wanted)):
        assert old in text
        qualifier = old.split(":")[0]
        text = text.replace(old, ":".join(p for p in (qualifier, value, form) if p))
    return text


def read(text):
    interchange = edifact.parse(text)
    order = transactions.read_order(interchange.groups[0].messages[0], "EDIFACT")
    notes = [element.note
             for message in validate.validate(interchange).messages
             for finding in message.segments for element in finding.elements]
    return (order.ordered_on, order.requested_on), notes


class EachFormat(unittest.TestCase):
    def test_102_is_a_date(self):
        self.assertEqual(read(orders(("20260924", "102"), ("20261010", "102"))),
                         ((ORDERED, WANTED), []))

    def test_203_is_a_date_and_a_time_and_the_date_is_read(self):
        self.assertEqual(
            read(orders(("202609241030", "203"), ("202610101200", "203"))),
            ((ORDERED, WANTED), []))

    def test_204_carries_seconds_as_well(self):
        self.assertEqual(
            read(orders(("20260924103059", "204"), ("20261010120000", "204"))),
            ((ORDERED, WANTED), []))

    def test_101_has_a_two_digit_year(self):
        self.assertEqual(read(orders(("260924", "101"), ("261010", "101"))),
                         ((ORDERED, WANTED), []))

    def test_an_unknown_format_is_reported_and_not_guessed_at(self):
        dates, notes = read(orders(("20260924", "102"), ("20261010", "718")))
        self.assertEqual(dates, (ORDERED, None))
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("718 is not a code", notes[0])

    def test_no_format_at_all_is_read_by_its_length(self):
        # 2379 is optional. Left out, the value is taken for what it looks like.
        self.assertEqual(read(orders(("20260924", ""), ("202610101200", ""))),
                         ((ORDERED, WANTED), []))


class AValueThatDoesNotFitItsFormat(unittest.TestCase):
    """Reported as an invalid date, and not read as one."""

    def refused(self, value, form, *fragments):
        dates, notes = read(orders(("20260924", "102"), (value, form)))
        self.assertEqual(dates, (ORDERED, None))
        self.assertEqual(len(notes), 1, notes)
        self.assertIn("not a valid date", notes[0])
        self.assertIn("2380", notes[0])
        for fragment in fragments:
            self.assertIn(fragment, notes[0])

    def test_eight_digits_called_203(self):
        # It used to be read, by the luck of its length.
        self.refused("20261010", "203", "CCYYMMDDHHMM")

    def test_twelve_digits_called_102(self):
        self.refused("202610101200", "102", "CCYYMMDD")

    def test_punctuation(self):
        self.refused("2026-10-10", "102")

    def test_a_day_that_does_not_exist(self):
        self.refused("20261340", "102")
        self.refused("20260230", "102")

    def test_a_time_that_does_not_exist(self):
        self.refused("202610102560", "203")
        self.refused("20261010120061", "204")

    def test_the_function_itself(self):
        for value, form, expected in (("20261010", "102", WANTED),
                                      ("202610101200", "203", WANTED),
                                      ("20261010120000", "204", WANTED),
                                      ("261010", "101", WANTED),
                                      ("20261010", "203", None),
                                      ("202610101200", "102", None),
                                      ("20261010", "718", None),
                                      ("", "102", None)):
            with self.subTest(value=value, form=form):
                self.assertEqual(parse_edifact_date(value, form), expected)


class EveryReaderThatTakesADate(unittest.TestCase):
    """ORDCHG, ORDRSP, DESADV and INVOIC read a 203 as ORDERS does."""

    def message(self, code, *body):
        return edifact.message(code, "1", list(body), version="D:96A:UN")

    def test_ordchg(self):
        change = transactions.read_change(self.message(
            "ORDCHG", seg("BGM", ["230"], ["PO-1"], "4"),
            seg("DTM", ["137", "202609241030", "203"]),
            seg("RFF", ["ON", "PO-1"])), "EDIFACT")
        self.assertEqual(change.changed_on, ORDERED)

    def test_ordrsp(self):
        response = transactions.read_response(self.message(
            "ORDRSP", seg("BGM", ["231"], ["SO-1"], "9", "AP"),
            seg("DTM", ["137", "202609241030", "203"]),
            seg("RFF", ["ON", "PO-1"]),
            seg("LIN", "1", "", ["W", "VP"]),
            seg("QTY", ["21", "5", "PCE"]), seg("QTY", ["113", "5", "PCE"]),
            seg("DTM", ["2", "202610101200", "203"])), "EDIFACT")
        self.assertEqual(response.responded_on, ORDERED)
        self.assertEqual(response.lines[0].scheduled_on, WANTED)

    def test_desadv(self):
        despatch = transactions.read_despatch(self.message(
            "DESADV", seg("BGM", ["351"], ["SH-1"], "9"),
            seg("DTM", ["11", "202609241030", "203"]),
            seg("RFF", ["ON", "PO-1"])), "EDIFACT")
        self.assertEqual(despatch.shipped_on, ORDERED)

    def test_invoic(self):
        invoice = transactions.read_invoice(self.message(
            "INVOIC", seg("BGM", ["380"], ["INV-1"], "9"),
            seg("DTM", ["137", "202609241030", "203"]),
            seg("RFF", ["ON", "PO-1"])), "EDIFACT")
        self.assertEqual(invoice.invoiced_on, ORDERED)


class OverTheWire(MockServerCase):
    def test_an_order_dated_with_203_keeps_its_dates(self):
        summary = self.send(
            orders(("202609241030", "203"), ("202610101200", "203")),
            headers=EDIFACT)
        self.assertTrue(summary["accepted"], summary)
        order = self.get("/_mock/orders/PO-DATES")[2]
        self.assertEqual((order["ordered_on"], order["requested_on"]),
                         ("2026-09-24", "2026-10-10"))

    def test_a_date_that_cannot_be_read_is_in_the_contrl(self):
        self.send(orders(("20260924", "102"), ("2026-10-10", "102")),
                  headers=EDIFACT)
        sent = self.mailbox(EURODIS, "acknowledgment")[0]["payload"]
        # 0085 has no word for a date of its own: 12, invalid value, at the
        # second component of the DTM's composite.
        # C507 is DTM's first data element and 0098 counts the tag, so 2
        # rather than 1; 2380 is still component 2 (#208).
        self.assertIn("UCD+12+2:2'", sent.replace("\n", ""))


if __name__ == "__main__":
    unittest.main()
