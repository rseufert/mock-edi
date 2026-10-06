"""The currency a remittance advice states, and what disagrees with it (#281).

`GET /_mock/remittances` answered a `total` and, per invoice, a `paid`, and no
currency anywhere, so `"total": "1416.00"` was 1416.00 of nothing in
particular. Whoever reads the listing to tie a remittance to a bank payment or
a cleared item had an amount and had to assume the rest, and the arithmetic
check compared an advice with the invoices it pays without asking whether they
were the same kind of money: a EUR advice against a USD invoice of the same
figure passed.

Where each dialect states it, sourced on the issue:

- **X12 820**: `CUR02`. `BPR` carries no currency at all - 21 positions in
  004010 and in 005010, element 100 among none of them - and `CUR` is 0..1 at
  heading level, outside the `ENT` loop that holds the invoices. So an 820
  cannot state two, and `total` is unambiguous once `CUR02` is read.
- **D.96A REMADV**: the summary `MOA`'s own 6345 (`C516`'s third component),
  falling back to the header `CUX`. `CUX` recurs in SG3, SG5 and SG9 and every
  `MOA` may carry its own, so a REMADV *can* state several - and the directory
  gives **no rule for which wins** when the header and the summary disagree.
  The mock reports that disagreement rather than picking a side.

An advice that states no currency is answered `null`, not `"USD"`. An absent
currency and a guessed one are different claims, and the orders and invoices
this mock writes state theirs; a default here would put a guess beside facts.

The seeded invoices this leans on: `INV9000000` is ACME's, in USD, and
`INV9000001` is EURODIS's, in EUR.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, remittance, x12
from mockedi.envelope import seg

from support import ACME, EURODIS, MockServerCase, _next_control

X12_TYPE = {"Content-Type": "application/edi-x12"}
EDIFACT_TYPE = {"Content-Type": "application/edifact"}

ACME_INVOICE = "INV9000000"        # USD
EURODIS_INVOICE = "INV9000001"     # EUR


def an_820(currency=None, total="100.00", invoice=ACME_INVOICE):
    """A remittance-only 820, with a CUR where one is asked for."""
    body = [seg("BPR", "I", total, "C", "NON", "", "", "", "", "",
                "", "", "", "", "", "", "20260928"),
            seg("TRN", "1", "TR-CUR")]
    if currency:
        body.append(seg("CUR", "PR", currency))
    body += [seg("N1", "PR", "Acme Distribution Inc"),
             seg("N1", "PE", "Mock EDI Supply Co"),
             seg("ENT", "1"),
             seg("RMR", "IV", invoice, "", total, total),
             seg("DTM", "003", "20260920")]
    control = _next_control(9)
    return x12.render(x12.wrap([x12.message("820", "0001", body)], ACME,
                               "MOCKEDI", control, str(int(control)), "RA"),
                      newline=True)


def a_remadv(header=None, summary_currency=None, total="100.00",
             invoice=EURODIS_INVOICE):
    """A REMADV with a header CUX, a summary MOA currency, either or neither."""
    body = [seg("BGM", ["481"], ["RA-CUR"], "9"),
            seg("DTM", ["137", "20260928", "102"]),
            seg("NAD", "PR", ["EURODIS", "", "92"]),
            seg("NAD", "PE", ["MOCKEDI", "", "92"])]
    if header:
        body.append(seg("CUX", ["2", header, "11"]))
    body += [seg("DOC", ["380"], [invoice]), seg("MOA", ["12", total]),
             seg("UNS", "S")]
    body.append(seg("MOA", ["12", total] + ([summary_currency]
                                            if summary_currency else [])))
    return edifact.render(edifact.wrap(
        [edifact.message("REMADV", "1", body, version="D:96A:UN")],
        EURODIS, "MOCKEDI", _next_control(4)), newline=True)


class WhatTheListingSays(MockServerCase):

    def advised(self, payload, headers):
        status, _h, data = self.post("/edi", payload, headers=headers)
        self.assertEqual(status, 200, data)
        _s, _h, rows = self.get("/_mock/remittances")
        return rows[-1]

    def test_an_820_answers_cur02(self):
        row = self.advised(an_820("USD"), X12_TYPE)
        self.assertEqual(row["currency"], "USD")

    def test_an_820_with_no_cur_answers_null(self):
        row = self.advised(an_820(), X12_TYPE)
        self.assertIsNone(row["currency"])

    def test_a_remadv_answers_its_header_cux(self):
        row = self.advised(a_remadv(header="EUR"), EDIFACT_TYPE)
        self.assertEqual(row["currency"], "EUR")

    def test_a_summary_moa_states_its_own_and_wins(self):
        # C516's third component. The mock has to choose, because D.96A does
        # not; it takes the one on the amount it is reporting.
        row = self.advised(a_remadv(header="EUR", summary_currency="GBP"),
                           EDIFACT_TYPE)
        self.assertEqual(row["currency"], "GBP")

    def test_a_remadv_with_neither_answers_null(self):
        row = self.advised(a_remadv(), EDIFACT_TYPE)
        self.assertIsNone(row["currency"])

    def test_total_is_still_there_and_nothing_is_renamed(self):
        row = self.advised(an_820("USD"), X12_TYPE)
        for key in ("total", "trace", "creditDebit", "settles", "invoices",
                    "status", "reversedBy", "currency"):
            self.assertIn(key, row)
        self.assertEqual(row["total"], "100.00")


class WhenItDisagreesWithTheInvoice(MockServerCase):

    def findings(self, payload, headers):
        status, _h, data = self.post("/edi", payload, headers=headers)
        self.assertEqual(status, 200, data)
        _s, _h, rows = self.get("/_mock/disagreements")
        return {row["rule"]: row for row in rows}

    def test_a_eur_820_against_a_usd_invoice(self):
        found = self.findings(an_820("EUR"), X12_TYPE)
        self.assertIn(remittance.CURRENCY_NOT_THE_INVOICE, found)
        row = found[remittance.CURRENCY_NOT_THE_INVOICE]
        self.assertEqual((row["expected"], row["found"]), ("USD", "EUR"))
        self.assertIn("not comparable", row["note"])
        self.assertIn(ACME_INVOICE, row["note"])

    def test_a_usd_820_against_the_same_invoice_is_clean(self):
        found = self.findings(an_820("USD"), X12_TYPE)
        self.assertNotIn(remittance.CURRENCY_NOT_THE_INVOICE, found)

    def test_a_remadv_in_the_invoices_currency_is_clean(self):
        found = self.findings(a_remadv(header="EUR"), EDIFACT_TYPE)
        self.assertNotIn(remittance.CURRENCY_NOT_THE_INVOICE, found)

    def test_a_remadv_in_another_currency_is_not(self):
        found = self.findings(a_remadv(header="USD"), EDIFACT_TYPE)
        self.assertIn(remittance.CURRENCY_NOT_THE_INVOICE, found)
        row = found[remittance.CURRENCY_NOT_THE_INVOICE]
        self.assertEqual((row["expected"], row["found"]), ("EUR", "USD"))

    def test_an_advice_that_states_none_is_not_a_finding(self):
        # Not knowing is not the same as being wrong, and a default to
        # compare against would be the guess #281 asks us not to make.
        found = self.findings(an_820(), X12_TYPE)
        self.assertNotIn(remittance.CURRENCY_NOT_THE_INVOICE, found)

    def test_an_invoice_the_mock_never_issued_is_not_compared(self):
        # The mock can only say two currencies differ about an invoice it
        # wrote. An advice naming somebody else's gets no finding.
        found = self.findings(an_820("EUR", invoice="INV-NOT-OURS"), X12_TYPE)
        self.assertNotIn(remittance.CURRENCY_NOT_THE_INVOICE, found)


class WhenAREMADVDisagreesWithItself(MockServerCase):
    """The one D.96A declines to settle."""

    def findings(self, payload):
        status, _h, data = self.post("/edi", payload, headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        _s, _h, rows = self.get("/_mock/disagreements")
        return {row["rule"]: row for row in rows}

    def test_a_header_cux_and_a_summary_moa_that_differ(self):
        found = self.findings(a_remadv(header="EUR", summary_currency="GBP"))
        self.assertIn(remittance.CURRENCY_DISAGREES, found)
        row = found[remittance.CURRENCY_DISAGREES]
        self.assertEqual((row["expected"], row["found"]), ("EUR", "GBP"))
        self.assertIn("does not say which wins", row["note"])

    def test_the_same_currency_twice_is_not_a_finding(self):
        found = self.findings(a_remadv(header="EUR", summary_currency="EUR"))
        self.assertNotIn(remittance.CURRENCY_DISAGREES, found)

    def test_only_one_of_them_is_not_a_finding(self):
        for advice in (a_remadv(header="EUR"), a_remadv(summary_currency="EUR")):
            with self.subTest():
                self.assertNotIn(remittance.CURRENCY_DISAGREES,
                                 self.findings(advice))

    def test_an_820_cannot_reach_this_rule(self):
        # CUR is 0..1 at heading level and there is no second place to state
        # one, so the rule is EDIFACT's by the shape of the standard.
        found = self.findings(a_remadv(header="EUR", summary_currency="EUR"))
        self.assertNotIn(remittance.CURRENCY_DISAGREES, found)
        status, _h, _d = self.post("/edi", an_820("EUR"), headers=X12_TYPE)
        self.assertEqual(status, 200)
        _s, _h, rows = self.get("/_mock/disagreements")
        self.assertNotIn(remittance.CURRENCY_DISAGREES,
                         {row["rule"] for row in rows})


if __name__ == "__main__":
    unittest.main()
