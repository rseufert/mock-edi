"""A repeated `CUX` or `MOA+12` in one `DOC` group is said, not dropped (#325).

D.96A's REMADV allows five `MOA` and a `CUX` loop of five inside the `DOC`
group, and `validate` says a document carrying two of each is clean. The
reader kept the first `CUX` and the *last* `MOA+12`, and said nothing about
either.

The `MOA` half lost money. Worse, it lost it inconsistently: the arithmetic
check had its own loop that added **every** amount, so

    DOC+380+INV-1 / MOA+12:60.00 / MOA+12:40.00 / UNS / MOA+12:100.00

balanced (60 + 40 = 100, no finding) while the listing reported 40.00 paid.
The mock held two readings of one document, and the finding whose job is to
catch an advice that does not add up had certified it.

Zack's decision: **the first wins, the rest are reported, nothing is
summed.** Nothing is summed because D.96A does not say two amounts in one
group add up, and assuming they do is the mock quietly changing what a payer
said. The arithmetic takes the first too, so there is one reading of the
document rather than two.

The `CUX` half is the header's rule, which the header already had and the
group did not: collect, take the first, report the rest.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, remittance, validate
from mockedi.envelope import seg

from support import EURODIS, MockServerCase, _next_control

EDIFACT_TYPE = {"Content-Type": "application/edifact"}
EURODIS_INVOICE = "INV9000001"     # EUR, EURODIS's

HEAD = (seg("BGM", ["481"], ["RA-REPEAT"], "9"),
        seg("DTM", ["137", "20260928", "102"]),
        seg("NAD", "PR", ["EURODIS", "", "92"]),
        seg("NAD", "PE", ["MOCKEDI", "", "92"]))


def group(invoice="INV1", amounts=("100.00",), currencies=()):
    out = [seg("DOC", ["380"], [invoice])]
    out += [seg("CUX", ["2", code, "11"]) for code in currencies]
    out += [seg("MOA", ["12", amount]) for amount in amounts]
    return out


def message(*body, total="100.00"):
    return edifact.message(
        "REMADV", "1",
        list(HEAD) + list(body) + [seg("UNS", "S"), seg("MOA", ["12", total])],
        version="D:96A:UN")


def payload(*body, total="100.00"):
    return edifact.render(edifact.wrap([message(*body, total=total)],
                                       EURODIS, "MOCKEDI", _next_control(4)),
                          newline=True)


def read(*body, total="100.00"):
    return remittance.read(message(*body, total=total), "EDIFACT")


class TheDocumentIsCorrect(unittest.TestCase):
    """The premise: none of this is a document the dictionary objects to."""

    def test_two_of_each_in_one_group_validates_clean(self):
        report = validate.validate_message(
            message(*group(amounts=("60.00", "40.00"),
                           currencies=("GBP", "USD"))), "EDIFACT")
        self.assertEqual([(s.tag, s.code, s.note) for s in report.segments], [])


class TheFirstAmountIsTaken(unittest.TestCase):

    def test_an_empty_5004_states_no_amount_and_is_not_the_first(self):
        # Found by Piotr: `_amount("")` is zero, so taking the first
        # segment regardless answered 0 for a group whose next MOA says
        # 40.00 - worse than the last-wins this replaced.
        advice = read(seg("DOC", ["380"], ["INV1"]),
                      seg("MOA", ["12", "", "USD"]),
                      seg("MOA", ["12", "40.00", "GBP"]), total="40.00")
        self.assertEqual((str(advice.invoices[0].paid),
                          advice.invoices[0].currency), ("40.00", "GBP"))
        self.assertEqual([str(a) for a in advice.invoices[0].amounts],
                         ["40.00"])

    def test_a_currency_on_an_amountless_moa_goes_with_the_amount(self):
        """`MOA+12::USD` alone leaves the document stating no currency.

        Found by Piotr. On `main` it read `0 USD`; it now reads nothing,
        because the empty `5004` is not an amount and #322's rule is that a
        document's amount and the currency it is in come from one segment.
        A currency belonging to a payment that was not stated is not that
        document's currency, and `paid: null` says plainly that nothing was
        stated. The group's own `CUX` is the way to say a currency without
        an amount, and it still works; the listing falls back to the
        advice's. Pinned so that it is a decision and not a silence.
        """
        advice = read(seg("DOC", ["380"], ["INV1"]),
                      seg("MOA", ["12", "", "USD"]), total="0")
        self.assertIsNone(advice.invoices[0].paid)
        self.assertIsNone(advice.invoices[0].currency)

    def test_but_the_groups_own_cux_still_says_it(self):
        advice = read(seg("DOC", ["380"], ["INV1"]),
                      seg("MOA", ["12", "", "USD"]),
                      seg("CUX", ["2", "GBP", "11"]), total="0")
        self.assertEqual(advice.invoices[0].currency, "GBP")

    def test_a_written_zero_is_an_amount_and_is_taken(self):
        advice = read(*group(amounts=("0", "40.00")), total="0")
        self.assertEqual(str(advice.invoices[0].paid), "0")

    def test_the_arithmetic_skips_an_unstated_amount_too(self):
        body = [seg("DOC", ["380"], ["INV1"]), seg("MOA", ["12", ""]),
                seg("MOA", ["12", "40.00"])]
        _total, parts, _c = remittance._total_not_parts(
            message(*body, total="100.00"), "EDIFACT")
        self.assertEqual(str(parts), "40.00")

    def test_the_first_not_the_last(self):
        advice = read(*group(amounts=("60.00", "40.00")))
        self.assertEqual(str(advice.invoices[0].paid), "60.00")

    def test_the_sum_appears_nowhere(self):
        # Zack's decision, pinned where summing would be tempting: the sum
        # is not the paid amount, not in the amounts kept, and not what the
        # arithmetic compares. Asserting only `paid != "100.00"` would pass
        # with `mockedi/` at `main`, which answers 40.00.
        body = group(amounts=("60.00", "40.00"))
        advice = read(*body)
        self.assertEqual(str(advice.invoices[0].paid), "60.00")
        self.assertNotIn("100.00",
                         [str(a) for a in advice.invoices[0].amounts])
        _total, parts, _counted = remittance._total_not_parts(
            message(*body), "EDIFACT")
        self.assertEqual(str(parts), "60.00")

    def test_every_amount_is_kept(self):
        advice = read(*group(amounts=("60.00", "40.00", "1.00")))
        self.assertEqual([str(a) for a in advice.invoices[0].amounts],
                         ["60.00", "40.00", "1.00"])

    def test_one_amount_is_the_ordinary_case_and_unchanged(self):
        advice = read(*group(amounts=("100.00",)))
        self.assertEqual((str(advice.invoices[0].paid),
                          [str(a) for a in advice.invoices[0].amounts]),
                         ("100.00", ["100.00"]))

    def test_each_group_counts_its_own(self):
        advice = read(*group("INV1", ("60.00", "40.00")),
                      *group("INV2", ("5.00",)))
        self.assertEqual([(i.invoice, str(i.paid), len(i.amounts))
                          for i in advice.invoices],
                         [("INV1", "60.00", 2), ("INV2", "5.00", 1)])


class TheFirstCurrencyIsTaken(unittest.TestCase):

    def test_the_first_of_several(self):
        advice = read(*group(currencies=("GBP", "USD")))
        self.assertEqual(advice.invoices[0].group_currency, "GBP")

    def test_and_the_rest_are_kept(self):
        advice = read(*group(currencies=("GBP", "USD")))
        self.assertEqual(advice.invoices[0].group_currencies, ["GBP", "USD"])

    def test_one_currency_is_the_ordinary_case_and_unchanged(self):
        advice = read(*group(currencies=("GBP",)))
        self.assertEqual((advice.invoices[0].group_currency,
                          advice.invoices[0].currency), ("GBP", "GBP"))

    def test_none_is_still_none(self):
        advice = read(*group())
        self.assertIsNone(advice.invoices[0].group_currency)
        self.assertEqual(advice.invoices[0].group_currencies, [])


class TheArithmeticAgreesWithTheListing(unittest.TestCase):
    """The contradiction this fixes: one reading of the document, not two."""

    def test_the_sum_takes_the_first_of_each_group(self):
        found = remittance._total_not_parts(
            message(*group(amounts=("60.00", "40.00"))), "EDIFACT")
        self.assertIsNotNone(found, "60.00 against a 100.00 total must report")
        total, parts, _counted = found
        self.assertEqual((str(total), str(parts)), ("100.00", "60.00"))

    def test_what_it_reports_is_what_the_listing_shows(self):
        body = group(amounts=("60.00", "40.00"))
        _total, parts, _counted = remittance._total_not_parts(
            message(*body), "EDIFACT")
        self.assertEqual(parts, read(*body).invoices[0].paid)

    def test_an_advice_whose_first_amounts_do_add_up_is_clean(self):
        self.assertIsNone(remittance._total_not_parts(
            message(*group("INV1", ("60.00", "999.00")),
                    *group("INV2", ("40.00",))), "EDIFACT"))

    def test_an_ajt_amount_is_still_not_a_document_amount(self):
        # It never was in the per-document sum, and must not become one.
        body = [seg("DOC", ["380"], ["INV1"]), seg("MOA", ["12", "100.00"]),
                seg("AJT", "1"), seg("MOA", ["12", "7.00"])]
        self.assertIsNone(remittance._total_not_parts(
            message(*body), "EDIFACT"))


class WhatIsReported(MockServerCase):

    def findings(self, *body, total="100.00"):
        status, _h, data = self.post("/edi", payload(*body, total=total),
                                     headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        _s, _h, rows = self.get("/_mock/disagreements")
        return {row["rule"]: row for row in rows}

    def test_a_second_amount_is_said_out_loud(self):
        found = self.findings(*group(EURODIS_INVOICE, ("60.00", "40.00")),
                              total="60.00")
        self.assertIn(remittance.AMOUNTS_REPEATED, found)
        row = found[remittance.AMOUNTS_REPEATED]
        self.assertEqual((row["expected"], row["found"]), ("60.00", "40.00"))
        self.assertIn("60.00 and 40.00", row["note"])
        self.assertIn("60.00 taken, being the first", row["note"])
        self.assertIn("not added to it", row["note"])

    def test_a_second_currency_is_said_out_loud(self):
        found = self.findings(*group(EURODIS_INVOICE, ("100.00",),
                                     ("EUR", "USD")))
        self.assertIn(remittance.CURRENCY_DISAGREES, found)
        row = found[remittance.CURRENCY_DISAGREES]
        self.assertIn("group names EUR, USD", row["note"])
        self.assertIn("EUR taken as the document's, being the first",
                      row["note"])

    def test_one_of_each_reports_neither(self):
        found = self.findings(*group(EURODIS_INVOICE, ("100.00",), ("EUR",)))
        self.assertNotIn(remittance.AMOUNTS_REPEATED, found)
        self.assertNotIn(remittance.CURRENCY_DISAGREES, found)

    def test_the_listing_shows_the_first_amount(self):
        self.findings(*group(EURODIS_INVOICE, ("60.00", "40.00")),
                      total="60.00")
        _s, _h, rows = self.get("/_mock/remittances?partner=" + EURODIS)
        self.assertEqual([i["paid"] for i in rows[-1]["invoices"]], ["60.00"])

    def test_two_equal_amounts_are_still_reported(self):
        # Named `repeated` and not `disagrees` for this case: two equal
        # amounts do not disagree, but they are ambiguous about whether one
        # payment was stated twice or two were made. Two equal currencies
        # say one thing twice and draw nothing; found by Piotr.
        found = self.findings(*group(EURODIS_INVOICE, ("50.00", "50.00"),
                                     ("EUR", "EUR")), total="50.00")
        self.assertIn(remittance.AMOUNTS_REPEATED, found)
        self.assertNotIn(remittance.CURRENCY_DISAGREES, found)

    def test_the_currencies_are_named_in_the_order_the_document_gave(self):
        # The note's reason is "being the first", so a sorted list that did
        # not start with it would read as a contradiction. Found by Piotr.
        found = self.findings(*group(EURODIS_INVOICE, ("100.00",),
                                     ("USD", "EUR")))
        self.assertIn("group names USD, EUR", found[
            remittance.CURRENCY_DISAGREES]["note"])

    def test_the_document_is_still_accepted(self):
        # Reported, not refused: it is a correct document that says two
        # things, and the mock says which it took.
        status, _h, data = self.post(
            "/edi", payload(*group(EURODIS_INVOICE, ("60.00", "40.00")),
                            total="60.00"), headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["accepted"], data)


if __name__ == "__main__":
    unittest.main()
