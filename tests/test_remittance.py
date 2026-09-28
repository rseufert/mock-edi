"""A remittance advice: the X12 820 and the EDIFACT REMADV (#149).

The mock sells, so it is the payee: it is *sent* a remittance advice, checks
it against the same dictionary it checks everything against, and answers
with a 997 or CONTRL. Beyond the syntax, one thing is refused by name: an 820
used as a payment order, a bank's document.

An advice whose total is not the sum of its parts can still be read, so it is
acknowledged; that disagreement is a business finding beside the 997 (#156),
never a rejection in it (#116).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, x12
from mockedi.envelope import seg

from support import ACME, EURODIS, MockServerCase, _next_control

X12_TYPE = {"Content-Type": "application/edi-x12"}
EDIFACT_TYPE = {"Content-Type": "application/edifact"}


def x12_remittance(trace="TR-0001", total="150.00", paid=(("INV8000001", "100.00"),
                                                           ("INV8000002", "50.00")),
                   handling="I", settles="20260928", sender=ACME, extra_bpr=None,
                   deductions=(), credit_debit="C"):
    """A remittance-only 820: BPR, TRN, payer and payee, one ENT and an RMR per invoice.

    `deductions` are ENT-level ADXs: an amount and a reason not tied to one
    invoice, which BPR02 includes and no RMR04 does. An entry of `paid` may
    carry a third item, an ADX (amount, reason) inside that invoice's RMR loop.
    """
    bpr = extra_bpr or seg("BPR", handling, total, credit_debit, "NON", "", "", "", "", "",
                           "", "", "", "", "", "", settles)
    body = [bpr, seg("TRN", "1", trace),
            seg("N1", "PR", "Acme Distribution Inc"),
            seg("N1", "PE", "Mock EDI Supply Co"),
            seg("ENT", "1")]
    body += [seg("ADX", amount, reason) for amount, reason in deductions]
    for invoice, amount, *adjustment in paid:
        body.append(seg("RMR", "IV", invoice, "", amount, amount))
        body.append(seg("DTM", "003", "20260920"))
        body += [seg("ADX", *item) for item in adjustment]
    control = _next_control(9)
    return x12.render(x12.wrap([x12.message("820", "0001", body)], sender,
                               "MOCKEDI", control, str(int(control)), "RA"),
                      newline=True)


def edifact_remittance(number="RA-0001", total="150.00",
                       paid=(("INV8000001", "100.00"), ("INV8000002", "50.00")),
                       summary=True):
    body = [seg("BGM", ["481"], [number], "9"),
            seg("DTM", ["137", "20260928", "102"]),
            seg("NAD", "PR", ["EURODIS", "", "92"]),
            seg("NAD", "PE", ["MOCKEDI", "", "92"]),
            seg("CUX", ["2", "EUR", "11"])]
    for invoice, amount, *adjustment in paid:
        body.append(seg("DOC", ["380"], [invoice]))
        body.append(seg("MOA", ["12", amount]))
        # An AJT group: its reason, and an amount of its own.
        for reason, adjusted in adjustment:
            body.append(seg("AJT", reason))
            body.append(seg("MOA", ["12", adjusted]))
    body.append(seg("UNS", "S"))
    if summary:
        body.append(seg("MOA", ["12", total]))
    return edifact.render(edifact.wrap(
        [edifact.message("REMADV", "1", body, version="D:96A:UN")],
        EURODIS, "MOCKEDI", _next_control(4)), newline=True)


class TheDictionary(MockServerCase):
    def test_the_820_is_described(self):
        status, _h, data = self.get("/_mock/dictionary/X12/820")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["name"], "Payment Order/Remittance Advice")
        text = str(data)
        for tag in ("BPR", "TRN", "ENT", "RMR", "ADX"):
            self.assertIn("'%s'" % tag, text)

    def test_the_remadv_is_described(self):
        status, _h, data = self.get("/_mock/dictionary/EDIFACT/REMADV")
        self.assertEqual(status, 200, data)
        for tag in ("BGM", "DOC", "MOA", "AJT", "UNS"):
            self.assertIn("'%s'" % tag, str(data))


class AnX12Remittance(MockServerCase):
    def send_820(self, payload):
        status, _h, data = self.post("/edi", payload, headers=X12_TYPE)
        self.assertEqual(status, 200, data)
        return data

    def ack(self):
        return self.document(ACME, "acknowledgment").groups[0].messages[0]

    def test_a_clean_one_is_accepted_and_acknowledged(self):
        data = self.send_820(x12_remittance())
        self.assertTrue(data["accepted"], data)
        self.assertEqual(data["transactionSets"][0]["kind"], "remittance")
        ak5 = self.ack().find("AK5")
        self.assertEqual(ak5.get(1), "A")
        self.assertEqual(self.ack().find("AK1").get(1), "RA")

    def test_it_is_archived_under_its_trace_number(self):
        self.send_820(x12_remittance(trace="TR-ARCHIVE"))
        _s, _h, rows = self.get("/_mock/documents?direction=in&code=820")
        self.assertEqual([(r["reference"], r["accepted"]) for r in rows],
                         [("TR-ARCHIVE", 1)])

    def test_a_malformed_one_is_refused_where_it_is_wrong(self):
        # BPR03, the credit/debit flag, is mandatory: missing, it is fatal.
        data = self.send_820(x12_remittance(extra_bpr=seg(
            "BPR", "I", "150.00", "", "NON")))
        self.assertFalse(data["accepted"])
        ack = self.ack()
        self.assertEqual(ack.find("AK5").get(1), "R")
        self.assertEqual(ack.find("AK3").get(1), "BPR")
        self.assertEqual((ack.find("AK4").get(1), ack.find("AK4").get(3)),
                         ("3", "1"))

    def test_a_bad_date_is_reported_but_not_fatal(self):
        # As for every set: an invalid value is an error, accepted and said.
        data = self.send_820(x12_remittance(settles="20269999"))
        self.assertTrue(data["accepted"])
        ack = self.ack()
        self.assertEqual(ack.find("AK5").get(1), "E")
        self.assertEqual((ack.find("AK3").get(1), ack.find("AK4").get(1)),
                         ("BPR", "16"))

    def disagreements(self, data):
        return data["transactionSets"][0]["disagreements"]

    def test_a_total_that_does_not_add_up_is_still_acknowledged(self):
        # Readable, so a 997 says A: the arithmetic is a business finding,
        # reported beside the acknowledgment rather than in it (#116).
        data = self.send_820(x12_remittance(
            total="150.00", paid=(("INV8000001", "100.00"),)))
        self.assertTrue(data["accepted"], data)
        self.assertEqual(self.ack().find("AK5").get(1), "A")
        self.assertIsNone(self.ack().find("AK3"))

    def test_and_says_so_beside_the_997(self):
        # The run paid two invoices, one payment came back AC04, and the
        # advice still claims the full total while listing one invoice.
        data = self.send_820(x12_remittance(
            total="150.00", paid=(("INV8000001", "100.00"),)))
        [found] = self.disagreements(data)
        self.assertEqual(found["rule"], "remittance-total-not-parts")
        self.assertEqual((found["found"], found["expected"]), ("150.00", "100.00"))
        self.assertIn("BPR02 says 150.00 was paid", found["note"])
        self.assertEqual(found["order"], "")

    def test_it_is_kept_with_the_other_disagreements(self):
        self.send_820(x12_remittance(total="150.00", paid=(("INV8000001", "100.00"),)))
        _s, _h, rows = self.get("/_mock/disagreements?partner=" + ACME)
        self.assertEqual([(r["rule"], r["kind"], r["code"]) for r in rows],
                         [("remittance-total-not-parts", "remittance", "820")])

    def test_the_997_is_the_same_as_for_a_clean_one(self):
        # #126's guard: a disagreement changes nothing a 997 says.
        def ack_of(payload):
            self.send_820(payload)
            ack = self.ack()
            self.mailbox(ACME, leave=False)
            return [(item.tag, item.elements) for item in ack.segments
                    if item.tag not in ("ST", "SE", "AK1")]
        clean = ack_of(x12_remittance(total="100.00", paid=(("INV-1", "100.00"),)))
        wrong = ack_of(x12_remittance(total="150.00", paid=(("INV-1", "100.00"),)))
        self.assertEqual(wrong, clean)

    def test_a_deduction_not_tied_to_one_invoice_is_part_of_the_payment(self):
        # 1000.00 invoiced, 50.00 deducted at the ENT level: BPR02 is 950.00
        # and no RMR04 says so. A correct 820, and the shape an AP system
        # writes for an unapplied deduction.
        data = self.send_820(x12_remittance(
            total="950.00", paid=(("INV-1", "1000.00"),),
            deductions=(("-50.00", "CS"),)))
        self.assertTrue(data["accepted"], data)
        self.assertEqual(data["transactionSets"][0]["findings"], [])
        self.assertEqual(self.disagreements(data), [])

    def test_an_adjustment_inside_an_rmr_is_already_in_its_amount(self):
        # RMR04 is 90.00 paid on a 100.00 invoice, with the 10.00 discount's
        # ADX in the RMR loop: counting that ADX again would be wrong.
        data = self.send_820(x12_remittance(
            total="90.00", paid=(("INV-2", "90.00", ("-10.00", "01")),)))
        self.assertTrue(data["accepted"], data)
        self.assertEqual(self.disagreements(data), [])

    def test_the_totals_may_include_a_credit(self):
        data = self.send_820(x12_remittance(
            total="70.00", paid=(("INV8000001", "100.00"), ("CM-1", "-30.00"))))
        self.assertTrue(data["accepted"], data)
        self.assertEqual(self.disagreements(data), [])

    def test_a_payment_order_is_refused_by_name(self):
        for handling in ("D", "P", "U", "X"):
            with self.subTest(bpr01=handling):
                data = self.send_820(x12_remittance(handling=handling))
                self.assertFalse(data["accepted"])
                self.assertTrue(any("payment order" in f and "pain.001" in f
                                    for f in data["transactionSets"][0]["findings"]))
                self.mailbox(ACME, leave=False)

    def test_payment_accompanying_the_advice_is_still_a_remittance(self):
        self.assertTrue(self.send_820(x12_remittance(handling="C"))["accepted"])

    def test_nothing_else_is_answered(self):
        self.send_820(x12_remittance())
        self.assertEqual([r["code"] for r in self.mailbox(ACME)], ["997"])


class NobodyAcknowledgedTheRemittance(MockServerCase):
    def test_no_ack_applies_to_an_820(self):
        self.behaviour(ACME, "no-ack")
        status, _h, data = self.post("/edi", x12_remittance(), headers=X12_TYPE)
        self.assertEqual(status, 200, data)
        self.assertTrue(data["accepted"])
        self.assertEqual(self.mailbox(ACME), [])


class AnEdifactRemittance(MockServerCase):
    def send_remadv(self, payload):
        status, _h, data = self.post("/edi", payload, headers=EDIFACT_TYPE)
        self.assertEqual(status, 200, data)
        return data

    def contrl(self):
        return self.document(EURODIS, "acknowledgment").groups[0].messages[0]

    def test_a_clean_one_is_accepted(self):
        data = self.send_remadv(edifact_remittance(number="RA-CLEAN"))
        self.assertTrue(data["accepted"], data)
        _s, _h, rows = self.get("/_mock/documents?direction=in&code=REMADV")
        self.assertEqual([r["reference"] for r in rows], ["RA-CLEAN"])

    def test_a_total_that_does_not_add_up_is_still_acknowledged(self):
        data = self.send_remadv(edifact_remittance(
            total="150.00", paid=(("INV8000001", "100.00"),)))
        self.assertTrue(data["accepted"], data)
        ucm = self.contrl().find("UCM")
        self.assertEqual((ucm.comp(2, 1), ucm.get(3)), ("REMADV", "7"))
        [found] = data["transactionSets"][0]["disagreements"]
        self.assertEqual((found["found"], found["expected"]), ("150.00", "100.00"))
        self.assertIn("MOA+12 after UNS says 150.00", found["note"])

    def test_a_clean_one_has_no_disagreement(self):
        data = self.send_remadv(edifact_remittance())
        self.assertEqual(data["transactionSets"][0]["disagreements"], [])

    def test_an_adjustments_amount_is_already_in_its_document(self):
        # 90.00 remitted on the DOC, whose AJT group carries the -10.00 that
        # explains it. Counting the AJT's MOA+12 again would make 80.00 of a
        # correct 90.00 total.
        data = self.send_remadv(edifact_remittance(
            total="90.00", paid=(("INV8000003", "90.00", ("1", "-10.00")),)))
        self.assertTrue(data["accepted"], data)
        self.assertEqual(data["transactionSets"][0]["disagreements"], [])

    def test_the_summary_amount_may_be_left_out(self):
        payload = edifact_remittance(summary=False)
        self.assertNotIn("MOA+12:150.00", payload)
        self.assertTrue(self.send_remadv(payload)["accepted"])


def _utc_day(offset=0):
    """The mock's date, from the same UTC clock it reads, moved by `offset` days."""
    import datetime
    day = datetime.datetime.now(datetime.timezone.utc).date()
    return (day + datetime.timedelta(days=offset)).strftime("%Y%m%d")


class SentBeforeTheMoneySettles(MockServerCase):
    """#149's first failure: an advice the payee reconciles before the cash is there."""

    def send(self, settles):
        status, _h, data = self.post("/edi", x12_remittance(settles=settles),
                                     headers=X12_TYPE)
        self.assertEqual(status, 200, data)
        return data

    def rules(self, data):
        return [d["rule"] for d in data["transactionSets"][0]["disagreements"]]

    def test_an_advice_that_takes_effect_later_is_found(self):
        data = self.send(_utc_day(3))
        self.assertTrue(data["accepted"])
        self.assertEqual(self.rules(data), ["remitted-before-settlement"])
        found = data["transactionSets"][0]["disagreements"][0]
        self.assertIn("cash that has not arrived", found["note"])
        ack = self.document(ACME, "acknowledgment").groups[0].messages[0]
        self.assertEqual(ack.find("AK5").get(1), "A")

    def test_one_that_has_taken_effect_is_not(self):
        self.assertEqual(self.rules(self.send(_utc_day(0))), [])
        self.assertEqual(self.rules(self.send(_utc_day(-1))), [])

    def test_the_mocks_clock_decides_so_advance_moves_it(self):
        self.post("/_mock/advance?seconds=%d" % (4 * 86400))
        self.assertEqual(self.rules(self.send(_utc_day(3))), [])


class ThePaymentCameBack(MockServerCase):
    """#149's second failure: the advice is corrected by a reversal, or it is not."""

    def send(self, trace, credit_debit, total="150.00"):
        status, _h, data = self.post("/edi", x12_remittance(
            trace=trace, credit_debit=credit_debit, total=total), headers=X12_TYPE)
        self.assertEqual(status, 200, data)
        return data

    def listing(self):
        _s, _h, rows = self.get("/_mock/remittances?partner=" + ACME)
        return rows

    def test_a_reversal_marks_the_advice_it_reverses(self):
        paid = self.send("TR-RETURNED", "C")
        reversal = self.send("TR-RETURNED", "D")
        self.assertEqual(reversal["transactionSets"][0]["disagreements"], [])
        first, second = self.listing()
        # Both sets are ST02 0001, as they so often are: the archive id is
        # what tells them apart.
        self.assertEqual(paid["transactionSets"][0]["control"],
                         reversal["transactionSets"][0]["control"])
        self.assertEqual((first["status"], first["reversedBy"]),
                         ("reversed", second["id"]))
        self.assertEqual(second["status"], "reversal")

    def test_an_advice_nothing_reversed_is_still_advised(self):
        self.send("TR-KEPT", "C")
        self.send("TR-OTHER", "C")
        self.assertEqual([r["status"] for r in self.listing()], ["advised", "advised"])

    def test_a_reversal_of_nothing_is_found(self):
        data = self.send("TR-NEVER-ADVISED", "D")
        self.assertTrue(data["accepted"])
        [found] = data["transactionSets"][0]["disagreements"]
        self.assertEqual(found["rule"], "reversal-of-nothing")
        self.assertIn("TR-NEVER-ADVISED", found["note"])


class TheListing(MockServerCase):
    def test_it_says_what_each_advice_says(self):
        self.post("/edi", x12_remittance(trace="TR-LIST", settles="20260928"),
                  headers=X12_TYPE)
        self.post("/edi", edifact_remittance(number="RA-LIST"), headers=EDIFACT_TYPE)
        _s, _h, rows = self.get("/_mock/remittances")
        x12_row, edifact_row = rows
        self.assertEqual((x12_row["trace"], x12_row["total"], x12_row["creditDebit"],
                          x12_row["settles"]), ("TR-LIST", "150.00", "C", "2026-09-28"))
        self.assertEqual([i["invoice"] for i in x12_row["invoices"]],
                         ["INV8000001", "INV8000002"])
        self.assertEqual((edifact_row["trace"], edifact_row["total"]),
                         ("RA-LIST", "150.00"))
        self.assertEqual([i["paid"] for i in edifact_row["invoices"]],
                         ["100.00", "50.00"])

    def test_a_refused_advice_is_not_listed(self):
        self.post("/edi", x12_remittance(handling="D"), headers=X12_TYPE)
        _s, _h, rows = self.get("/_mock/remittances")
        self.assertEqual(rows, [])

    def test_it_is_filtered_by_partner(self):
        self.post("/edi", x12_remittance(), headers=X12_TYPE)
        _s, _h, rows = self.get("/_mock/remittances?partner=GLOBEX")
        self.assertEqual(rows, [])


class FromASupplier(MockServerCase):
    def test_the_mock_does_not_take_a_remittance_from_someone_it_pays(self):
        status, _h, data = self.post(
            "/edi", x12_remittance(sender="NORTHWIND"), headers=X12_TYPE)
        self.assertEqual(status, 200, data)
        self.assertFalse(data["accepted"])
        self.assertIn("not one it receives",
                      data["transactionSets"][0]["findings"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
