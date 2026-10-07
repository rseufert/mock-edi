"""A REMADV judged early, and one REMADV replacing another (#187).

Two of the remittance findings were judged for an 820 only: an advice whose
payment takes effect in the future (`BPR16`), and a debit against a trace no
advice ever credited (`BPR03` D). A REMADV reached neither, because D.96A
settles neither question — which date is the value date, and how one advice
corrects another.

**EANCOM's REMADV guide settles both**, and differently from X12:

- the settlement date is the header `DTM+138`, D.96A's "Payment date". The
  guide admits 137, 138, 203, 227 and 263 at the head and not 209, and says
  each advice relates to one settlement date;
- there is **no debit and no negative amount**. A correction is a *new*
  advice with `BGM` 1225 code 5, Replace, naming the one it replaces in a
  header `RFF+RA`. So the X12 counterpart of `reversal-of-nothing` is
  `replacement-of-nothing`: a replacement of an advice never received.

The dictionary gains `138` at `2005` and `RA` at `1153`. Those lists are per
element and shared by every set, which is what D.96A makes them; a guide
narrowing them to a subset is a guide's business and not this dictionary's.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, remittance, schema, validate
from mockedi.envelope import seg

from support import EURODIS, MockServerCase, _next_control

EDIFACT_TYPE = {"Content-Type": "application/edifact"}


def day(offset=0):
    """The mock's date in the host's zone, moved by `offset` days."""
    import datetime
    return (datetime.datetime.now().date()
            + datetime.timedelta(days=offset)).strftime("%Y%m%d")


def advice(number="RA-1", settles=None, purpose="9", replaces="",
           extra=(), total="100.00"):
    body = [seg("BGM", ["481"], [number], purpose),
            seg("DTM", ["137", "20260928", "102"])]
    if settles:
        body.append(seg("DTM", ["138", settles, "102"]))
    if replaces:
        body.append(seg("RFF", ["RA", replaces]))
    body += [seg("NAD", "PR", ["EURODIS", "", "92"]),
             seg("NAD", "PE", ["MOCKEDI", "", "92"]),
             seg("CUX", ["2", "EUR", "11"])]
    body += list(extra)
    body += [seg("DOC", ["380"], ["INV8000001"]), seg("MOA", ["12", total]),
             seg("UNS", "S"), seg("MOA", ["12", total])]
    return edifact.render(edifact.wrap(
        [edifact.message("REMADV", "1", body, version="D:96A:UN")],
        EURODIS, "MOCKEDI", _next_control(4)), newline=True)


def read(**kwargs):
    _group, message = list(edifact.parse(advice(**kwargs)).messages())[0]
    return remittance.read(message, "EDIFACT")


class TheDictionaryKnowsBoth(unittest.TestCase):

    def test_138_is_a_date_qualifier_named_as_the_directory_names_it(self):
        self.assertEqual(schema.EDIFACT_DATE_QUALIFIERS["138"], "Payment date")

    def test_ra_is_a_reference_qualifier(self):
        self.assertEqual(schema.EDIFACT_REFERENCE_QUALIFIERS["RA"],
                         "Remittance advice number")

    def test_an_advice_carrying_both_is_clean(self):
        # The point of declaring them: before this, DTM+138 and RFF+RA each
        # drew a code-7 finding against a correct document.
        _group, message = list(edifact.parse(advice(
            settles="20261015", purpose="5", replaces="RA-0")).messages())[0]
        report = validate.validate_message(message, "EDIFACT")
        self.assertEqual(
            [(segment.tag, element.code, element.note)
             for segment in report.segments if segment.tag in ("DTM", "RFF")
             for element in segment.elements], [])


class WhatIsRead(unittest.TestCase):

    def test_the_settlement_date_comes_from_the_header_dtm_138(self):
        import datetime
        self.assertEqual(read(settles="20261015").settles,
                         datetime.date(2026, 10, 15))

    def test_an_advice_with_no_dtm_138_settles_nothing(self):
        self.assertIsNone(read().settles)

    def test_the_purpose_and_the_advice_it_replaces_are_read(self):
        got = read(purpose="5", replaces="RA-0")
        self.assertEqual((got.purpose, got.replaces), ("5", "RA-0"))

    def test_an_ordinary_advice_replaces_nothing(self):
        got = read()
        self.assertEqual((got.purpose, got.replaces), ("9", ""))

    def test_only_the_header_counts(self):
        # A DTM+138 inside a DOC group dates that document and one after UNS
        # is nobody's; an RFF+RA inside a group is that document's reference.
        # Counting either as the advice's is the fault #315 had with CUX.
        got = read(settles="20261015", purpose="5", replaces="RA-0",
                   extra=[seg("DOC", ["380"], ["INV8000002"]),
                          seg("MOA", ["12", "1.00"]),
                          seg("DTM", ["138", "20991231", "102"]),
                          seg("RFF", ["RA", "NOT-THE-HEADERS"])])
        import datetime
        self.assertEqual(got.settles, datetime.date(2026, 10, 15))
        self.assertEqual(got.replaces, "RA-0")


class AnAdviceThatHasNotSettledYet(MockServerCase):
    """`remitted-before-settlement`, now for a REMADV as well as an 820."""

    def send(self, **kwargs):
        status, _h, data = self.post("/edi", advice(**kwargs),
                                     headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        return data

    def rules(self, data):
        return [d["rule"] for d in data["transactionSets"][0]["disagreements"]]

    def test_one_that_takes_effect_later_is_found(self):
        data = self.send(number="RA-LATER", settles=day(3))
        self.assertTrue(data["accepted"], data)
        self.assertIn(remittance.BEFORE_SETTLEMENT, self.rules(data))

    def test_the_note_names_the_segment_this_document_has(self):
        data = self.send(number="RA-NOTE", settles=day(3))
        found = [d for d in data["transactionSets"][0]["disagreements"]
                 if d["rule"] == remittance.BEFORE_SETTLEMENT][0]
        self.assertIn("DTM+138", found["note"])
        self.assertNotIn("BPR16", found["note"])
        self.assertIn("cash that has not arrived", found["note"])

    def test_one_that_has_taken_effect_is_not(self):
        for offset in (0, -1):
            with self.subTest(offset=offset):
                data = self.send(number="RA-ON-%d" % offset,
                                 settles=day(offset))
                self.assertNotIn(remittance.BEFORE_SETTLEMENT,
                                 self.rules(data))

    def test_one_with_no_settlement_date_is_neither(self):
        data = self.send(number="RA-NODATE")
        self.assertNotIn(remittance.BEFORE_SETTLEMENT, self.rules(data))

    def test_the_mocks_clock_decides_it(self):
        self.post("/_mock/advance?seconds=%d" % (4 * 86400))
        data = self.send(number="RA-ADVANCED", settles=day(3))
        self.assertNotIn(remittance.BEFORE_SETTLEMENT, self.rules(data))

    def test_the_listing_carries_the_date_and_agrees_with_the_finding(self):
        self.send(number="RA-L1", settles=day(3))
        self.send(number="RA-L2", settles=day(-1))
        _s, _h, rows = self.get("/_mock/remittances?partner=" + EURODIS)
        self.assertEqual([(r["settles"], r["settledOnArrival"]) for r in rows],
                         [(day(3)[:4] + "-" + day(3)[4:6] + "-" + day(3)[6:],
                           False),
                          (day(-1)[:4] + "-" + day(-1)[4:6] + "-" + day(-1)[6:],
                           True)])

    def test_an_advice_with_no_date_says_so_in_the_listing(self):
        self.send(number="RA-NONE")
        _s, _h, rows = self.get("/_mock/remittances?partner=" + EURODIS)
        self.assertEqual((rows[0]["settles"], rows[0]["settledOnArrival"]),
                         ("", None))


class OneAdviceReplacingAnother(MockServerCase):

    def send(self, **kwargs):
        status, _h, data = self.post("/edi", advice(**kwargs),
                                     headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        return data

    def rules(self, data):
        return [d["rule"] for d in data["transactionSets"][0]["disagreements"]]

    def listing(self):
        _s, _h, rows = self.get("/_mock/remittances?partner=" + EURODIS)
        return [(r["trace"], r["status"], r["replaces"], r["replacedBy"])
                for r in rows]

    def test_a_replacement_relates_to_the_advice_it_names(self):
        self.send(number="RA-A")
        self.send(number="RA-B", purpose="5", replaces="RA-A")
        rows = self.listing()
        self.assertEqual([(t, s, r) for t, s, r, _b in rows],
                         [("RA-A", "replaced", None),
                          ("RA-B", "advised", "RA-A")])
        self.assertIsNotNone(rows[0][3], "RA-A should name what replaced it")

    def test_a_replacement_of_nothing_is_a_finding(self):
        data = self.send(number="RA-ORPHAN", purpose="5", replaces="RA-NEVER")
        self.assertIn(remittance.REPLACES_NOTHING, self.rules(data))
        found = [d for d in data["transactionSets"][0]["disagreements"]
                 if d["rule"] == remittance.REPLACES_NOTHING][0]
        self.assertEqual(found["expected"], "RA-NEVER")
        self.assertIn("no advice numbered RA-NEVER was received", found["note"])

    def test_an_advice_naming_its_own_number_has_replaced_nothing(self):
        data = self.send(number="RA-SELF", purpose="5", replaces="RA-SELF")
        self.assertIn(remittance.REPLACES_NOTHING, self.rules(data))

    def test_a_replacement_of_an_advice_that_exists_is_no_finding(self):
        self.send(number="RA-OK-1")
        data = self.send(number="RA-OK-2", purpose="5", replaces="RA-OK-1")
        self.assertNotIn(remittance.REPLACES_NOTHING, self.rules(data))

    def test_an_ordinary_advice_naming_an_rff_ra_replaces_nothing(self):
        # 1225 must say 5. An RFF+RA on an original advice is a reference,
        # not a replacement, so it is neither related nor reported.
        self.send(number="RA-P1")
        data = self.send(number="RA-P2", purpose="9", replaces="RA-P1")
        self.assertNotIn(remittance.REPLACES_NOTHING, self.rules(data))
        self.assertEqual([(t, s) for t, s, _r, _b in self.listing()],
                         [("RA-P1", "advised"), ("RA-P2", "advised")])

    def test_a_chain_leaves_each_naming_its_own_predecessor(self):
        self.send(number="RA-C1")
        self.send(number="RA-C2", purpose="5", replaces="RA-C1")
        self.send(number="RA-C3", purpose="5", replaces="RA-C2")
        self.assertEqual([(t, s, r) for t, s, r, _b in self.listing()],
                         [("RA-C1", "replaced", None),
                          ("RA-C2", "replaced", "RA-C1"),
                          ("RA-C3", "advised", "RA-C2")])


if __name__ == "__main__":
    unittest.main()
