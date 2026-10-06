"""A mock that posts nothing until it is asked: `--hold-delivery` and `step`.

Two mocks wired to each other finish a whole rally inside one `settle()`: the
seller makes its promises on receipt of the 850, and the buyer answers the
855 with a 997, and both happen before anything outside can look. The two
events sit in different mocks, so nothing orders them afterwards either.

A held mock keeps what it would have posted, in the order it would have
posted it, and `step` sends the head of that queue and says what it was. A
read between two steps is a read of something standing still (#271).
"""
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi.__main__ import build_parser, config_from_args
from mockedi.testing import Mock, MockError

from support import ACME, as2_headers, x12_order


class Listener(BaseHTTPRequestHandler):
    """A partner's AS2 listener, which keeps what it is sent."""
    received = []
    refuse_first = 0
    delay = 0.0

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace")
        Listener.received.append({"path": self.path, "body": body})
        if Listener.delay:
            time.sleep(Listener.delay)
        status = 200
        if Listener.refuse_first > 0:
            Listener.refuse_first -= 1
            status = 503
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


def codes(received):
    """The set code of each document a listener was posted, in order."""
    out = []
    for item in received:
        for line in item["body"].replace("~", "\n").splitlines():
            if line.startswith("ST*"):
                out.append(line.split("*")[1])
    return out


class ListenerCase(unittest.TestCase):
    """A mock whose partner ACME has somewhere to receive documents."""
    hold = True

    @classmethod
    def setUpClass(cls):
        cls.listener = HTTPServer(("127.0.0.1", 0), Listener)
        cls.url = "http://127.0.0.1:%d" % cls.listener.server_address[1]
        threading.Thread(target=cls.listener.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()

    def setUp(self):
        Listener.received = []
        Listener.refuse_first = 0
        Listener.delay = 0.0
        self.mock = Mock.start(hold_delivery=self.hold, deliver_timeout=3.0)
        self.addCleanup(self.mock.close)
        self.mock.partner(ACME, as2_url=self.url + "/as2")

    def ready(self):
        return [row for row in self.mock.outbox() if row["status"] == "ready"]


class AHeldMockPostsNothing(ListenerCase):

    def test_what_it_releases_stays_ready_and_the_partner_hears_nothing(self):
        self.mock.send(x12_order("HOLD-QUIET"))
        self.assertGreaterEqual(len(self.ready()), 4)
        self.assertEqual(Listener.received, [])

    def test_health_says_it_is_held(self):
        self.assertTrue(self.mock.expect("GET", "/_mock/health")["deliveryHeld"])
        self.assertTrue(self.mock.held)

    def test_settle_raises_at_once_and_names_the_hold(self):
        self.mock.send(x12_order("HOLD-SETTLE"))
        began = time.time()
        with self.assertRaises(MockError) as caught:
            self.mock.settle(timeout=30)
        self.assertLess(time.time() - began, 5)
        self.assertIn("hold_delivery", str(caught.exception))
        self.assertIn("step()", str(caught.exception))

    def test_exchange_raises_if_any_of_the_mocks_is_held(self):
        other = Mock.start()
        self.addCleanup(other.close)
        for first, second in ((self.mock, other), (other, self.mock)):
            with self.assertRaises(MockError) as caught:
                first.exchange(second)
            self.assertIn("exchange()", str(caught.exception))
            self.assertIn(self.mock.base, str(caught.exception))


class OneStepIsOneDocument(ListenerCase):

    def test_a_step_sends_the_oldest_and_names_it(self):
        self.mock.send(x12_order("HOLD-ONE"))
        oldest = min(self.ready(), key=lambda row: row["id"])
        sent = self.mock.step()
        self.assertEqual(len(Listener.received), 1)
        self.assertEqual(
            {name: sent[name] for name in ("type", "id", "partner", "status")},
            {"type": "document", "id": oldest["id"], "partner": ACME,
             "status": "delivered"})
        self.assertEqual(sent["control"], oldest["control"])
        self.assertEqual(sent["code"], oldest["code"])
        self.assertEqual(sent["to"], self.url + "/as2")
        self.assertIn("*%s*" % sent["control"].zfill(9), Listener.received[0]["body"])

    def test_the_next_step_sends_the_next_and_nothing_moves_between(self):
        self.mock.send(x12_order("HOLD-TWO"))
        waiting = sorted(row["id"] for row in self.ready())
        first = self.mock.step()
        time.sleep(0.3)                       # nothing else is on its way
        self.assertEqual(len(Listener.received), 1)
        self.assertEqual(sorted(row["id"] for row in self.ready()), waiting[1:])
        second = self.mock.step()
        self.assertEqual([first["id"], second["id"]], waiting[:2])
        self.assertEqual(len(Listener.received), 2)

    def test_the_answer_counts_what_is_still_waiting(self):
        self.mock.send(x12_order("HOLD-COUNT"))
        held = len(self.ready())
        answer = self.mock.expect("POST", "/_mock/deliver")
        self.assertEqual(answer["waiting"], held - 1)
        self.assertEqual(answer["sent"]["type"], "document")

    def test_stepping_until_there_is_nothing_sends_everything_once(self):
        self.mock.send(x12_order("HOLD-LOOP"))
        held = len(self.ready())
        steps = 0
        while self.mock.step():
            steps += 1
            self.assertLess(steps, 20)
        self.assertEqual(steps, held)
        self.assertEqual(len(Listener.received), held)
        self.assertEqual(self.ready(), [])

    def test_with_nothing_to_send_it_says_so_and_does_not_wait(self):
        began = time.time()
        answer = self.mock.expect("POST", "/_mock/deliver")
        self.assertLess(time.time() - began, 2)
        self.assertEqual(answer, {"sent": None, "waiting": 0})
        self.assertIsNone(self.mock.step())

    def test_all_sends_everything_held_in_order(self):
        self.mock.send(x12_order("HOLD-ALL"))
        waiting = sorted(row["id"] for row in self.ready())
        sent = self.mock.step(everything=True)
        self.assertEqual([item["id"] for item in sent], waiting)
        self.assertEqual(len(Listener.received), len(waiting))
        self.assertEqual(self.mock.step(everything=True), [])

    def test_get_is_not_how_you_deliver(self):
        status, headers, body = self.mock.request("GET", "/_mock/deliver", raw=True)
        self.assertEqual((status, body), (405, b"POST to deliver what is held\n"))
        self.assertEqual(headers["Allow"], "POST")


class TheOrderIsTheCouriersOwn(ListenerCase):
    """ "Next" is what an unheld mock would have posted next."""

    def test_stepping_delivers_in_the_order_an_unheld_mock_does(self):
        self.mock.send(x12_order("HOLD-ORDER"))
        self.mock.step(everything=True)
        stepped = codes(Listener.received)

        Listener.received = []
        free = Mock.start(deliver_timeout=3.0)
        self.addCleanup(free.close)
        free.partner(ACME, as2_url=self.url + "/as2")
        free.send(x12_order("HOLD-ORDER"))
        free.settle()
        self.assertEqual(stepped, codes(Listener.received))
        self.assertEqual(stepped, ["997", "855", "856", "810"])


class WhatIsNotAStep(ListenerCase):

    def test_a_mailbox_partner_is_passed_over(self):
        self.mock.partner(ACME, as2_url="")
        self.mock.send(x12_order("HOLD-BOX"))
        self.assertTrue(self.ready())
        self.assertEqual(self.mock.expect("POST", "/_mock/deliver"),
                         {"sent": None, "waiting": 0})
        self.assertEqual(Listener.received, [])
        self.assertEqual(len(self.mock.mailbox(ACME, leave=False)),
                         len(self.mock.outbox()))

    def test_a_document_collected_meanwhile_is_passed_over(self):
        self.mock.send(x12_order("HOLD-TAKEN"))
        waiting = sorted(row["id"] for row in self.ready())
        self.mock.mailbox(ACME, kind="acknowledgment", leave=False)
        sent = self.mock.step()
        self.assertEqual(sent["id"], waiting[1])
        self.assertEqual(len(Listener.received), 1)


class AFailureIsAStepToo(ListenerCase):

    def test_a_refused_delivery_says_failed(self):
        Listener.refuse_first = 1
        self.mock.send(x12_order("HOLD-FAIL"))
        sent = self.mock.step()
        self.assertEqual(sent["status"], "failed")
        self.assertIn("503", sent["note"])

    def test_a_retry_is_held_like_anything_else(self):
        Listener.refuse_first = 1
        self.mock.send(x12_order("HOLD-RETRY"))
        failed = self.mock.step()
        self.mock.step(everything=True)
        posts = len(Listener.received)
        answer = self.mock.expect("POST", "/_mock/outbox/%d/retry" % failed["id"])
        self.assertEqual(answer["retried"], [failed["id"]])
        time.sleep(0.3)
        self.assertEqual(len(Listener.received), posts)       # not until asked
        again = self.mock.step()
        self.assertEqual((again["id"], again["status"]), (failed["id"], "delivered"))
        self.assertEqual(len(Listener.received), posts + 1)

    def test_work_that_falls_due_is_released_and_held(self):
        slow = Mock.start(hold_delivery=True, invoice_delay_ms=3600 * 1000)
        self.addCleanup(slow.close)
        slow.partner(ACME, as2_url=self.url + "/as2")
        slow.send(x12_order("HOLD-DUE"))
        slow.step(everything=True)
        posts = len(Listener.received)
        slow.advance(everything=True)
        time.sleep(0.3)
        self.assertEqual(len(Listener.received), posts)
        self.assertEqual(slow.step()["code"], "810")


class AnAsynchronousReceiptIsItsOwnStep(ListenerCase):

    def test_it_waits_and_is_stepped_apart_from_the_documents(self):
        response = self.mock.as2(
            x12_order("HOLD-MDN"),
            as2_headers(async_url=self.url + "/mdn"))
        self.assertEqual(response.status, 202)
        pending = [row for row in self.mock.expect("GET", "/_mock/mdns")
                   if row["direction"] == "out"]
        self.assertEqual([row["status"] for row in pending], ["pending"])
        self.assertEqual(Listener.received, [])

        # The receipt was queued when the order was answered, which is after
        # the order's documents were released: it goes last.
        sent = self.mock.step(everything=True)
        self.assertEqual([item["type"] for item in sent][-1], "mdn")
        self.assertEqual(sent[-1]["status"], "sent")
        self.assertEqual(sent[-1]["to"], self.url + "/mdn")
        self.assertEqual(Listener.received[-1]["path"], "/mdn")

    def test_a_read_between_shows_delivered_and_not_yet_receipted(self):
        self.mock.as2(x12_order("HOLD-BETWEEN"),
                      as2_headers(async_url=self.url + "/mdn"))
        sent = None
        while self.mock.expect("GET", "/_mock/health")["queued"]:
            sent = self.mock.step()
        self.assertEqual(sent["type"], "document")
        receipt = [row for row in self.mock.expect("GET", "/_mock/mdns")
                   if row["direction"] == "out"][0]
        self.assertEqual(receipt["status"], "pending")
        self.assertEqual(self.mock.step()["type"], "mdn")


class WhileAStepIsWaiting(ListenerCase):
    """The mock's lock is let go for the wait, so other requests get in."""

    def in_the_background(self, work):
        outcome = {}

        def run():
            try:
                outcome["answer"] = work()
            except Exception as error:      # reported by the test, not lost
                outcome["error"] = error
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, outcome

    def test_a_reset_in_the_middle_is_answered_and_says_the_row_is_gone(self):
        self.mock.send(x12_order("HOLD-RESET"))
        Listener.delay = 1.0
        stepper = Mock(self.mock.base)
        self.addCleanup(stepper.disconnect)
        thread, outcome = self.in_the_background(
            lambda: stepper.post("/_mock/deliver"))
        while not Listener.received:
            time.sleep(0.02)
        self.mock.reset()
        thread.join(10)
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["answer"].status, 200, outcome["answer"].body)
        sent = outcome["answer"].body["sent"]
        self.assertEqual((sent["type"], sent["status"]), ("document", "gone"))
        self.assertEqual(outcome["answer"].body["waiting"], 0)

    def test_what_moved_is_gone_for_a_row_that_no_longer_exists(self):
        courier = self.mock.server.mock.courier
        with self.mock.server.mock.lock:
            self.assertEqual(courier._moved("document", 999999)["status"], "gone")
            self.assertEqual(courier._moved("mdn", 999999)["status"], "gone")

    def test_two_steps_at_once_are_one_document_each(self):
        self.mock.send(x12_order("HOLD-TWICE"))
        waiting = sorted(row["id"] for row in self.ready())
        Listener.delay = 0.4
        seen = []

        def step():
            client = Mock(self.mock.base)
            try:
                sent = client.step()
                seen.append((sent["id"], len(Listener.received)))
            finally:
                client.disconnect()
        threads = [threading.Thread(target=step, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(sent for sent, _posts in seen), waiting[:2])
        # Whichever step finished first finished before the other's document
        # had been posted: one step, one document on the wire.
        self.assertEqual(sorted(posts for _sent, posts in seen), [1, 2])


class AMockSomebodyElseStarted(ListenerCase):

    def test_it_is_asked_once_whether_it_is_held(self):
        client = Mock(self.mock.base)
        self.addCleanup(client.disconnect)
        self.assertTrue(client.held)
        before = len(self.mock.expect("GET", "/_mock/requests?limit=200"))
        self.assertTrue(client.held)
        with self.assertRaises(MockError):
            client.settle()
        after = self.mock.expect("GET", "/_mock/requests?limit=200")
        self.assertEqual([row["path"] for row in after[:len(after) - before]
                          if "health" in row["path"]], [])


class AMockThatIsNotHeld(ListenerCase):
    hold = False

    def test_deliver_is_refused_and_says_how_to_hold(self):
        response = self.mock.post("/_mock/deliver")
        self.assertEqual(response.status, 409)
        self.assertIn("--hold-delivery", response.body["error"])

    def test_it_delivers_as_it_always_did(self):
        self.assertFalse(self.mock.held)
        self.assertFalse(self.mock.expect("GET", "/_mock/health")["deliveryHeld"])
        self.mock.send(x12_order("HOLD-NOT"))
        self.mock.settle()
        self.assertEqual(codes(Listener.received), ["997", "855", "856", "810"])


class TwoHeldMocksSteppedInTurn(unittest.TestCase):
    """The case it was asked for: what one settle() used to run together."""

    def setUp(self):
        self.seller = Mock.start(as2_id="SELLCO", hold_delivery=True)
        self.buyer = Mock.start(as2_id="BUYCO", hold_delivery=True)
        self.addCleanup(self.seller.close)
        self.addCleanup(self.buyer.close)
        self.seller.expect("POST", "/_mock/partners", {
            "id": "BUYCO", "name": "Buy Co", "as2_url": self.buyer.base + "/edi"},
            status=201)
        self.buyer.expect("POST", "/_mock/partners", {
            "id": "SELLCO", "name": "Sell Co", "role": "supplier",
            "as2_url": self.seller.base + "/edi"}, status=201)

    def received(self, mock):
        return [row["code"] for row in reversed(
            mock.documents(direction="in", limit=50))]

    def test_the_sellers_promises_and_the_buyers_997_can_be_read_apart(self):
        self.buyer.expect("POST", "/_mock/purchase", {
            "partner": "SELLCO", "po_number": "HOLD-PAIR",
            "lines": [{"sku": "WIDGET-001", "quantity": "10", "uom": "EA",
                       "price": "12.50"}]}, status=201)
        self.assertEqual(self.received(self.seller), [])

        order = self.buyer.step()
        self.assertEqual((order["code"], order["status"]), ("850", "delivered"))
        # The seller has the order and has answered nobody yet.
        self.assertEqual(self.received(self.seller), ["850"])
        self.assertEqual(self.received(self.buyer), [])
        promised = self.seller.scheduled()
        self.assertTrue(promised)

        sent = []
        while "855" not in sent:
            sent.append(self.seller.step()["code"])
        self.assertEqual(sent, ["997", "855"])
        # The buyer has the 855 and its answer to it is written and held: the
        # seller has made its promises and has not been acknowledged.
        self.assertEqual(self.received(self.buyer), ["997", "855"])
        self.assertEqual(self.received(self.seller), ["850"])

        answer = self.buyer.step()
        self.assertEqual(answer["code"], "997")
        self.assertEqual(self.received(self.seller), ["850", "997"])

    def test_stepping_each_in_turn_finishes_the_flow(self):
        self.buyer.expect("POST", "/_mock/purchase", {
            "partner": "SELLCO", "po_number": "HOLD-RALLY",
            "lines": [{"sku": "WIDGET-001", "quantity": "10", "uom": "EA",
                       "price": "12.50"}]}, status=201)
        for _ in range(40):
            moved = [mock.step() for mock in (self.buyer, self.seller)]
            if not any(moved):
                break
        else:
            self.fail("the two mocks were still stepping after 40 rounds")
        self.assertEqual(sorted(code for code in self.received(self.buyer)
                                if code != "997"), ["810", "855", "856"])


class AMockThatPostsToItself(unittest.TestCase):

    def test_a_step_does_not_wait_for_an_answer_only_it_can_give(self):
        mock = Mock.start(hold_delivery=True, deliver_timeout=5.0)
        self.addCleanup(mock.close)
        mock.partner(ACME, as2_url=mock.base + "/edi")
        mock.send(x12_order("HOLD-SELF"))
        began = time.time()
        sent = mock.step()
        self.assertLess(time.time() - began, 4)
        self.assertEqual(sent["type"], "document")
        self.assertNotEqual(sent["status"], "ready")


class TheFlag(unittest.TestCase):

    def test_hold_delivery_is_off_unless_asked_for(self):
        parser = build_parser()
        self.assertFalse(config_from_args(parser.parse_args([])).hold_delivery)
        self.assertTrue(config_from_args(
            parser.parse_args(["--hold-delivery"])).hold_delivery)


if __name__ == "__main__":
    unittest.main()
