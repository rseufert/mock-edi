"""A document's currency on its own `MOA`, as the advice's is (#322).

`C516` carries a currency of its own in component 3. The reader used it at
the summary level and ignored it inside a `DOC` group, so the same fact
stated the same way was read at one level and lost at the other. A document
could only say its currency with a `CUX` in its group (D.96A's SG5, read
since #298) — and per the EANCOM REMADV guide an advice is likelier to put
the invoice's own currency on its `MOA`, which meant
`remittance-currency-not-the-invoice` never fired for exactly the case it
exists to catch: a dollar invoice paid out of a euro account.

The two levels now resolve it the same way, which is the point of the
change rather than a side effect of it:

| | header | document |
| --- | --- | --- |
| what the `CUX` said | `Advice.header_currency` | `Paid.group_currency` |
| what the `MOA` said, and what wins | `Advice.currency` | `Paid.currency` |
| they disagree | `remittance-currency-disagrees` | the same rule |

`MOA+9` is still not read. That is a stated limitation of the mock, in
`EDIFACT_AMOUNT_QUALIFIERS`, and a larger question than this one.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, remittance
from mockedi.envelope import seg

from support import EURODIS, MockServerCase, _next_control

EDIFACT_TYPE = {"Content-Type": "application/edifact"}
EURODIS_INVOICE = "INV9000001"     # EUR, EURODIS's

HEAD = (seg("BGM", ["481"], ["RA-MOA"], "9"),
        seg("DTM", ["137", "20260928", "102"]),
        seg("NAD", "PR", ["EURODIS", "", "92"]),
        seg("NAD", "PE", ["MOCKEDI", "", "92"]))


def payload(*body, total="100.00", summary=None):
    """The whole message. `summary` is the summary `MOA`'s own currency."""
    tail = seg("MOA", ["12", total] + ([summary] if summary else []))
    return edifact.render(edifact.wrap(
        [edifact.message("REMADV", "1",
                         list(HEAD) + list(body) + [seg("UNS", "S"), tail],
                         version="D:96A:UN")],
        EURODIS, "MOCKEDI", _next_control(4)), newline=True)


def read(*body, total="100.00", summary=None):
    _group, message = list(
        edifact.parse(payload(*body, total=total,
                              summary=summary)).messages())[0]
    return remittance.read(message, "EDIFACT")


def document(invoice="INV1", amount="100.00", on_moa=None, on_cux=None):
    """A `DOC` group stating its currency on its `MOA`, its `CUX`, or both."""
    out = [seg("DOC", ["380"], [invoice]),
           seg("MOA", ["12", amount] + ([on_moa] if on_moa else []))]
    if on_cux:
        out.append(seg("CUX", ["2", on_cux, "11"]))
    return out


class ADocumentStatesItsCurrencyOnItsMoa(unittest.TestCase):

    def test_the_moas_own_currency_is_the_documents(self):
        # The case in #322: read as None before.
        advice = read(*document(on_moa="USD"))
        self.assertEqual(advice.invoices[0].currency, "USD")

    def test_the_groups_cux_still_works_on_its_own(self):
        advice = read(*document(on_cux="GBP"))
        self.assertEqual(advice.invoices[0].currency, "GBP")
        self.assertEqual(advice.invoices[0].group_currency, "GBP")

    def test_a_document_that_states_neither_has_none(self):
        advice = read(*document())
        self.assertIsNone(advice.invoices[0].currency)
        self.assertIsNone(advice.invoices[0].group_currency)

    def test_each_document_keeps_its_own(self):
        advice = read(*document("INV1", on_moa="USD"),
                      *document("INV2", on_moa="GBP"),
                      *document("INV3"))
        self.assertEqual([(item.invoice, item.currency)
                          for item in advice.invoices],
                         [("INV1", "USD"), ("INV2", "GBP"), ("INV3", None)])

    def test_the_amount_and_its_currency_come_from_one_segment(self):
        advice = read(*document(amount="42.50", on_moa="USD"))
        self.assertEqual((str(advice.invoices[0].paid),
                          advice.invoices[0].currency), ("42.50", "USD"))

    def test_the_two_levels_do_not_leak_into_each_other(self):
        # The summary MOA says EUR and the document's says USD. Each stays
        # where it was said: reading C516 in two places must not merge them.
        advice = read(*document(on_moa="USD"), summary="EUR")
        self.assertEqual(advice.currency, "EUR")
        self.assertEqual(advice.invoices[0].currency, "USD")

    def test_a_document_states_nothing_where_only_the_advice_does(self):
        advice = read(seg("CUX", ["2", "EUR", "11"]), *document())
        self.assertEqual(advice.currency, "EUR")
        self.assertIsNone(advice.invoices[0].currency)


class WhichOfTheTwoWins(unittest.TestCase):
    """The `MOA`'s, with the group's `CUX` as the fallback — as at the head."""

    def test_the_moa_beats_the_groups_cux(self):
        advice = read(*document(on_moa="USD", on_cux="GBP"))
        self.assertEqual(advice.invoices[0].currency, "USD")
        self.assertEqual(advice.invoices[0].group_currency, "GBP")

    def test_the_cux_is_the_fallback_where_the_moa_states_none(self):
        advice = read(*document(on_cux="GBP"))
        self.assertEqual(advice.invoices[0].currency, "GBP")

    def test_the_order_in_the_group_does_not_decide_it(self):
        # A CUX may sit either side of its MOA; neither order may win.
        before = read(seg("DOC", ["380"], ["INV1"]),
                      seg("CUX", ["2", "GBP", "11"]),
                      seg("MOA", ["12", "100.00", "USD"]))
        after = read(*document(on_moa="USD", on_cux="GBP"))
        for advice in (before, after):
            self.assertEqual((advice.invoices[0].currency,
                              advice.invoices[0].group_currency),
                             ("USD", "GBP"))

    def test_agreeing_is_not_a_disagreement(self):
        advice = read(*document(on_moa="USD", on_cux="USD"))
        self.assertEqual(advice.invoices[0].currency, "USD")
        self.assertEqual(advice.invoices[0].group_currency, "USD")


class TheDisagreementIsReported(MockServerCase):

    def findings(self, body, rule):
        status, _headers, data = self.post("/edi", payload(*body),
                                           headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        _s, _h, rows = self.get("/_mock/disagreements")
        return [row for row in rows if row["rule"] == rule]

    def test_a_group_naming_two_currencies_is_said_out_loud(self):
        found = self.findings(document(EURODIS_INVOICE, on_moa="USD",
                                       on_cux="GBP"),
                              remittance.CURRENCY_DISAGREES)
        self.assertEqual(len(found), 1, found)
        self.assertEqual((found[0]["expected"], found[0]["found"]),
                         ("GBP", "USD"))
        self.assertIn("the CUX in invoice %s's group says GBP and its MOA "
                      "says USD" % EURODIS_INVOICE, found[0]["note"])

    def test_a_group_that_agrees_with_itself_is_not_reported(self):
        self.assertEqual(self.findings(
            document(EURODIS_INVOICE, on_moa="EUR", on_cux="EUR"),
            remittance.CURRENCY_DISAGREES), [])

    def test_the_case_this_issue_is_about(self):
        # EURODIS's invoice is in EUR. An EANCOM-shaped advice pays it as a
        # dollar document, saying so on the MOA. Before this, the document
        # read as stating nothing and no finding was raised at all.
        found = self.findings(document(EURODIS_INVOICE, on_moa="USD"),
                              remittance.CURRENCY_NOT_THE_INVOICE)
        self.assertEqual(len(found), 1, found)
        self.assertEqual((found[0]["expected"], found[0]["found"]),
                         ("EUR", "USD"))
        self.assertIn("the entry for invoice", found[0]["note"])

    def test_a_document_in_its_invoices_currency_is_clean(self):
        self.assertEqual(self.findings(
            document(EURODIS_INVOICE, on_moa="EUR"),
            remittance.CURRENCY_NOT_THE_INVOICE), [])

    def test_the_advice_is_still_named_where_the_document_states_nothing(self):
        # This branch keys on `item.currency is None`, which the change
        # makes false more often; it must still be true when it should be.
        found = self.findings(
            [seg("CUX", ["2", "USD", "11"])] + document(EURODIS_INVOICE),
            remittance.CURRENCY_NOT_THE_INVOICE)
        self.assertEqual(len(found), 1, found)
        self.assertIn("the advice is in USD", found[0]["note"])

    def test_an_advice_that_states_nothing_anywhere_is_not_a_finding(self):
        # #281's rule, and the easiest thing to break here.
        for rule in (remittance.CURRENCY_NOT_THE_INVOICE,
                     remittance.CURRENCY_DISAGREES):
            with self.subTest(rule=rule):
                self.assertEqual(
                    self.findings(document(EURODIS_INVOICE), rule), [])


if __name__ == "__main__":
    unittest.main()
