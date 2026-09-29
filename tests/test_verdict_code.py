"""The overall verdict an 855, 865 or ORDRSP announces, against what it carries.

`transactions.acknowledgment_type` picks `BAK02`/`BCA02` (X12 element 587) and
`BGM` 4343 (EDIFACT) from what happened to the lines. Several of 587's codes
also say whether the document details its lines - `RD` *Reject - With Detail*,
`RJ` *Rejected - No Detail* - and a receiving translator may branch on that
before it looks at the loops. So the code has to agree with what was written,
and it did not: every refusal the mock sent said `RJ` over a line loop (#181).

The check reads the claim from the code's own name in the dictionary rather
than from a list kept here, so a code added later is held to what it says.
EDIFACT's 4343 is checked against its own list, not assumed to mirror X12's.
"""
import datetime
import itertools
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import schema, transactions
from mockedi.transactions import Change, Party

WHEN = datetime.datetime(2026, 10, 2, 9, 30)
US = Party(role="SE", name="Mock EDI Supply Co", identifier="MOCKEDI")
PARTNER = {"id": "ACME", "name": "Acme Distribution Inc"}
ORDER = {"po_number": "PO-181", "ordered_on": "2026-10-01",
         "currency": "USD", "seller_order": "5100181"}
VERDICTS = (transactions.ACCEPTED, transactions.REJECTED,
            transactions.SHORT, transactions.BACKORDERED)
# The segment that opens each line's detail, per document.
LINE_OPENERS = {"855": "PO1", "865": "POC", "ORDRSP": "LIN"}


def row(number, status):
    confirmed = "0" if status == transactions.REJECTED else "4"
    return {"line": str(number), "sku": "W-%d" % number, "quantity": "10",
            "uom": "EA", "price": "1.00", "confirmed": confirmed,
            "status": status, "scheduled_on": "2026-10-09",
            "reason": "discontinued" if status == transactions.REJECTED else ""}


def cases():
    """Every mix of up to three line verdicts, and no lines at all."""
    for size in range(4):
        for mix in itertools.combinations_with_replacement(VERDICTS, size):
            yield [row(n, status) for n, status in enumerate(mix, 1)]


def documents(lines):
    """Each document the verdict is written into, as (kind, code, body)."""
    change = Change(po_number=ORDER["po_number"], sequence="1")
    for kind, body in (
            ("855", transactions.write_response(
                "X12", US, PARTNER, ORDER, lines, WHEN)),
            ("865", transactions.write_change_response(
                "X12", US, PARTNER, ORDER, lines, change, WHEN)),
            ("ORDRSP", transactions.write_response(
                "EDIFACT", US, PARTNER, ORDER, lines, WHEN)),
            ("ORDRSP", transactions.write_change_response(
                "EDIFACT", US, PARTNER, ORDER, lines, change, WHEN))):
        head = body[0]
        code = head.get(4) if kind == "ORDRSP" else head.get(2)
        yield kind, code, body


class TheVerdictCodeAgreesWithTheDetail(unittest.TestCase):

    def each(self):
        """Every document written, as (statuses, kind, code, detailed)."""
        return [([line["status"] for line in lines], kind, code,
                 any(item.tag == LINE_OPENERS[kind] for item in body))
                for lines in cases() for kind, code, body in documents(lines)]

    def name(self, kind, code):
        codes = (schema.RESPONSE_TYPE_CODES if kind == "ORDRSP"
                 else schema.ACK_TYPE_CODES)
        self.assertIn(code, codes)
        return codes[code].lower()

    def test_the_code_is_one_its_own_list_has(self):
        for statuses, kind, code, _ in self.each():
            with self.subTest(kind=kind, statuses=statuses, code=code):
                self.name(kind, code)

    def test_a_code_saying_no_detail_is_never_written_over_a_line_loop(self):
        for statuses, kind, code, detailed in self.each():
            with self.subTest(kind=kind, statuses=statuses, code=code):
                if "no detail" in self.name(kind, code):
                    self.assertFalse(detailed)

    def test_a_code_saying_with_detail_always_has_some(self):
        for statuses, kind, code, detailed in self.each():
            with self.subTest(kind=kind, statuses=statuses, code=code):
                if "with detail" in self.name(kind, code):
                    self.assertTrue(detailed)

    def test_nothing_claims_to_detail_only_the_exceptions(self):
        # `AE`/`RF`, and 4343's `AI` "only changes": the mock details every
        # line, so a code that says only some are detailed is also untrue.
        for statuses, kind, code, _ in self.each():
            with self.subTest(kind=kind, statuses=statuses, code=code):
                name = self.name(kind, code)
                self.assertNotIn("exception detail only", name)
                self.assertNotIn("only changes", name)

    def test_a_rejection_is_announced_only_when_every_line_is_rejected(self):
        for statuses, kind, code, _ in self.each():
            with self.subTest(kind=kind, statuses=statuses, code=code):
                refused = set(statuses) == {transactions.REJECTED}
                self.assertEqual(self.name(kind, code).startswith("reject"),
                                 refused)

    def test_the_refusal_says_reject_with_detail(self):
        lines = [row(1, transactions.REJECTED), row(2, transactions.REJECTED)]
        written = {(kind, code) for kind, code, _ in documents(lines)}
        self.assertEqual(written, {("855", "RD"), ("865", "RD"),
                                   ("ORDRSP", "RE")})


class TheResponseTypeListIsD96As(unittest.TestCase):
    """4343's names, as D.96A gives them, for the codes the dictionary holds.

    `AI` was named "Acknowledge - with detail, no change", which is `AD`'s
    name; D.96A's `AI` acknowledges only the changes. A partner's ORDRSP
    carrying `AD` was reported as an invalid code.
    """

    def test_ad_is_with_detail_and_no_change(self):
        self.assertEqual(schema.RESPONSE_TYPE_CODES["AD"],
                         "Acknowledge - with detail, no change")

    def test_ai_acknowledges_only_the_changes(self):
        self.assertEqual(schema.RESPONSE_TYPE_CODES["AI"],
                         "Acknowledge only changes")


if __name__ == "__main__":
    unittest.main()
