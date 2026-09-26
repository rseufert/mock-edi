"""Reading what a supplier sends, not only what this mock writes.

`tests/test_dictionary.py` round-trips the readers against the writers, which
proves they agree with each other. It cannot prove they agree with anybody
else - and a reader that only accepts its own side's output is no use for the
job this project exists to do.

So the documents below are built to be *unlike* the mock's profile, in the
ways real senders differ: the item number under a qualifier that is not first
in the list, optional segments left out entirely, lines with no numbers on
them, a pack level between the order and the item in an 856's hierarchy, a
line amount that disagrees with the line's own price.

They are constructed for that purpose rather than lifted from a named
implementation guide - what matters is that they are not what
`transactions.py` writes, and every one of them would have been read wrongly
or not at all by a reader written only against our own output.
"""
import datetime
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import edifact, transactions, x12
from mockedi.envelope import seg


def x12_message(code, body):
    return x12.message(code, "0001", body)


def edifact_message(code, body):
    return edifact.message(code, "1", body, version="D:96A:UN")


class AnEightFiveFiveFromSomebodyElse(unittest.TestCase):
    """An 855 written the way another translator writes one."""

    def read(self, body):
        return transactions.read_response(x12_message("855", body), "X12")

    def test_the_item_number_is_taken_from_whatever_qualifier_is_there(self):
        # SA rather than VP, and the UPC first in the segment.
        response = self.read([
            seg("BAK", "00", "AD", "PO-77", "20260101"),
            seg("PO1", "1", "10", "EA", "5.00", "",
                "UP", "000012345678", "SA", "SUPP-ART-9"),
            seg("ACK", "IA", "10", "EA"),
        ])
        self.assertEqual(response.lines[0].sku, "SUPP-ART-9")
        self.assertEqual(response.lines[0].upc, "000012345678")

    def test_an_unlisted_qualifier_is_still_read_as_the_item(self):
        response = self.read([
            seg("BAK", "00", "AD", "PO-78", "20260101"),
            seg("PO1", "1", "3", "EA", "1.00", "", "ZZ", "ODD-ONE"),
            seg("ACK", "IA", "3", "EA"),
        ])
        self.assertEqual(response.lines[0].sku, "ODD-ONE")

    def test_a_line_with_no_number_takes_its_position(self):
        response = self.read([
            seg("BAK", "00", "AD", "PO-79", "20260101"),
            seg("PO1", "", "1", "EA", "1.00", "", "VP", "A"),
            seg("ACK", "IA", "1", "EA"),
            seg("PO1", "", "2", "EA", "2.00", "", "VP", "B"),
            seg("ACK", "IA", "2", "EA"),
        ])
        self.assertEqual([line.number for line in response.lines], ["1", "2"])

    def test_a_line_with_no_ack_at_all_is_confirmed_as_ordered(self):
        # Plenty of senders answer a whole order with BAK02 and leave the
        # lines alone. Refusing that would reject a perfectly ordinary 855.
        response = self.read([
            seg("BAK", "00", "AT", "PO-80", "20260101"),
            seg("PO1", "1", "12", "EA", "3.00", "", "VP", "A"),
        ])
        self.assertEqual(response.lines[0].confirmed, Decimal("12"))
        self.assertEqual(response.lines[0].status, transactions.ACCEPTED)

    def test_a_rejected_line_is_confirmed_as_nothing(self):
        response = self.read([
            seg("BAK", "00", "AD", "PO-81", "20260101"),
            seg("PO1", "1", "9", "EA", "3.00", "", "VP", "A"),
            seg("ACK", "IR", "9", "EA"),     # a sender that echoes the quantity
        ])
        self.assertEqual(response.lines[0].confirmed, Decimal("0"))
        self.assertEqual(response.rejected, response.lines)

    def test_a_short_line_reports_what_is_missing(self):
        response = self.read([
            seg("BAK", "00", "AC", "PO-82", "20260101"),
            seg("PO1", "1", "100", "EA", "1.00", "", "VP", "A"),
            seg("ACK", "IQ", "60", "EA", "068", "20260210"),
        ])
        line = response.lines[0]
        self.assertEqual(line.short_by, Decimal("40"))
        self.assertEqual(line.scheduled_on, datetime.date(2026, 2, 10))
        self.assertEqual(response.short, [line])

    def test_a_promised_date_without_its_qualifier_is_still_read(self):
        response = self.read([
            seg("BAK", "00", "AD", "PO-83", "20260101"),
            seg("PO1", "1", "5", "EA", "1.00", "", "VP", "A"),
            seg("ACK", "IA", "5", "EA", "20260315"),   # date in ACK04
        ])
        self.assertEqual(response.lines[0].scheduled_on,
                         datetime.date(2026, 3, 15))

    def test_the_response_date_falls_back_to_a_dtm(self):
        response = self.read([
            seg("BAK", "00", "AD", "PO-84", "20260101"),
            seg("DTM", "137", "20260105"),
            seg("PO1", "1", "1", "EA", "1.00", "", "VP", "A"),
        ])
        self.assertEqual(response.responded_on, datetime.date(2026, 1, 5))

    def test_a_missing_mandatory_segment_is_not_an_exception(self):
        # The same contract `read_order` has: a reader reports what is there.
        # Saying a document is malformed is `validate.py`'s job, and a reader
        # that raised would take that decision away from the 997.
        response = self.read([
            seg("PO1", "1", "1", "EA", "1.00", "", "VP", "A"),
            seg("ACK", "IA", "1", "EA"),
        ])
        self.assertEqual(response.po_number, "")
        self.assertEqual(len(response.lines), 1)


class AnEightFiveSixWithAPackLevel(unittest.TestCase):
    """An 856 whose hierarchy is not the three flat levels the mock writes."""

    def read(self, body):
        return transactions.read_despatch(x12_message("856", body), "X12")

    def test_items_under_a_pack_level_are_still_found(self):
        # Shipment > Order > Pack > Item, which is ordinary in retail. Reading
        # the tree by position rather than by parent pointer loses every item.
        despatch = self.read([
            seg("BSN", "00", "SH-1", "20260201", "1200", "0002"),
            seg("HL", "1", "", "S", "1"),
            seg("TD1", "CTN25", "4", "", "", "", "G", "18", "LB"),
            seg("TD5", "", "2", "FDEG", "M", "FedEx Ground"),
            seg("REF", "BM", "BOL-1"),
            seg("DTM", "011", "20260201"),
            seg("HL", "2", "1", "O", "1"),
            seg("PRF", "PO-90", "", "", "20260115"),
            seg("HL", "3", "2", "P", "1"),
            seg("HL", "4", "3", "I", "0"),
            seg("LIN", "1", "VP", "A-1"),
            seg("SN1", "1", "7", "EA", "", "10", "EA"),
            seg("HL", "5", "3", "I", "0"),
            seg("LIN", "2", "VP", "A-2"),
            seg("SN1", "2", "3", "EA"),
        ])
        self.assertEqual(despatch.po_number, "PO-90")
        self.assertEqual([item.line for item in despatch.items], ["1", "2"])
        self.assertEqual(despatch.items[0].quantity, Decimal("7"))
        self.assertEqual(despatch.items[0].ordered, Decimal("10"))
        self.assertEqual(despatch.total_quantity, Decimal("10"))

    def test_the_shipment_details_are_read_from_the_s_level(self):
        despatch = self.read([
            seg("BSN", "00", "SH-2", "20260201", "1200", "0004"),
            seg("HL", "1", "", "S", "1"),
            seg("TD1", "CTN25", "9", "", "", "", "G", "40.5", "LB"),
            seg("TD5", "", "2", "UPSN", "M", "United Parcel Service"),
            seg("REF", "BM", "BOL-2"),
            seg("REF", "CN", "1Z-TRACK"),
            seg("DTM", "011", "20260203"),
            seg("HL", "2", "1", "O", "1"),
            seg("PRF", "PO-91"),
        ])
        self.assertEqual(despatch.cartons, 9)
        self.assertEqual(despatch.weight, Decimal("40.5"))
        self.assertEqual((despatch.scac, despatch.carrier),
                         ("UPSN", "United Parcel Service"))
        self.assertEqual(despatch.bol, "BOL-2")
        self.assertEqual(despatch.tracking, "1Z-TRACK")
        self.assertEqual(despatch.shipped_on, datetime.date(2026, 2, 3))

    def test_an_item_with_no_lin_is_read_from_its_sn1(self):
        despatch = self.read([
            seg("BSN", "00", "SH-3", "20260201", "1200", "0004"),
            seg("HL", "1", "", "S", "1"),
            seg("HL", "2", "1", "O", "1"),
            seg("PRF", "PO-92"),
            seg("HL", "3", "2", "I", "0"),
            seg("SN1", "4", "6", "CA"),
        ])
        self.assertEqual(despatch.items[0].line, "4")
        self.assertEqual(despatch.items[0].uom, "CA")

    def test_an_item_outside_any_order_is_left_out(self):
        # A sender that hangs an item straight off the shipment has told us
        # nothing about which order it belongs to.
        despatch = self.read([
            seg("BSN", "00", "SH-4", "20260201", "1200", "0004"),
            seg("HL", "1", "", "S", "1"),
            seg("HL", "2", "1", "I", "0"),
            seg("LIN", "1", "VP", "STRAY"),
            seg("SN1", "1", "1", "EA"),
        ])
        self.assertEqual(despatch.items, [])


class AnInvoiceThatDoesNotAddUp(unittest.TestCase):
    """The three-way match's whole reason for existing."""

    def test_an_810s_total_has_two_implied_decimals(self):
        invoice = transactions.read_invoice(x12_message("810", [
            seg("BIG", "20260201", "INV-1", "20260115", "PO-95", "", "", "DI", "00"),
            seg("IT1", "1", "10", "EA", "12.50", "", "VP", "A"),
            seg("TDS", "12500"),
        ]), "X12")
        self.assertEqual(invoice.total, Decimal("125.00"))
        self.assertEqual(invoice.line_total, Decimal("125.00"))

    def test_a_line_amount_that_disagrees_is_reported_not_corrected(self):
        # An INVOIC states the line amount in MOA+203. When it disagrees with
        # the price times the quantity, the reader keeps what was said: the
        # disagreement is the finding, and a reader that recomputed would
        # hide it.
        invoice = transactions.read_invoice(edifact_message("INVOIC", [
            seg("BGM", ["380"], ["INV-2"], "9"),
            seg("DTM", ["137", "20260201", "102"]),
            seg("RFF", ["ON", "PO-96"]),
            seg("CUX", ["2", "GBP", "4"]),
            seg("LIN", "1", "", ["A", "VP"]),
            seg("QTY", ["47", "10", "PCE"]),
            seg("MOA", ["203", "99.00"]),          # should be 125.00
            seg("PRI", ["AAA", "12.50"]),
            seg("UNS", "S"),
            seg("MOA", ["139", "125.00"]),
        ]), "EDIFACT")
        self.assertEqual(invoice.lines[0].amount, Decimal("99.00"))
        self.assertEqual(invoice.total, Decimal("125.00"))
        self.assertNotEqual(invoice.line_total, invoice.total)

    def test_an_x12_line_amount_is_worked_out_when_nobody_states_it(self):
        invoice = transactions.read_invoice(x12_message("810", [
            seg("BIG", "20260201", "INV-3", "20260115", "PO-97"),
            seg("IT1", "1", "4", "EA", "2.25", "", "VP", "A"),
            seg("TDS", "900"),
        ]), "X12")
        self.assertEqual(invoice.lines[0].amount, Decimal("9.00"))

    def test_terms_and_a_discount_are_read_from_itd(self):
        invoice = transactions.read_invoice(x12_message("810", [
            seg("BIG", "20260201", "INV-4", "20260115", "PO-98"),
            seg("ITD", "08", "3", "2", "", "10", "", "45"),
            seg("IT1", "1", "1", "EA", "1.00", "", "VP", "A"),
            seg("TDS", "100"),
        ]), "X12")
        self.assertEqual(invoice.terms_days, 45)
        self.assertEqual(invoice.discount_pct, Decimal("2"))
        self.assertEqual(invoice.discount_days, 10)

    def test_tax_is_summed_from_every_txi(self):
        invoice = transactions.read_invoice(x12_message("810", [
            seg("BIG", "20260201", "INV-5", "20260115", "PO-99"),
            seg("IT1", "1", "1", "EA", "10.00", "", "VP", "A"),
            seg("TDS", "1200"),
            seg("TXI", "ST", "1.00"),
            seg("TXI", "CT", "1.00"),
        ]), "X12")
        self.assertEqual(invoice.tax, Decimal("2.00"))


class AnOrdrspFromSomebodyElse(unittest.TestCase):
    """EDIFACT says the line verdict by how much it confirms."""

    def read(self, body):
        return transactions.read_response(
            edifact_message("ORDRSP", body), "EDIFACT")

    def header(self, po_number):
        return [seg("BGM", ["231"], ["SO-1"], "9", "4"),
                seg("DTM", ["137", "20260201", "102"]),
                seg("RFF", ["ON", po_number]),
                seg("CUX", ["2", "SEK", "9"])]

    def test_confirming_everything_is_accepted(self):
        response = self.read(self.header("PO-A") + [
            seg("LIN", "1", "", ["A", "VP"]),
            seg("QTY", ["21", "10", "PCE"]),
            seg("QTY", ["113", "10", "PCE"]),
        ])
        self.assertEqual(response.lines[0].status, transactions.ACCEPTED)
        self.assertEqual(response.currency, "SEK")

    def test_confirming_less_is_short(self):
        response = self.read(self.header("PO-B") + [
            seg("LIN", "1", "", ["A", "VP"]),
            seg("QTY", ["21", "10", "PCE"]),
            seg("QTY", ["113", "4", "PCE"]),
            seg("QTY", ["83", "6", "PCE"]),
        ])
        self.assertEqual(response.lines[0].status, transactions.SHORT)
        self.assertEqual(response.lines[0].short_by, Decimal("6"))

    def test_confirming_nothing_is_rejected(self):
        response = self.read(self.header("PO-C") + [
            seg("LIN", "1", "", ["A", "VP"]),
            seg("QTY", ["21", "10", "PCE"]),
            seg("QTY", ["113", "0", "PCE"]),
            seg("FTX", "AAO", "", "", ["discontinued"]),
        ])
        self.assertEqual(response.lines[0].status, transactions.REJECTED)
        self.assertEqual(response.lines[0].reason, "discontinued")

    def test_a_line_with_only_one_quantity_is_read_as_confirmed_in_full(self):
        # A sender that states what it will deliver and nothing else.
        response = self.read(self.header("PO-D") + [
            seg("LIN", "1", "", ["A", "VP"]),
            seg("QTY", ["113", "8", "PCE"]),
        ])
        self.assertEqual(response.lines[0].quantity, Decimal("8"))
        self.assertEqual(response.lines[0].confirmed, Decimal("8"))
        self.assertEqual(response.lines[0].status, transactions.ACCEPTED)

    def test_an_item_number_from_a_pia_is_found(self):
        response = self.read(self.header("PO-E") + [
            seg("LIN", "1", "", ["", ""]),
            seg("PIA", "1", ["SUPP-9", "SA"], ["000012345678", "EN"]),
            seg("QTY", ["113", "1", "PCE"]),
        ])
        self.assertEqual(response.lines[0].sku, "SUPP-9")
        self.assertEqual(response.lines[0].upc, "000012345678")

    def test_the_unit_comes_back_in_x12_terms(self):
        response = self.read(self.header("PO-F") + [
            seg("LIN", "1", "", ["A", "VP"]),
            seg("QTY", ["113", "2", "CT"]),
        ])
        self.assertEqual(response.lines[0].uom, "CA")

    def test_a_unit_the_mock_cannot_translate_is_kept_as_it_came(self):
        response = self.read(self.header("PO-F2") + [
            seg("LIN", "1", "", ["A", "VP"]),
            seg("QTY", ["113", "2", "ZZZ"]),
        ])
        self.assertEqual(response.lines[0].uom, "ZZZ")


class ADesadvFromSomebodyElse(unittest.TestCase):

    def test_the_order_line_comes_from_rff_on(self):
        # 1156 in RFF+ON is the buyer's own line number, which is what the
        # buyer needs to match against - not the position in the despatch.
        despatch = transactions.read_despatch(edifact_message("DESADV", [
            seg("BGM", ["351"], ["SH-9"], "9"),
            seg("DTM", ["11", "20260205", "102"]),
            seg("RFF", ["ON", "PO-G"]),
            seg("TDT", "20", "BOL-9", "", "", ["DHLC", "", "", "DHL"]),
            seg("PAC", "3", "", ["CT", "", "", "Carton"]),
            seg("LIN", "1", "", ["A", "VP"]),
            seg("QTY", ["12", "5", "PCE"]),
            seg("RFF", ["ON", "PO-G", "7"]),
        ]), "EDIFACT")
        self.assertEqual(despatch.po_number, "PO-G")
        self.assertEqual(despatch.cartons, 3)
        self.assertEqual((despatch.scac, despatch.carrier), ("DHLC", "DHL"))
        self.assertEqual(despatch.items[0].line, "7")
        self.assertEqual(despatch.items[0].quantity, Decimal("5"))


if __name__ == "__main__":
    unittest.main()
