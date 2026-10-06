"""A currency per document in a REMADV, which is how it pays in two (#298).

D.96A puts `CUX` in three places, five of each — [edifactory.de's D96A
REMADV](https://www.edifactory.de/edifact/directory/D96A/message/REMADV) and
xedi's agree:

| Group | Level | Max |
| --- | --- | --- |
| SG3 | header | 5 |
| SG5 | document, inside the `DOC` group | 5 |
| SG9 | line, inside the line group SG8 | 5 |

This dictionary declared the header one — at nine, which was its own number
and not the standard's — and nothing else. So a REMADV stating a currency per
document, which is the whole reason an advice can pay invoices in more than
one, was answered `no segment CUX is expected here` and refused, where a
translator that knows D.96A accepts it. Same class as #202 and #239: the
dictionary says `D:96A:UN` and declared something narrower.

**SG9 is still not declared**, and deliberately: this message has no line
group for it to sit in, so a `CUX` inside a line remains an unexpected
segment. That is honest while `LIN` is absent, and it is the one piece of
D.96A's three this change leaves alone.

The reader was already written not to look inside a `DOC` group, with a
comment saying that reading a segment the validator refuses would be
answering a document the mock turns away (#281). That comment was the
placeholder for this.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, remittance, schema
from mockedi.envelope import seg

from support import EURODIS, MockServerCase, _next_control

EDIFACT_TYPE = {"Content-Type": "application/edifact"}

ACME_INVOICE = "INV9000000"        # USD, ACME's
EURODIS_INVOICE = "INV9000001"     # EUR, EURODIS's


def a_remadv(header=None, documents=(), total="100.00"):
    """A REMADV whose DOC groups may each state their own currency.

    `documents` are `(invoice, amount, currency or None)`.
    """
    body = [seg("BGM", ["481"], ["RA-DOC"], "9"),
            seg("DTM", ["137", "20260928", "102"]),
            seg("NAD", "PR", ["EURODIS", "", "92"]),
            seg("NAD", "PE", ["MOCKEDI", "", "92"])]
    if header:
        body.append(seg("CUX", ["2", header, "11"]))
    for invoice, amount, currency in documents:
        body.append(seg("DOC", ["380"], [invoice]))
        body.append(seg("MOA", ["12", amount]))
        if currency:
            body.append(seg("CUX", ["2", currency, "11"]))
    body += [seg("UNS", "S"), seg("MOA", ["12", total])]
    return edifact.render(edifact.wrap(
        [edifact.message("REMADV", "1", body, version="D:96A:UN")],
        EURODIS, "MOCKEDI", _next_control(4)), newline=True)


class WhatTheDictionaryDeclares(unittest.TestCase):

    def loops(self):
        found = {}

        def walk(items, inside=""):
            for item in items:
                if isinstance(item, schema.Loop):
                    for child in item.children:
                        if (isinstance(child, schema.Use)
                                and child.segment.tag == "CUX"):
                            found[inside or "header"] = item.repeat
                    walk(item.children, item.id)

        walk(schema.EDIFACT_SETS["REMADV"].children)
        return found

    def test_the_header_group_is_five_as_sg3_is(self):
        self.assertEqual(self.loops().get("header"), 5)

    def test_and_the_document_group_has_one_too(self):
        self.assertEqual(self.loops().get("DOC"), 5)

    def test_but_no_line_level_one_because_there_is_no_line_group(self):
        tags = set()

        def walk(items):
            for item in items:
                if isinstance(item, schema.Loop):
                    walk(item.children)
                elif isinstance(item, schema.Use):
                    tags.add(item.segment.tag)

        walk(schema.EDIFACT_SETS["REMADV"].children)
        self.assertNotIn("LIN", tags)


class ADocumentStatingItsOwnCurrency(MockServerCase):

    def advise(self, payload):
        status, _headers, data = self.post("/edi", payload,
                                           headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        _s, _h, rows = self.get("/_mock/remittances")
        _s, _h, found = self.get("/_mock/disagreements")
        return rows[-1], [row for row in found]

    def test_the_segment_is_no_longer_unexpected(self):
        row, _found = self.advise(a_remadv(
            header="EUR", documents=[(EURODIS_INVOICE, "100.00", "EUR")]))
        self.assertEqual(row["currency"], "EUR")

    def test_the_listing_answers_a_currency_per_invoice(self):
        row, _found = self.advise(a_remadv(
            header="EUR",
            documents=[(EURODIS_INVOICE, "60.00", "EUR"),
                       (ACME_INVOICE, "40.00", "USD")]))
        self.assertEqual(
            [(item["invoice"], item["currency"]) for item in row["invoices"]],
            [(EURODIS_INVOICE, "EUR"), (ACME_INVOICE, "USD")])

    def test_a_document_that_states_none_falls_back_to_the_advices(self):
        row, _found = self.advise(a_remadv(
            header="EUR", documents=[(EURODIS_INVOICE, "100.00", None)]))
        self.assertEqual(row["invoices"][0]["currency"], "EUR")

    def test_and_with_neither_it_is_null(self):
        row, _found = self.advise(a_remadv(
            documents=[(EURODIS_INVOICE, "100.00", None)]))
        self.assertIsNone(row["invoices"][0]["currency"])


class WhichLoopACUXBelongsTo(unittest.TestCase):
    """Both loops begin on `CUX`, and the validator does not check sequence.

    So which loop a `CUX` falls into is decided by the reader's position in
    the message, and that wants testing rather than reasoning about — the
    senior's question on the pull request. Position is the right basis: an
    EDIFACT segment group *is* positional, and a `CUX` after the first `DOC`
    is inside that `DOC`'s group by definition.

    The last case is the one the question found. A stray `CUX` after `UNS`
    was being counted as the header's, which would report a disagreement
    about a segment that is not a header `CUX` at all. The dictionary
    declares none in the summary section, but the validator checks which
    segments a set may hold and not where they sit, so it accepts the
    document and the reader cannot lean on it.
    """

    def read(self, *body):
        payload = edifact.render(edifact.wrap(
            [edifact.message("REMADV", "1", list(body), version="D:96A:UN")],
            EURODIS, "MOCKEDI", _next_control(4)))
        _group, message = list(edifact.parse(payload).messages())[0]
        return remittance.read(message, "EDIFACT")

    HEAD = (seg("BGM", ["481"], ["RA-WHICH"], "9"),
            seg("DTM", ["137", "20260928", "102"]),
            seg("NAD", "PR", ["EURODIS", "", "92"]),
            seg("NAD", "PE", ["MOCKEDI", "", "92"]))
    DOC = (seg("DOC", ["380"], ["INV1"]), seg("MOA", ["12", "1.00"]))
    TAIL = (seg("UNS", "S"), seg("MOA", ["12", "1.00"]))

    def test_before_the_first_doc_it_is_the_headers(self):
        advice = self.read(*self.HEAD, seg("CUX", ["2", "EUR", "11"]),
                           *self.DOC, *self.TAIL)
        self.assertEqual(advice.header_currencies, ["EUR"])
        self.assertIsNone(advice.invoices[0].currency)

    def test_after_the_first_doc_it_is_that_documents(self):
        advice = self.read(*self.HEAD, *self.DOC,
                           seg("CUX", ["2", "GBP", "11"]), *self.TAIL)
        self.assertEqual(advice.header_currencies, [])
        self.assertEqual(advice.invoices[0].currency, "GBP")

    def test_both_at_once_are_not_conflated(self):
        advice = self.read(*self.HEAD, seg("CUX", ["2", "EUR", "11"]),
                           *self.DOC, seg("CUX", ["2", "GBP", "11"]),
                           *self.TAIL)
        self.assertEqual(advice.header_currencies, ["EUR"])
        self.assertEqual(advice.invoices[0].currency, "GBP")

    def test_each_document_gets_its_own(self):
        advice = self.read(
            *self.HEAD, seg("CUX", ["2", "EUR", "11"]),
            seg("DOC", ["380"], ["INV1"]), seg("MOA", ["12", "1.00"]),
            seg("CUX", ["2", "GBP", "11"]),
            seg("DOC", ["380"], ["INV2"]), seg("MOA", ["12", "1.00"]),
            seg("CUX", ["2", "USD", "11"]), *self.TAIL)
        self.assertEqual([(item.invoice, item.currency)
                          for item in advice.invoices],
                         [("INV1", "GBP"), ("INV2", "USD")])

    def test_a_stray_cux_after_uns_is_nobodys(self):
        advice = self.read(*self.HEAD, seg("CUX", ["2", "EUR", "11"]),
                           *self.DOC, *self.TAIL,
                           seg("CUX", ["2", "GBP", "11"]))
        self.assertEqual(advice.header_currencies, ["EUR"])
        self.assertIsNone(advice.invoices[0].currency)


class TheComparisonIsPerDocument(MockServerCase):
    """An advice can now be right about one invoice and wrong about another."""

    def findings(self, payload):
        status, _headers, data = self.post("/edi", payload,
                                           headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        _s, _h, rows = self.get("/_mock/disagreements")
        return [row for row in rows
                if row["rule"] == remittance.CURRENCY_NOT_THE_INVOICE]

    def test_the_document_currency_is_what_is_compared(self):
        # EURODIS's invoice is in EUR. The advice says EUR at the head and
        # USD over that document, so the document is what is wrong.
        found = self.findings(a_remadv(
            header="EUR", documents=[(EURODIS_INVOICE, "100.00", "USD")]))
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["expected"], found[0]["found"]),
                         ("EUR", "USD"))
        self.assertIn("the entry for invoice", found[0]["note"])

    def test_a_document_in_its_invoices_currency_is_clean_even_if_the_head_is_not(self):
        # The head says USD and the document says EUR, which matches the
        # invoice. Before #298 the head would have been compared and this
        # would have been a finding about a document that is correct.
        found = self.findings(a_remadv(
            header="USD", documents=[(EURODIS_INVOICE, "100.00", "EUR")]))
        self.assertEqual(found, [])

    def test_the_advice_is_named_where_the_document_states_nothing(self):
        found = self.findings(a_remadv(
            header="USD", documents=[(EURODIS_INVOICE, "100.00", None)]))
        self.assertEqual(len(found), 1)
        self.assertIn("the advice is in USD", found[0]["note"])

    def test_one_right_and_one_wrong_in_the_same_advice(self):
        """Two entries for one invoice, one in its currency and one not.

        An odd document - it pays the same invoice twice - and it is the
        shape that isolates the property with the seeded data, because the
        mock compares only against invoices *it* issued to that partner. An
        advice from EURODIS naming ACME's invoice gets no finding at all,
        which is right and proves nothing here; and a second EURODIS invoice
        in another currency cannot be made through the business path, since
        the invoice takes the partner's currency and not the order's.
        """
        found = self.findings(a_remadv(
            header="EUR",
            documents=[(EURODIS_INVOICE, "60.00", "EUR"),
                       (EURODIS_INVOICE, "40.00", "USD")]))
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["expected"], found[0]["found"]),
                         ("EUR", "USD"))

    def test_an_invoice_the_partner_was_never_sent_is_not_compared(self):
        # ACME's invoice, named by a EURODIS advice. The mock can only say
        # two currencies differ about an invoice it issued to that partner.
        self.assertEqual(self.findings(a_remadv(
            header="EUR", documents=[(ACME_INVOICE, "40.00", "GBP")])), [])

    def test_a_document_currency_with_no_advice_currency_is_still_compared(self):
        # The guard used to return early when the advice stated none; a
        # document may state one where the advice does not.
        found = self.findings(a_remadv(
            documents=[(EURODIS_INVOICE, "100.00", "USD")]))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["found"], "USD")


if __name__ == "__main__":
    unittest.main()
