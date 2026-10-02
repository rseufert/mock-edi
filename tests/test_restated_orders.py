"""An order that says it cancels, changes or replaces one is never a new order (#198).

A buyer may restate an order rather than send an 860 or an ORDCHG, and says so
in BEG01 or BGM 1225. Against an order the mock holds that is a change. Against
one it does not hold it is refused beside the acknowledgment, as an 860 for an
unknown order is - and before this an X12 one was fulfilled as a new order, and
an EDIFACT one always was, because 1225's `1` was compared with X12's `01`.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import documents
from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order

WINDOW = {"despatch_delay_ms": 60000, "invoice_delay_ms": 60000}
MORE = (("WIDGET-001", 70, "12.50"), ("BRKT-050", 40, "4.15"))


class _Restated(MockServerCase):
    """The cases, for a dialect the subclass names."""

    config_kwargs = WINDOW
    partner = ""
    headers = None
    cancels = ()        # the purposes that withdraw the order
    changes = ()        # and those that restate it
    acknowledgment = ""
    response = ""

    def place(self, po_number, purpose=None, lines=None):
        raise NotImplementedError

    def after(self, po_number):
        """Every code sent about the order once the clock has run out."""
        self.post("/_mock/advance?all")
        _s, _h, rows = self.get("/_mock/documents?direction=out&reference="
                                + po_number)
        return sorted(row["code"] for row in rows)

    def test_one_for_an_order_never_seen_is_refused_and_not_fulfilled(self):
        for purpose in self.cancels + self.changes:
            po_number = "PO-NEVER-%s" % purpose
            with self.subTest(purpose=purpose):
                summary = self.place(po_number, purpose)
                self.assertEqual(summary["orders"], [])
                self.assertEqual(summary["changed"], [])
                self.assertEqual(summary["refusals"], [
                    {"order": po_number, "reason": documents.NOT_FOUND}])
                # The syntax was fine, and the acknowledgment says so; the
                # refusal is beside it.
                self.assertEqual([q["code"] for q in summary["queued"]],
                                 [self.acknowledgment])
                status, _h, _data = self.get("/_mock/orders/" + po_number)
                self.assertEqual(status, 404)
                self.assertEqual(self.after(po_number), [])

    def test_a_cancellation_of_a_held_order_cancels_it(self):
        for purpose in self.cancels:
            po_number = "PO-HELD-%s" % purpose
            with self.subTest(purpose=purpose):
                self.place(po_number)
                summary = self.place(po_number, purpose)
                self.assertEqual(summary["orders"], [])
                self.assertEqual(summary["changed"], [po_number])
                self.assertEqual([q["code"] for q in summary["queued"]],
                                 [self.acknowledgment, self.response])
                self.assertEqual(self.order(po_number)["status"], "cancelled")
                # The first answer and the answer to the cancellation, and
                # nothing packed or billed.
                self.assertEqual(self.after(po_number), sorted(
                    self.responses_to_an_order_and_its_change))

    def test_a_change_or_replacement_of_a_held_order_changes_it(self):
        for purpose in self.changes:
            po_number = "PO-HELD-%s" % purpose
            with self.subTest(purpose=purpose):
                self.place(po_number)
                summary = self.place(po_number, purpose, MORE)
                self.assertEqual(summary["orders"], [])
                self.assertEqual(summary["changed"], [po_number])
                self.assertEqual(summary["refusals"], [])
                self.assertEqual([q["code"] for q in summary["queued"]],
                                 [self.acknowledgment, self.response])
                self.assertEqual(
                    self.order(po_number)["lines"][0]["quantity"], "70")

    def test_an_original_is_still_an_order(self):
        summary = self.place("PO-ORIGINAL")
        self.assertEqual(summary["orders"], ["PO-ORIGINAL"])
        self.assertEqual(summary["refusals"], [])


class AnX12OrderThatRestatesOne(_Restated):
    partner = ACME
    cancels = ("01", "03")
    changes = ("04", "05")
    acknowledgment = "997"
    response = "865"
    responses_to_an_order_and_its_change = ("855", "865")

    def place(self, po_number, purpose="00", lines=None):
        kwargs = {"lines": lines} if lines else {}
        return self.send(x12_order(po_number, purpose=purpose, **kwargs))


class AnEdifactOrderThatRestatesOne(_Restated):
    partner = EURODIS
    cancels = ("1", "3")
    changes = ("4", "5")
    acknowledgment = "CONTRL"
    response = "ORDRSP"
    responses_to_an_order_and_its_change = ("ORDRSP", "ORDRSP")

    def place(self, po_number, purpose="9", lines=None):
        kwargs = {"lines": lines} if lines else {}
        return self.send(edifact_order(po_number, purpose=purpose, **kwargs),
                         headers={"Content-Type": "application/edifact"})


del _Restated

if __name__ == "__main__":
    unittest.main()
