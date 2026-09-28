"""Orders the mock places with a supplier, as they are stored.

The mock has only ever received orders, and every column of `purchase_order`
and `order_line` meant what the seller did. An order it places means what it
asked for, and the seller's machinery - deciding lines, packing, invoicing,
applying a customer's change - must never touch one. These are the storage
rules; placing one over HTTP and reading what the supplier sends back is
#125's wiring, tested with it.
"""
import datetime
import os
import sys
import unittest
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import db, documents, edifact, schema, transactions, validate, x12
from mockedi.transactions import Party

from support import (ACME, MockServerCase, _next_control, acknowledge, parse,
                     x12_change, x12_order)

US = Party(role="BY", name="Mock EDI Inc", identifier="MOCKEDI",
           street="1 Test St", city="Springfield", region="IL", postal="62701",
           country="US")
SUPPLIER = {"id": "NORTHWIND", "name": "Northwind Traders", "street": "",
            "city": "", "region": "", "postal": "", "country": "US"}
LINES = [{"sku": "WIDGET-001", "quantity": "100", "uom": "EA", "price": "12.50"},
         {"sku": "GADGET-042", "quantity": "5", "price": "3.10"}]


class Stored(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()
        db.seed(self.conn)
        self.addCleanup(self.conn.close)

    def place(self, **request):
        request.setdefault("lines", LINES)
        return documents.place_order(self.conn, SUPPLIER, US, request)


class PlacingAnOrder(Stored):
    def test_it_is_stored_as_placed_with_what_was_asked_for(self):
        order = self.place(requested_on="2026-10-15")
        self.assertEqual((order["direction"], order["status"], order["partner"]),
                         ("placed", "placed", "NORTHWIND"))
        self.assertEqual(order["total"], "1265.50")      # 100 x 12.50 + 5 x 3.10
        self.assertEqual(order["ship_to_id"], "MOCKEDI")
        line = documents.order_lines(self.conn, order["po_number"], NORTHWIND)[0]
        self.assertEqual((line["quantity"], line["price"], line["ordered_price"]),
                         ("100", "12.50", "12.50"))
        # Nothing the supplier has said yet, and nothing the seller decided.
        self.assertEqual((line["status"], line["confirmed"], line["shipped"],
                          line["invoiced"]), ("", "0", "0", "0"))

    def test_the_number_is_the_mocks_own_unless_given(self):
        first, second = self.place(), self.place()
        self.assertEqual(first["po_number"], "4800000001")
        self.assertEqual(second["po_number"], "4800000002")
        self.assertEqual(self.place(po_number="PO-MINE")["po_number"], "PO-MINE")

    def test_the_catalogue_fills_in_the_other_item_number(self):
        order = self.place()
        line = documents.order_lines(self.conn, order["po_number"], NORTHWIND)[0]
        self.assertTrue(line["upc"])
        self.assertTrue(line["description"])

    def test_every_problem_is_named_at_once(self):
        self.place(po_number="PO-TAKEN")
        with self.assertRaises(documents.Refused) as caught:
            self.place(po_number="PO-TAKEN", requested_on="15/10/2026",
                       lines=[{"quantity": "0", "price": "x"}])
        text = str(caught.exception)
        for fragment in ("already exists", "not a date", "names no item",
                         "price 'x' is not a number", "for 0 of something"):
            self.assertIn(fragment, text)
        self.assertEqual(len(caught.exception.problems), 5)


class ChangingIt(Stored):
    def setUp(self):
        super().setUp()
        self.po = self.place()["po_number"]

    def change(self, **request):
        return documents.change_placed(self.conn, self.po, NORTHWIND, request)

    def test_a_line_changed_added_and_deleted(self):
        change = self.change(lines=[
            {"line": "1", "quantity": "80"},
            {"line": "2", "action": "delete"},
            {"action": "add", "sku": "WIDGET-001", "quantity": "1", "price": "12.00"}])
        self.assertEqual([(l.number, l.action, l.quantity) for l in change.lines],
                         [("1", "CA", Decimal("80")), ("2", "DI", Decimal("5")),
                          ("3", "AI", Decimal("1"))])
        self.assertFalse(change.cancels)
        rows = {r["line"]: r for r in documents.order_lines(self.conn, self.po, NORTHWIND)}
        self.assertEqual(sorted(rows), ["1", "3"])
        self.assertEqual((rows["1"]["quantity"], rows["1"]["price"]), ("80", "12.50"))
        self.assertEqual(documents.order_row(self.conn, self.po, NORTHWIND)["total"], "1012.00")

    def test_changes_are_numbered_in_sequence(self):
        first = self.change(lines=[{"line": "1", "quantity": "90"}])
        second = self.change(lines=[{"line": "1", "quantity": "95"}])
        self.assertEqual((first.sequence, second.sequence), ("1", "2"))

    def test_a_cancellation(self):
        change = self.change(cancel=True)
        self.assertTrue(change.cancels)
        self.assertEqual(documents.order_row(self.conn, self.po, NORTHWIND)["status"], "cancelled")
        with self.assertRaises(documents.Refused):
            self.change(lines=[{"line": "1", "quantity": "1"}])

    def test_what_cannot_be_changed_is_named(self):
        with self.assertRaises(documents.Refused) as caught:
            self.change(lines=[{"line": "9", "quantity": "1"},
                               {"line": "1", "action": "replace"},
                               {"line": "1", "action": "add", "sku": "WIDGET-001",
                                "quantity": "1", "price": "1"}])
        self.assertEqual(len(caught.exception.problems), 3, caught.exception.problems)

    def test_deleting_every_line_is_a_cancellation_not_a_change(self):
        with self.assertRaises(documents.Refused) as caught:
            self.change(lines=[{"line": "1", "action": "delete"},
                               {"line": "2", "action": "delete"}])
        self.assertIn("cancel: true", str(caught.exception))

    def test_only_a_placed_order_is_changed_this_way(self):
        with self.assertRaises(LookupError):
            documents.change_placed(self.conn, "NO-SUCH-PO", NORTHWIND, {"cancel": True})


class TheSellerLeavesItAlone(MockServerCase):
    """A customer using the number of an order the mock placed.

    Its number, not its order: orders are keyed by partner and number (#132),
    so the customer's traffic is about its own order and the placed one is
    untouched whatever arrives.
    """

    def setUp(self):
        super().setUp()
        conn = self.httpd.mock.conn
        documents.place_order(conn, SUPPLIER, US,
                              {"po_number": "PO-PLACED", "lines": LINES})

    def placed(self):
        order = self.order("PO-PLACED?partner=NORTHWIND")
        self.assertEqual((order["direction"], order["lines"][0]["quantity"]),
                         ("placed", "100"))
        return order

    def test_a_customers_850_is_its_own_order(self):
        summary = self.send(x12_order("PO-PLACED"))
        self.assertEqual(summary["orders"], ["PO-PLACED"])
        self.assertEqual(self.order("PO-PLACED?partner=ACME")["direction"],
                         "received")
        self.placed()

    def test_its_860_changes_nothing_of_the_mocks(self):
        summary = self.send(x12_change("PO-PLACED"))
        self.assertEqual([r["reason"] for r in summary["refusals"]],
                         [documents.NOT_FOUND])
        self.placed()

    def test_nor_does_an_850_restating_it(self):
        self.send(x12_order("PO-PLACED", purpose="04"))  # a restatement
        self.placed()

    def test_and_the_mock_does_not_answer_it_on_demand(self):
        status, _h, data = self.post("/_mock/send", {
            "partner": NORTHWIND, "kind": schema.RESPONSE, "po": "PO-PLACED"})
        self.assertEqual(status, 400, data)
        self.assertIn("the mock placed", data["error"])
        status, _h, data = self.post("/_mock/send", {
            "partner": ACME, "kind": schema.RESPONSE, "po": "PO-PLACED"})
        self.assertEqual(status, 400, data)
        self.assertIn("ACME has no purchase order 'PO-PLACED'; NORTHWIND does",
                      data["error"])


# -- over HTTP, with a supplier on the other end

NORTHWIND = "NORTHWIND"
WHEN = datetime.datetime(2026, 9, 25, 10, 0)


def supplier_sends(kind, po_number, sender=NORTHWIND, dialect="X12",
                   description="Widget"):
    """What a supplier's translator sends about `po_number`: an 855, 856 or 810.

    Written by the mock's own seller-side writers with the parties turned
    round, so the documents are the ones a real seller of the mock's kind
    produces.
    """
    supplier = Party(role="SE", name="Northwind Components Ltd", identifier=sender,
                     country="US")
    mock = {"id": "MOCKEDI", "name": "Mock EDI", "qualifier": "ZZ", "street": "",
            "city": "", "region": "", "postal": "", "country": "US", "duns": "",
            "dialect": dialect, "version": "004010"}
    order = {"po_number": po_number, "seller_order": "SO-77", "ordered_on":
             "2026-09-24", "requested_on": "", "currency": "USD", "total": "1250.00",
             "ship_to_name": "Mock EDI", "ship_to_id": "MOCKEDI", "ship_to_street": "",
             "ship_to_city": "", "ship_to_region": "", "ship_to_postal": "",
             "ship_to_country": "US"}
    lines = [{"line": "1", "sku": "WIDGET-001", "upc": "", "description": description,
              "quantity": "100", "uom": "EA", "price": "12.50",
              "ordered_price": "12.50", "status": "IA", "confirmed": "100",
              "shipped": "100", "invoiced": "100", "reason": "", "scheduled_on": ""}]
    shipment = {"shipment_id": "SH-1", "shipped_on": "2026-09-25", "tracking": "1Z1",
                "bol": "BOL-1", "carrier": "UPS", "scac": "UPSN", "cartons": "1",
                "weight": "10"}
    invoice = {"invoice_number": "INV-1", "total": "1250.00", "subtotal": "1250.00", "invoiced_on":
               "2026-09-25", "tax": "0.00", "currency": "USD"}
    if kind == schema.RESPONSE:
        body = transactions.write_response(dialect, supplier, mock, order, lines, WHEN)
    elif kind == schema.DESPATCH:
        body = transactions.write_despatch(dialect, supplier, mock, order, lines,
                                           shipment, WHEN)
    else:
        body = transactions.write_invoice(dialect, supplier, mock, order, lines,
                                          invoice, shipment, WHEN)
    code = schema.set_code(dialect, kind)
    control = _next_control(9)
    if dialect == "X12":
        return x12.render(x12.wrap([x12.message(code, "0001", body)], sender,
                                   "MOCKEDI", control, control.lstrip("0"),
                                   schema.lookup(dialect, code).group))
    return edifact.render(edifact.wrap([edifact.message(code, "1", body, "D:96A:UN")],
                                       sender, "MOCKEDI", control))


class BuyingCase(MockServerCase):
    def flip_role(self, partner, role):
        """Put a partner's role out of step with the orders it holds.

        #146 refuses this at the PATCH while the partner holds a live order,
        so it can no longer be done through the control plane. A database
        written before that refusal existed can still hold it and the mock
        opens those in place, so this is the only shape it now arrives in -
        and writing it here keeps these tests meaning what they say however
        far along the orders happen to be.
        """
        with self.httpd.mock.lock:
            self.httpd.mock.conn.execute(
                "UPDATE partner SET role = ? WHERE id = ?", (role, partner))
            self.httpd.mock.conn.commit()
        self.assertEqual(self.get("/_mock/partners/" + partner)[2]["role"], role)

    def purchase(self, partner=NORTHWIND, **request):
        request.setdefault("lines", LINES)
        return self.post("/_mock/purchase", dict(request, partner=partner))

    def placed(self, **request):
        status, _h, data = self.purchase(**request)
        self.assertEqual(status, 201, data)
        return data

    def sent(self, kind, partner=NORTHWIND):
        rows = self.mailbox(partner, kind)
        self.assertTrue(rows, "nothing of kind %s for %s" % (kind, partner))
        return rows[-1]["payload"]


class PlacingOverHttp(BuyingCase):
    def test_the_850_goes_out_and_says_what_was_ordered(self):
        order = self.placed(po_number="4500001001", requested_on="2026-10-15")
        self.assertEqual((order["direction"], order["sent"]["code"]),
                         ("placed", "850"))
        interchange = parse(self.sent(schema.ORDER))
        self.assertTrue(validate.validate(interchange).clean)
        message = interchange.groups[0].messages[0]
        # The mock is the buyer and the goods come to it; the supplier sells.
        self.assertEqual({n1.get(1): n1.get(4) for n1 in message.find_all("N1")},
                         {"BY": "MOCKEDI", "SE": NORTHWIND, "ST": "MOCKEDI"})
        read = transactions.read_order(message, "X12")
        self.assertEqual(read.po_number, "4500001001")
        self.assertEqual([(l.sku, l.quantity) for l in read.lines],
                         [("WIDGET-001", Decimal("100")), ("GADGET-042", Decimal("5"))])

    def test_in_edifact_for_an_edifact_supplier(self):
        self.post("/_mock/partners", {"id": "NORDIC", "dialect": "EDIFACT",
                                      "version": "D:96A:UN", "role": "supplier",
                                      "qualifier": "14"})
        self.placed(partner="NORDIC", po_number="PO-EU-1")
        interchange = parse(self.sent(schema.ORDER, "NORDIC"))
        self.assertEqual(interchange.dialect, "EDIFACT")
        message = next(interchange.messages())[1]
        self.assertEqual({nad.get(1): nad.comp(2, 1) for nad in message.find_all("NAD")},
                         {"BY": "MOCKEDI", "SU": "NORDIC", "DP": "MOCKEDI"})
        self.assertEqual(transactions.read_order(message, "EDIFACT").po_number,
                         "PO-EU-1")

    def test_the_mock_does_not_order_from_a_customer(self):
        status, _h, data = self.purchase(partner=ACME)
        self.assertEqual(status, 400, data)
        self.assertIn("the mock sells to ACME; it does not order from it", data["error"])

    def test_what_is_wrong_is_named(self):
        status, _h, data = self.purchase(lines=[{"sku": "WIDGET-001"}])
        self.assertEqual(status, 400)
        self.assertEqual(len(data["problems"]), 2, data["problems"])
        status, _h, _data = self.purchase(partner="NOBODY")
        self.assertEqual(status, 404)

    def test_a_change_and_a_cancellation_go_out_as_860s(self):
        self.placed(po_number="PO-CHG")
        status, _h, data = self.post("/_mock/purchase/PO-CHG/change",
                                     {"lines": [{"line": "1", "quantity": "80"}]})
        self.assertEqual((status, data["sent"]["code"]), (200, "860"), data)
        change = transactions.read_change(
            parse(self.sent(schema.CHANGE)).groups[0].messages[0], "X12")
        self.assertEqual([(l.number, l.quantity) for l in change.lines],
                         [("1", Decimal("80"))])
        self.post("/_mock/purchase/PO-CHG/change", {"cancel": True})
        change = transactions.read_change(
            parse(self.sent(schema.CHANGE)).groups[0].messages[0], "X12")
        self.assertTrue(change.cancels)
        self.assertEqual(self.order("PO-CHG")["status"], "cancelled")

    def test_changing_an_order_the_mock_did_not_place(self):
        self.send(x12_order("PO-RECEIVED"))
        status, _h, data = self.post("/_mock/purchase/PO-RECEIVED/change",
                                     {"cancel": True})
        self.assertEqual(status, 404)
        self.assertIn("no purchase order 'PO-RECEIVED' placed", data["error"])
        self.assertNotEqual(self.order("PO-RECEIVED")["status"], "cancelled")

    def test_the_suppliers_997_acknowledges_it(self):
        self.placed(po_number="PO-ACKED")
        summary = self.send(acknowledge(self.sent(schema.ORDER)))
        self.assertEqual([(a["code"], a["status"]) for a in summary["acknowledged"]],
                         [("850", "accepted")])

    def test_the_timeline_starts_with_the_order_going_out(self):
        self.placed(po_number="PO-TL")
        _s, _h, data = self.get("/_mock/orders/PO-TL/timeline")
        self.assertEqual(data["direction"], "placed")
        self.assertEqual([(e["event"], e.get("code")) for e in data["events"]][:2],
                         [("ordered", None), ("sent", "850")])
        self.assertIn("placed with NORTHWIND", data["events"][0]["summary"])


class WhatComesBack(BuyingCase):
    def setUp(self):
        super().setUp()
        self.placed(po_number="PO-BUY")

    def test_the_855_856_and_810_are_accepted_and_filed(self):
        for kind in (schema.RESPONSE, schema.DESPATCH, schema.INVOICE):
            summary = self.send(supplier_sends(kind, "PO-BUY"))
            self.assertTrue(summary["accepted"], summary["transactionSets"])
            self.assertEqual([(f["kind"], f["order"]) for f in summary["filed"]],
                             [(kind, "PO-BUY")])
        _s, _h, data = self.get("/_mock/orders/PO-BUY/timeline")
        received = [e["code"] for e in data["events"] if e["event"] == "received"]
        self.assertEqual(received, ["855", "856", "810"])

    def test_the_timeline_has_them_after_the_order_that_prompted_them(self):
        # All inside one second, as they are when nothing is delayed: the
        # tie-break must not put the supplier's answer before the question.
        for kind in (schema.RESPONSE, schema.DESPATCH, schema.INVOICE):
            self.send(supplier_sends(kind, "PO-BUY"))
        _s, _h, data = self.get("/_mock/orders/PO-BUY/timeline")
        self.assertEqual([(e["event"], e.get("code")) for e in data["events"]
                          if e["event"] in ("ordered", "sent", "received")],
                         [("ordered", None), ("sent", "850"),
                          ("received", "855"), ("sent", "997"),
                          ("received", "856"), ("sent", "997"),
                          ("received", "810"), ("sent", "997")])

    def test_and_each_is_acknowledged(self):
        self.send(supplier_sends(schema.DESPATCH, "PO-BUY"))
        ack = parse(self.sent(schema.ACKNOWLEDGMENT))
        self.assertEqual(ack.groups[0].messages[0].find("AK5").get(1), "A")

    def test_in_edifact_too(self):
        self.post("/_mock/partners", {"id": "NORDIC", "dialect": "EDIFACT",
                                      "version": "D:96A:UN", "role": "supplier",
                                      "qualifier": "14"})
        self.placed(partner="NORDIC", po_number="PO-EU-2")
        for kind in (schema.RESPONSE, schema.DESPATCH, schema.INVOICE):
            summary = self.send(supplier_sends(kind, "PO-EU-2", "NORDIC", "EDIFACT"),
                                headers={"Content-Type": "application/edifact"})
            self.assertEqual([f["order"] for f in summary["filed"]], ["PO-EU-2"],
                             summary["transactionSets"])

    def test_one_for_an_order_never_placed_is_rejected(self):
        summary = self.send(supplier_sends(schema.RESPONSE, "PO-NEVER"))
        self.assertFalse(summary["accepted"])
        self.assertEqual(summary["filed"], [])
        self.assertIn("purchase order PO-NEVER was never placed with NORTHWIND",
                      " ".join(summary["transactionSets"][0]["findings"]))
        message = parse(self.sent(schema.ACKNOWLEDGMENT)).groups[0].messages[0]
        ak4 = message.find("AK4")
        self.assertEqual((message.find("AK3").get(1), ak4.get(1), ak4.get(3),
                          ak4.get(4)), ("BAK", "3", "7", "PO-NEVER"))
        self.assertEqual(message.find("AK5").get(1), "R")

    def test_in_edifact_the_contrl_points_at_rff(self):
        self.post("/_mock/partners", {"id": "NORDIC", "dialect": "EDIFACT",
                                      "version": "D:96A:UN", "role": "supplier",
                                      "qualifier": "14"})
        summary = self.send(supplier_sends(schema.INVOICE, "PO-NEVER", "NORDIC",
                                           "EDIFACT"),
                            headers={"Content-Type": "application/edifact"})
        self.assertFalse(summary["accepted"])
        contrl = next(parse(self.sent(schema.ACKNOWLEDGMENT, "NORDIC")).messages())[1]
        ucd = contrl.find("UCD")
        self.assertEqual((ucd.get(1), ucd.comp(2, 1), ucd.comp(2, 2)), ("12", "1", "2"))

    def test_one_for_another_suppliers_order_is_rejected(self):
        self.post("/_mock/partners", {"id": "OTHERSUP", "role": "supplier"})
        summary = self.send(supplier_sends(schema.INVOICE, "PO-BUY", "OTHERSUP"))
        self.assertFalse(summary["accepted"])
        self.assertIn("never placed with OTHERSUP",
                      " ".join(summary["transactionSets"][0]["findings"]))

    def test_one_for_an_order_it_received_is_rejected(self):
        # A customer that became a supplier: the orders it sent are still
        # ones the mock received, and nothing it confirms can be filed there.
        self.send(x12_order("PO-WAS-SOLD"))
        self.flip_role(ACME, "supplier")
        summary = self.send(supplier_sends(schema.RESPONSE, "PO-WAS-SOLD", ACME))
        self.assertFalse(summary["accepted"])
        self.assertEqual(summary["filed"], [])

    def test_a_suppliers_850_is_refused_by_the_role(self):
        summary = self.send(x12_order("PO-FROM-SUP", sender=NORTHWIND))
        self.assertEqual(summary["orders"], [])
        self.assertIn("the buyer sends",
                      " ".join(summary["transactionSets"][0]["findings"]))

    def test_a_customer_is_unchanged(self):
        # ACME's 850 is answered as it always was; its 855 is still refused.
        summary = self.send(x12_order("PO-STILL-SELLING"))
        self.assertEqual(summary["orders"], ["PO-STILL-SELLING"])
        self.assertEqual(summary["filed"], [])
        summary = self.send(supplier_sends(schema.RESPONSE, "PO-BUY", ACME))
        self.assertIn("the seller sends",
                      " ".join(summary["transactionSets"][0]["findings"]))


class ABehaviourOnASupplier(BuyingCase):
    """The behaviours that describe how the mock answers, on the buying side.

    `accept` is every other test here. Each of these is the seller's own
    behaviour, and on a supplier it means the same thing about what the mock
    sends back - the 997 for an 855, and the 850 and 860 themselves.
    """

    def setUp(self):
        super().setUp()
        self.placed(po_number="PO-BHV")

    def acks(self):
        return self.mailbox(NORTHWIND, schema.ACKNOWLEDGMENT)

    def test_no_ack_files_the_855_and_never_acknowledges_it(self):
        self.behaviour(NORTHWIND, "no-ack")
        summary = self.send(supplier_sends(schema.RESPONSE, "PO-BHV"))
        self.assertEqual([f["order"] for f in summary["filed"]], ["PO-BHV"])
        self.post("/_mock/advance?all")
        self.assertEqual(self.acks(), [])

    def test_late_acknowledges_an_hour_later(self):
        self.behaviour(NORTHWIND, "late")
        self.send(supplier_sends(schema.RESPONSE, "PO-BHV"))
        self.assertEqual(self.acks(), [])
        self.post("/_mock/advance?seconds=3601")
        self.assertEqual(len(self.acks()), 1)

    def test_reject_ack_rejects_a_clean_855_and_files_nothing(self):
        self.behaviour(NORTHWIND, "reject-ack")
        summary = self.send(supplier_sends(schema.RESPONSE, "PO-BHV"))
        self.assertEqual(summary["filed"], [])
        self.assertEqual(summary["transactionSets"][0]["findings"], [])
        ak5 = parse(self.acks()[-1]["payload"]).groups[0].messages[0].find("AK5")
        self.assertEqual(ak5.get(1), "R")

    def test_strict_rejects_what_accept_lets_through(self):
        # PID05 is 80 characters at most: a finding, but not a fatal one.
        sloppy = lambda: supplier_sends(schema.RESPONSE, "PO-BHV", description="W" * 90)
        summary = self.send(sloppy())
        self.assertTrue(summary["transactionSets"][0]["findings"])
        self.assertEqual([f["order"] for f in summary["filed"]], ["PO-BHV"])
        self.behaviour(NORTHWIND, "strict")
        summary = self.send(sloppy())
        self.assertEqual(summary["filed"], [])
        ak5 = parse(self.acks()[-1]["payload"]).groups[0].messages[0].find("AK5")
        self.assertEqual(ak5.get(1), "R")

    def test_corrupt_miscounts_the_850_and_860_the_mock_sends(self):
        self.behaviour(NORTHWIND, "corrupt")
        self.placed(po_number="PO-BHV-2")
        self.post("/_mock/purchase/PO-BHV-2/change", {"cancel": True})
        for kind in (schema.ORDER, schema.CHANGE):
            message = parse(self.sent(kind)).groups[0].messages[0]
            self.assertEqual(int(message.segments[-1].get(1)),
                             len(message.segments) + 1, kind)
            self.assertFalse(validate.validate(parse(self.sent(kind))).clean)
        # Its 997s stay intact: a corrupt acknowledgment tests something else.
        self.send(supplier_sends(schema.RESPONSE, "PO-BHV-2"))
        self.assertTrue(validate.validate(parse(self.acks()[-1]["payload"])).clean)


class ASupplierTurnedCustomer(BuyingCase):
    """The review's reproduction on #133: a role changed under a placed order.

    #134's ownership check keeps other partners off an order the mock placed,
    because the order is the supplier's. It says nothing when the supplier
    itself becomes a customer. What the mock bought from it is still not an
    order it sold to it, and nothing on the seller's side may touch it.

    #146 closed the way in: the PATCH that flips the role is now refused
    while the partner holds a live order, so this state can no longer be
    made through the control plane. It can still be *read*, out of a database
    a mock without that refusal wrote, and the mock opens those in place. So
    the flip below is done in the database rather than over HTTP - which is
    the only way it now arises, and exactly the shape it arrives in. These
    tests are the second line, and the one that has to hold for a file that
    is already on disk.
    """

    def setUp(self):
        super().setUp()
        self.placed(po_number="PO-FLIP")
        self.before = self.order("PO-FLIP")
        self.flip_role(NORTHWIND, "customer")

    def unchanged(self):
        after = self.order("PO-FLIP")
        self.assertEqual((after["direction"], after["status"], after["total"]),
                         ("placed", "placed", self.before["total"]))
        self.assertEqual([row["quantity"] for row in after["lines"]],
                         [row["quantity"] for row in self.before["lines"]])
        codes = [row["code"] for row in self.mailbox(NORTHWIND)]
        self.assertNotIn("865", codes)
        self.assertNotIn("855", codes)

    def test_its_860_cannot_cancel_it(self):
        summary = self.send(x12_change("PO-FLIP", lines=[("1", "CA", 0, "0")],
                                       purpose="01", sender=NORTHWIND))
        self.assertEqual(summary["changed"], [])
        self.assertEqual([r["reason"] for r in summary["refusals"]],
                         [documents.NOT_FOUND])
        self.unchanged()

    def test_nor_change_it(self):
        self.send(x12_change("PO-FLIP", lines=[("1", "QD", 5, "12.50")],
                             sender=NORTHWIND))
        self.unchanged()

    def test_nor_restate_it(self):
        self.send(x12_order("PO-FLIP", sender=NORTHWIND, purpose="04"))
        self.unchanged()

    def test_nor_replace_it_with_an_order_of_its_own(self):
        summary = self.send(x12_order("PO-FLIP", sender=NORTHWIND))
        self.assertEqual(summary["orders"], [])
        self.assertEqual([r["reason"] for r in summary["refusals"]],
                         [documents.NUMBER_IN_USE])
        self.assertEqual(self.order("PO-FLIP")["direction"], "placed")
        self.assertEqual(self.order("PO-FLIP")["lines"][0]["quantity"], "100")


if __name__ == "__main__":
    unittest.main(verbosity=2)
