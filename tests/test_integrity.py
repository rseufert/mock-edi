"""An interchange is taken whole or not at all.

Two ways it used not to be: a line number used twice in one order was a 500
that deleted the lines of the order it restated, and any failure halfway
through receiving left whatever had been written by then committed.
"""
import contextlib
import io
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import validate

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order

TWO_LINES = (("WIDGET-001", 10, "12.50"), ("BRKT-050", 5, "4.15"))
EDIFACT = {"Content-Type": "application/edifact"}


class DuplicateLineNumbers(MockServerCase):
    def test_an_850_that_numbers_two_lines_1_is_rejected_by_its_997(self):
        text = x12_order("DUP-X12", lines=TWO_LINES).replace("PO1*2*", "PO1*1*")
        summary = self.send(text)
        self.assertEqual(summary["orders"], [])
        self.assertEqual([q["code"] for q in summary["queued"]], ["997"])

        message = self.document(ACME, "acknowledgment").groups[0].messages[0]
        ak3 = message.find("AK3")
        ak4 = message.find("AK4")
        self.assertEqual((ak3.get(1), ak3.get(4)), ("PO1", "8"))
        self.assertEqual((ak4.get(1), ak4.get(3), ak4.get(4)), ("1", "7", "1"))
        self.assertEqual(message.find("AK5").get(1), "R")
        self.assertTrue(validate.validate_message(message, "X12").clean)

    def test_the_order_it_restates_keeps_its_lines(self):
        self.send(x12_order("DUP-KEEP", lines=TWO_LINES))
        self.send(x12_order("DUP-KEEP", lines=TWO_LINES, control="000000078")
                  .replace("PO1*2*", "PO1*1*"))
        lines = self.order("DUP-KEEP")["lines"]
        self.assertEqual([line["line"] for line in lines], ["1", "2"])

    def test_an_empty_line_number_that_lands_on_a_used_one_is_caught(self):
        # An empty PO101 is read as the line's position, so a first line
        # numbered 2 and an unnumbered second line are both line 2.
        text = x12_order("DUP-EMPTY", lines=TWO_LINES)
        text = text.replace("PO1*1*", "PO1*2*", 1).replace("PO1*2*5*", "PO1**5*")
        summary = self.send(text)
        self.assertEqual(summary["orders"], [])

    def test_an_orders_that_repeats_a_lin_is_rejected_by_its_contrl(self):
        text = edifact_order("DUP-EDI", lines=TWO_LINES).replace("LIN+2+", "LIN+1+")
        summary = self.send(text, headers=EDIFACT)
        self.assertEqual(summary["orders"], [])
        message = self.document(EURODIS, "acknowledgment").groups[0].messages[0]
        self.assertEqual(message.find("UCM").get(3), "4")
        self.assertEqual(message.find("UCS").get(2), "12")


class AFailureHalfwayThrough(MockServerCase):
    """Anything that raises inside `receive` leaves the database untouched.

    The failure is a real one, from SQLite: a trigger that aborts the insert
    of one particular line, which is well after the interchange, its
    transaction sets and the old lines' deletion have all been written.
    """

    def setUp(self):
        super().setUp()
        self.conn = self.httpd.mock.conn
        with self.httpd.mock.lock:
            self.conn.execute(
                "CREATE TRIGGER boom BEFORE INSERT ON order_line"
                " WHEN NEW.sku = 'BRKT-050'"
                " BEGIN SELECT RAISE(ABORT, 'boom'); END")
            self.conn.commit()

    def tearDown(self):
        with self.httpd.mock.lock:
            self.conn.execute("DROP TRIGGER IF EXISTS boom")
            self.conn.commit()
        super().tearDown()

    def snapshot(self):
        with self.httpd.mock.lock:
            # The request log is meant to grow; everything else is not.
            return [line for line in self.conn.iterdump()
                    if "request_log" not in line]

    def test_nothing_is_left_behind(self):
        self.send(x12_order("HALF", lines=TWO_LINES[:1]))
        before = self.snapshot()

        said = io.StringIO()
        with contextlib.redirect_stderr(said):
            status, _headers, data = self.post(
                "/edi", x12_order("HALF", lines=TWO_LINES, control="000000078"),
                headers={"Content-Type": "application/edi-x12"})
        self.assertEqual(status, 500, data)
        self.assertIn("boom", data["error"])
        self.assertEqual(data["type"], "IntegrityError")
        # A 500 is a bug, and its traceback is the only way to find it (#31).
        self.assertIn("Traceback (most recent call last)", said.getvalue())
        self.assertIn("sqlite3.IntegrityError: boom", said.getvalue())

        self.assertEqual(self.snapshot(), before)
        self.assertEqual([line["line"] for line in self.order("HALF")["lines"]],
                         ["1"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
