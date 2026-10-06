"""A composite that says how wide the standard makes it (#186).

`RFF`'s `C506` was declared with a fifth component, 1060. D.96A's has four.
The dictionary declares `D:96A:UN`, serves that at `GET /_mock/dictionary` and
validates against it, so that was a quiet untruth in the thing a partner
builds a guide from.

Correcting the declaration refuses nothing on its own: a component past a
composite's definition was never reported, and deliberately. Most composites
here are short on purpose - D.96A's `C058` holds five `3124`s where one
carries almost every real address - so reporting past the end would be the
false positive #54 removed at the segment level.

So `Element.full_width`, the component-level twin of `Segment.full_width`. A
composite that declares a width is claiming to be *complete*, and there a
component past the end is code 3. `C506` is the only one that declares one.

`BGM`'s other half of #186 - position 2 being element 1004 rather than the
composite `C106` - is **not here**. The mock writes an ORDCHG's change
sequence in `C106`'s 1060 (`transactions.py`) and reads it back from there, so
declaring position 2 simple would leave the dictionary and the writer
disagreeing, and #288 would then refuse the mock's own ORDCHG. Where that
sequence lives in D.96A changes bytes, so it waits for a decision on #186.

Sources: the D.96A `RFF` page at edifactory.de, corroborated by xedi's D.96A
message pages. UNECE's own service answers 403.

Five of the seven tests here fail with `mockedi/` at the commit before this
one - two failures and three errors, the errors because `Element.full_width`
does not exist there. The other two are pins and say so: an ordinary `RFF`,
and an empty fifth component, which passed before because *nothing* past the
end was reported.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import schema
from mockedi.envelope import seg

from test_validate import check, notes

HEADER = [seg("DTM", ["137", "20260924", "102"]),
          seg("NAD", "BY", ["ACME", "", "91"]),
          seg("NAD", "SU", ["EURODIS", "", "91"])]
LINE = [seg("LIN", "1", "", ["WIDGET-001", "VP"])]
TAIL = [seg("UNS", "S"), seg("CNT", ["2", "1"])]


ORDINARY = seg("BGM", ["220"], ["PO4711"], "9")


def orders(*extra):
    body = [ORDINARY] + list(extra) + HEADER + LINE + TAIL
    return notes(check(body, code="ORDERS", dialect="EDIFACT"))



class WhatD96ASaysC506Is(unittest.TestCase):
    """Four components, and the composite says so, so a fifth is reported."""

    def test_the_dictionary_declares_four_and_claims_to_be_complete(self):
        c506 = schema.RFF.elements[0]
        self.assertEqual(tuple(c.ref for c in c506.components),
                         ("1153", "1154", "1156", "4000"))
        self.assertEqual(c506.full_width, 4)

    def test_an_ordinary_reference_is_unaffected(self):
        self.assertEqual(orders(seg("RFF", ["ON", "PO4711"])), [])
        self.assertEqual(orders(seg("RFF", ["ON", "PO4711", "1"])), [])

    def test_a_fifth_component_is_a_reference_d96a_cannot_carry(self):
        found = orders(seg("RFF", ["ON", "PO4711", "", "", "7"]))
        self.assertIn("RFF01 (C506) has no component at position 5", found)

    def test_an_empty_fifth_component_is_not_reported(self):
        # Trailing separators are how a sender writes "nothing here". A
        # document ending `RFF+ON:PO4711::::'` is not making a claim about
        # 1060 and is not told that it is.
        self.assertEqual(orders(seg("RFF", ["ON", "PO4711", "", "", ""])),
                         [])


class WhichCompositesClaimToBeComplete(unittest.TestCase):
    """Only the one. Every other composite stays unreported past its end."""

    def test_c506_is_the_only_one(self):
        declaring = []
        for name, segment in sorted(vars(schema).items()):
            if not isinstance(segment, schema.Segment):
                continue
            for position, element in enumerate(segment.elements, start=1):
                if element.composite and element.full_width:
                    declaring.append((segment.tag, position, element.ref))
        self.assertEqual({ref for _tag, _pos, ref in declaring}, {"C506"},
                         declaring)

    def test_a_short_composite_still_says_nothing(self):
        # D.96A's C058 holds five 3124s and the dictionary declares one, on
        # purpose. A NAD carrying three address lines is a correct document
        # and is not told otherwise - the #54 rule, one level down.
        c058 = schema.NAD.elements[2]
        self.assertEqual(len(c058.components), 1)
        self.assertEqual(c058.full_width, 0)
        body = [ORDINARY] + [
            seg("DTM", ["137", "20260924", "102"]),
            seg("NAD", "BY", ["ACME", "", "91"],
                ["Floor 3", "Building B", "Gate 7"]),
            seg("NAD", "SU", ["EURODIS", "", "91"]),
        ] + LINE + TAIL
        self.assertEqual(notes(check(body, code="ORDERS", dialect="EDIFACT")),
                         [])

    def test_the_width_of_a_composite_that_declares_none_is_its_length(self):
        for name, segment in sorted(vars(schema).items()):
            if not isinstance(segment, schema.Segment):
                continue
            for element in segment.elements:
                if element.composite and not element.full_width:
                    with self.subTest("%s/%s" % (segment.tag, element.ref)):
                        self.assertEqual(element.component_width,
                                         len(element.components))


if __name__ == "__main__":
    unittest.main()
