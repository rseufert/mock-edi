"""What an ORDCHG identifies itself by (#186).

`BGM` position 2 was declared as the composite `C106` — a number, a version
in 1056 and a revision in 1060 — and the mock wrote the order number in the
first component and the change's sequence in the third. **D.96A has no `C106`
in `BGM` at all**: position 2 is element 1004, the document's own number, and
the order being amended is named by `RFF+ON`, which this message has always
written on the next line.

So the dictionary and the writer were telling the same untrue story, which is
why #289 could correct the `RFF` half of #186 and not this one: declaring
position 2 simple while the writer still packed three components into it
would have left #288 refusing the mock's own ORDCHG.

What goes in 1004 follows from D.96A's own note for the segment, in two
published copies:

> "A segment by which the sender must uniquely identify the order change by
> means of its number and when necessary its function."

**Uniquely.** A bare sequence does not — every order's first change would be
`1` — so the number is the order's and the sequence together, `PO4711-2`, and
`RFF+ON` still names the order in its own right. Sources: edifactory.de's
D.96A ORDCHG and xedi's, which agree; the latter gives 1004 as "reference
number assigned to the change request by the issuer".

The reader takes **both** forms, as the decision on #186 requires: a buyer
sending the shape this mock wrote before 0.8.0 is still understood, and so is
one sending D.96A's.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import schema, transactions
from mockedi.envelope import Delimiters, render_segment, seg

from support import MockServerCase
from test_order_writers import a_change, change_message

DELIMS = Delimiters(segment="'", element="+", component=":", release="?",
                    repetition="", decimal=".")


def header(message, tag):
    for item in message.body:
        if item.tag == tag:
            return item
    return None


def written(message, tag):
    item = header(message, tag)
    return render_segment(item, DELIMS) if item is not None else ""


class WhatTheDictionaryDeclares(unittest.TestCase):

    def test_position_two_is_element_1004_on_its_own(self):
        position = schema.BGM.elements[1]
        self.assertEqual(position.ref, "1004")
        self.assertFalse(position.composite,
                         "C106 is a later directory's, not D.96A's")
        self.assertEqual((position.min_len, position.max_len), (1, 35))

    def test_and_bgm_is_four_positions_of_which_one_is_composite(self):
        self.assertEqual([e.ref for e in schema.BGM.elements],
                         ["C002", "1004", "1225", "4343"])
        self.assertEqual([e.ref for e in schema.BGM.elements if e.composite],
                         ["C002"])


class WhatTheMockWrites(unittest.TestCase):

    def test_the_number_is_the_order_and_the_sequence(self):
        message = change_message("EDIFACT", a_change())
        self.assertEqual(written(message, "BGM"), "BGM+230+4500001001-2+4")

    def test_and_the_order_is_named_in_its_own_right(self):
        message = change_message("EDIFACT", a_change())
        self.assertEqual(written(message, "RFF"), "RFF+ON:4500001001")

    def test_a_cancellation_differs_only_in_the_function_code(self):
        # The same number; 1225 is what says it withdraws the order.
        message = change_message("EDIFACT", a_change(purpose="01"))
        self.assertEqual(written(message, "BGM"), "BGM+230+4500001001-2+1")

    def test_no_component_separator_in_position_two(self):
        # The thing #288 will refuse once it lands, and the reason this
        # change comes first.
        message = change_message("EDIFACT", a_change())
        self.assertNotIsInstance(header(message, "BGM").raw(2), list)

    def test_a_change_with_no_sequence_is_numbered_one(self):
        change = a_change()
        change.sequence = ""
        self.assertEqual(written(change_message("EDIFACT", change), "BGM"),
                         "BGM+230+4500001001-1+4")


class WhatTheMockReads(unittest.TestCase):
    """Both forms, which is what the decision on #186 asks for."""

    def read(self, bgm, rff=True):
        body = [bgm, seg("DTM", ["137", "20261005", "102"])]
        if rff:
            body.append(seg("RFF", ["ON", "4500001001"]))
        body.append(seg("LIN", "1", "3", ["WIDGET-001", "VP"]))
        body.append(seg("QTY", ["21", "60"]))
        from test_order_writers import wrap
        message = wrap("EDIFACT", "ORDCHG", body, "9").groups[0].messages[0]
        return transactions.read_change(message, "EDIFACT")

    def test_d96as_form(self):
        change = self.read(seg("BGM", ["230"], "4500001001-2", "4"))
        self.assertEqual((change.po_number, change.sequence, change.purpose),
                         ("4500001001", "2", "04"))

    def test_the_form_this_mock_wrote_before(self):
        change = self.read(seg("BGM", ["230"],
                               ["4500001001", "", "2"], "4"))
        self.assertEqual((change.po_number, change.sequence, change.purpose),
                         ("4500001001", "2", "04"))

    def test_a_partners_own_numbering_is_taken_whole(self):
        # Not of the mock's shape, so there is no prefix to strip: the order
        # comes from RFF+ON and the number is the number.
        change = self.read(seg("BGM", ["230"], "CHG-00417", "4"))
        self.assertEqual((change.po_number, change.sequence),
                         ("4500001001", "CHG-00417"))

    def test_with_no_rff_the_number_is_still_read(self):
        # A buyer that omits RFF+ON. The order cannot be known from the
        # number alone, so the number stands for both rather than being
        # silently split.
        change = self.read(seg("BGM", ["230"], "4500001001-2", "4"), rff=False)
        self.assertEqual(change.po_number, "4500001001-2")

    def test_a_cancellation_either_way(self):
        for bgm in (seg("BGM", ["230"], "4500001001-1", "1"),
                    seg("BGM", ["230"], ["4500001001", "", "1"], "1")):
            with self.subTest():
                self.assertEqual(self.read(bgm).purpose, "01")


class AnOrderNumberTooLongToChange(MockServerCase):
    """1004 is `an..35`, and the number is the order's plus the sequence.

    So an order number of 34 or 35 characters is legal in its own right and
    leaves no room for a sequence, and the mock would write an ORDCHG its own
    dictionary reports - #291's fault, one element over. Refused at the door
    instead, the way `/_mock/purchase` refuses what the dictionary would
    reject (#237). The senior found this second-reading the change that
    introduced it.

    The window is narrow and real: 33 characters still fits, 34 does not, and
    an order of 36 or more is already refused when it is *placed*, by the
    guard that checks a placed order's number against `BGM02`.
    """

    SUPPLIER = "EUROSUP"

    def setUp(self):
        super().setUp()
        status, _headers, body = self.post("/_mock/partners", {
            "id": self.SUPPLIER, "name": "Eurosup", "dialect": "EDIFACT",
            "version": "D:96A:UN", "role": "supplier"})
        self.assertEqual(status, 201, body)

    def place(self, po_number, partner=None):
        return self.post("/_mock/purchase", {
            "partner": partner or self.SUPPLIER, "po_number": po_number,
            "lines": [{"sku": "WIDGET-001", "quantity": "10",
                       "price": "12.50"}]})

    def change(self, po_number, partner=None):
        return self.post(
            "/_mock/purchase/%s/change?partner=%s"
            % (po_number, partner or self.SUPPLIER),
            {"lines": [{"line": "1", "quantity": "5", "price": "12.50"}]})

    def test_thirty_three_characters_still_fits(self):
        # 33 + "-" + "1" is exactly 35.
        po = "P" * 33
        self.assertEqual(self.place(po)[0], 201)
        status, _headers, body = self.change(po)
        self.assertEqual(status, 200, body)

    def test_thirty_four_is_refused_and_the_refusal_does_the_arithmetic(self):
        po = "P" * 34
        self.assertEqual(self.place(po)[0], 201)
        status, _headers, body = self.change(po)
        self.assertEqual(status, 400, body)
        said = " ".join(body["problems"])
        self.assertIn("36 characters", said)
        self.assertIn("allows 35", said)
        self.assertIn("shorter number", said)

    def test_the_order_itself_is_not_refused(self):
        # Only the change is. 34 characters is legal in BGM's 1004 when the
        # number stands alone, and in RFF's 1154, so refusing the order would
        # be wrong.
        self.assertEqual(self.place("P" * 34)[0], 201)

    def test_an_order_too_long_for_bgm02_is_refused_when_placed(self):
        # The guard that was already there, reading the corrected BGM02.
        status, _headers, body = self.place("P" * 40)
        self.assertEqual(status, 400, body)
        self.assertIn("BGM02 is 40 characters", " ".join(body["problems"]))

    def test_an_x12_supplier_is_not_refused(self):
        # The 860 carries the sequence in BCH05 on its own and the order
        # number in BCH03, so nothing is packed together. NORTHWIND is the
        # seeded X12 supplier.
        po = "N" * 22
        self.assertEqual(self.place(po, partner="NORTHWIND")[0], 201)
        status, _headers, body = self.change(po, partner="NORTHWIND")
        self.assertEqual(status, 200, body)


class TogetherWith288(unittest.TestCase):
    """What #288's finding and this change do when both are in.

    #288 reports a simple element that arrives with components. This change
    makes `BGM` position 2 simple. So the two meet, and the order they landed
    in does not matter - but the combination produces something neither does
    alone, and it is worth a test rather than an argument.

    The mock's own ORDCHG is **clean**, which is the whole reason this change
    had to come before #288 could be turned on. And a buyer still sending the
    composite form is now **told** - and still **understood**, because the
    finding is an error rather than a fatal one, so the change is read and
    applied. Reported but not refused is the outcome Zack's decision on #186
    asked for, arrived at from both ends.
    """

    def report(self, bgm):
        from mockedi import validate
        from test_order_writers import wrap
        body = [bgm, seg("DTM", ["137", "20261005", "102"]),
                seg("RFF", ["ON", "4500001001"]),
                seg("LIN", "1", "3", ["WIDGET-001", "VP"]),
                seg("QTY", ["21", "60"])]
        interchange = wrap("EDIFACT", "ORDCHG", body, "9")
        found = validate.validate(interchange)
        notes = [element.note for message in found.messages
                 for finding in message.segments
                 for element in finding.elements]
        _group, message = list(interchange.messages())[0]
        # The BGM02 finding only, not the whole verdict: a hand-built body is
        # not a complete ORDCHG and draws structural findings of its own,
        # which are not what these tests are about.
        about_bgm = [note for note in notes if "BGM02" in note]
        return about_bgm, transactions.read_change(message, "EDIFACT")

    def test_the_mocks_own_ordchg_draws_nothing_at_all(self):
        # This one *is* a complete document, so the whole verdict is fair.
        from mockedi import validate
        from test_order_writers import wrap
        message = change_message("EDIFACT", a_change())
        found = validate.validate(wrap("EDIFACT", "ORDCHG", message.body, "9"))
        self.assertTrue(found.clean)

    def test_d96as_form_draws_nothing_about_bgm02(self):
        about_bgm, _change = self.report(
            seg("BGM", ["230"], "4500001001-2", "4"))
        self.assertEqual(about_bgm, [])

    def test_the_old_composite_form_is_now_reported(self):
        about_bgm, _change = self.report(
            seg("BGM", ["230"], ["4500001001", "", "2"], "4"))
        self.assertTrue(any("is a simple element" in note
                            for note in about_bgm), about_bgm)

    def test_and_is_still_read_and_applied(self):
        # The finding is an error, not a fatal one, so the change is read.
        # "The reader accepts both forms" stays true with #288 in.
        _about_bgm, change = self.report(
            seg("BGM", ["230"], ["4500001001", "", "2"], "4"))
        self.assertEqual((change.po_number, change.sequence),
                         ("4500001001", "2"))


class TheNumberSurvivesARoundTrip(unittest.TestCase):
    """Written, read and written again has to give the same bytes.

    The inverse matters: without it a change request read and written again
    would carry `4500001001-4500001001-2`, which is the trap a prefix
    convention sets.
    """

    def test_written_read_and_written_again(self):
        first = change_message("EDIFACT", a_change())
        back = transactions.read_change(first, "EDIFACT")
        again = change_message("EDIFACT", back)
        self.assertEqual(written(again, "BGM"), written(first, "BGM"))
        self.assertEqual(back.sequence, "2")

    def test_the_two_helpers_are_inverses(self):
        for po, sequence in (("4500001001", "2"), ("PO-2026-1", "11"),
                             ("A", "1"), ("ORDER-1-2", "3")):
            with self.subTest(po=po, sequence=sequence):
                number = transactions.change_number(po, sequence)
                self.assertEqual(
                    transactions.change_sequence(number, po), sequence)


if __name__ == "__main__":
    unittest.main()
