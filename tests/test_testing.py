"""The client the package ships, tested as a thing people will rely on.

Most of it is exercised by every other file in this suite, because
`tests/support.py` is written on top of it - which is the only test of a
"use this instead of writing your own" module that means anything. What is
here is the part that is the client's own behaviour rather than the mock's:
how it starts and stops a mock, what it does with a refusal, and the small
conveniences that exist so a test does not have to split a payload on `~`.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi.testing import Document, Mock, MockError, _query

from support import ACME, EURODIS, edifact_order, x12_order


class StartingAndStopping(unittest.TestCase):
    def test_start_gives_a_mock_on_its_own_port(self):
        with Mock.start() as mock:
            self.assertTrue(mock.base.startswith("http://127.0.0.1:"))
            self.assertEqual(mock.get("/_mock/health").status, 200)

    def test_two_mocks_do_not_collide(self):
        with Mock.start() as first, Mock.start() as second:
            self.assertNotEqual(first.base, second.base)
            self.assertEqual(second.get("/_mock/health").status, 200)

    def test_it_stops_when_the_block_ends(self):
        with Mock.start() as mock:
            base = mock.base
        stopped = Mock(base, timeout=2.0)
        with self.assertRaises(Exception):
            stopped.get("/_mock/health")

    def test_config_keywords_reach_the_server(self):
        with Mock.start(invoice_delay_ms=3600 * 1000) as mock:
            self.assertEqual(mock.server.mock.config.invoice_delay_ms, 3600000)

    def test_a_client_that_started_nothing_has_no_server(self):
        self.assertIsNone(Mock("http://127.0.0.1:1").server)

    def test_closing_twice_is_harmless(self):
        mock = Mock.start()
        mock.close()
        mock.close()


class WhatARefusalDoes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = Mock.start()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def test_an_unexpected_status_raises_with_what_the_mock_said(self):
        with self.assertRaises(MockError) as caught:
            self.mock.order("NO-SUCH-ORDER")
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("NO-SUCH-ORDER", str(caught.exception))

    def test_the_body_is_kept_for_a_caller_that_wants_it(self):
        with self.assertRaises(MockError) as caught:
            self.mock.expect("PATCH", "/_mock/partners/" + ACME,
                             {"nonsense": "x"})
        self.assertIn("nonsense", str(caught.exception))
        self.assertIsInstance(caught.exception.body, dict)

    def test_the_plain_verbs_return_the_status_instead(self):
        # A test that is *about* the refusal should not have to catch.
        self.assertEqual(self.mock.get("/_mock/orders/NOPE").status, 404)

    def test_a_response_reads_by_name_and_unpacks_as_a_tuple(self):
        reply = self.mock.get("/_mock/health")
        self.assertEqual(reply.status, 200)
        status, headers, body = reply
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertIn("Content-Type", headers)


class SendingWithoutSayingTheDialect(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = Mock.start()

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def setUp(self):
        self.mock.reset()

    def test_an_x12_payload_is_sent_as_x12(self):
        self.assertEqual(self.mock.send(x12_order("CL-X"))["dialect"], "X12")

    def test_an_edifact_payload_is_sent_as_edifact(self):
        self.assertEqual(self.mock.send(edifact_order("CL-E"))["dialect"],
                         "EDIFACT")

    def test_and_the_dialect_can_still_be_named(self):
        summary = self.mock.send(x12_order("CL-N"), dialect="X12")
        self.assertTrue(summary["accepted"])

    def test_findings_come_back_as_prose_without_changing_anything(self):
        lines = self.mock.findings(x12_order("CL-DRY"))
        self.assertEqual(lines, ["850/0001: accepted"])
        self.assertEqual(self.mock.get("/_mock/orders/CL-DRY").status, 404)


class ReadingADocument(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mock = Mock.start()
        cls.mock.send(x12_order("CL-DOC"))

    @classmethod
    def tearDownClass(cls):
        cls.mock.close()

    def test_find_reaches_through_the_groups(self):
        response = self.mock.document(partner=ACME, kind="response")
        self.assertEqual(response.find("ACK").get(1), "IA")

    def test_all_gives_every_one_in_order(self):
        response = self.mock.document(partner=ACME, kind="response")
        self.assertEqual(len(response.all("PO1")), 2)

    def test_a_tag_that_is_not_there_is_none(self):
        response = self.mock.document(partner=ACME, kind="response")
        self.assertIsNone(response.find("ZZZ"))

    def test_the_interchange_underneath_is_still_reachable(self):
        response = self.mock.document(partner=ACME, kind="response")
        self.assertEqual(response.groups[0].messages[0].code, "855")
        self.assertEqual(response.codes(), ["855"])

    def test_the_payload_is_kept(self):
        response = self.mock.document(partner=ACME, kind="response")
        self.assertIn("ISA*", response.payload)

    def test_asking_for_one_when_there_are_none_says_so(self):
        with self.assertRaises(MockError) as caught:
            self.mock.document(partner=ACME, kind="nonesuch")
        self.assertIn("expected one", str(caught.exception))

    def test_a_document_knows_its_dialect(self):
        self.assertEqual(Document(x12_order("CL-D2")).dialect, "X12")
        self.assertEqual(Document(edifact_order("CL-D3")).dialect, "EDIFACT")


class WaitingForDelivery(unittest.TestCase):
    def test_a_mailbox_partner_is_not_waited_for(self):
        # Nobody undertook to deliver to a partner with no as2_url, so its
        # documents stay ready for ever and settling must not block on them.
        with Mock.start() as mock:
            mock.send(x12_order("CL-WAIT"))
            rows = mock.settle(timeout=5.0)
            self.assertTrue(any(row["status"] == "ready" for row in rows))



class PushingAConversationThrough(unittest.TestCase):
    """`exchange`, which exists because every author discovered it the hard way.

    Two mocks are a rally, and two kinds of work are not `ready` however often
    you settle: a `pending` row in the outbox, and an undone promise in the
    schedule. A test should not have to know which, or how many alternating
    calls it takes.
    """

    def test_an_idle_mock_converges_at_once(self):
        with Mock.start() as mock:
            mock.exchange()

    def test_a_mailbox_partner_does_not_hang_it(self):
        # Those documents stay ready until something collects them, which is
        # not a conversation waiting to finish.
        with Mock.start() as mock:
            mock.send(x12_order("EX-MAIL"))
            mock.exchange()
            self.assertTrue(mock.outbox())

    def test_it_keeps_a_promise_the_clock_was_hiding(self):
        # The case no amount of settling reaches: the invoice does not exist
        # yet, so it cannot be pending - it is a row in the schedule.
        with Mock.start(invoice_delay_ms=3600 * 1000) as mock:
            mock.send(x12_order("EX-PROMISE"))
            self.assertEqual([row["kind"] for row
                              in mock.scheduled(pending_only=True)],
                             ["invoice"])
            mock.exchange()
            self.assertEqual(mock.scheduled(pending_only=True), [])
            self.assertTrue([row for row in mock.outbox()
                             if row["code"] == "810"])

    def test_advance_false_leaves_the_clock_where_it_was(self):
        # For a test about *when* rather than about what.
        with Mock.start(invoice_delay_ms=3600 * 1000) as mock:
            mock.send(x12_order("EX-WHEN"))
            mock.exchange(advance=False)
            self.assertEqual([row["kind"] for row
                              in mock.scheduled(pending_only=True)],
                             ["invoice"])

    def test_it_reports_what_is_still_held_rather_than_hanging(self):
        # A rally with no end is a bug in the test or in the mock. Zero passes
        # is the cheapest way to reach that branch without writing one.
        with Mock.start() as mock:
            mock.send(x12_order("EX-STUCK"))
            with self.assertRaises(MockError) as caught:
                mock.exchange(passes=0)
            self.assertIn("still talking", str(caught.exception))
            self.assertIn("holding", caught.exception.body)

    def test_the_schedule_is_readable_without_a_raw_get(self):
        with Mock.start(invoice_delay_ms=3600 * 1000) as mock:
            mock.send(x12_order("EX-SCHED"))
            every = mock.scheduled()
            open_only = mock.scheduled(pending_only=True)
            self.assertTrue(every)
            self.assertLess(len(open_only), len(every))
            self.assertTrue(all(not row["done_at"] for row in open_only))


class TheQueryBuilder(unittest.TestCase):
    """This control plane spells a flag as a bare word, not `?flag=true`."""

    def test_a_true_value_is_the_name_alone(self):
        self.assertEqual(_query(leave=True), "?leave")

    def test_a_value_is_a_pair(self):
        self.assertEqual(_query(partner="ACME"), "?partner=ACME")

    def test_nothing_given_is_nothing_asked(self):
        self.assertEqual(_query(partner="", kind=None, leave=False), "")

    def test_values_are_escaped(self):
        self.assertIn("PO%2F1", _query(partner="PO/1"))

    def test_an_older_than_of_zero_is_still_asked_for(self):
        # 0 is a meaningful cutoff - "everything" - and must not be dropped
        # the way an empty string is.
        self.assertEqual(_query(**{"older-than": 0}), "?older-than=0")


class TheClientKeepsItsConnection(unittest.TestCase):
    """One socket for a client's requests, not one for each (#220).

    A socket closed after every request sits in `TIME_WAIT` for the best part
    of a minute. The suite makes several thousand requests, and two runs in a
    row used every ephemeral port the host had; what failed then was
    everything, with nothing pointing here.
    """

    def test_a_few_hundred_requests_open_one_connection(self):
        with Mock.start() as mock:
            for number in range(300):
                self.assertEqual(mock.get("/_mock/health").status, 200)
            mock.post("/edi", x12_order("PO-KEPT-1"),
                      headers={"Content-Type": "application/edi-x12"})
            mock.patch("/_mock/partners/ACME", {"behaviour": "accept"})
            self.assertEqual(mock.get("/_mock/nothing").status, 404)
            self.assertEqual(mock.request("HEAD", "/_mock/health").status, 200)
            self.assertEqual(mock.connections_opened, 1)

    def test_a_connection_the_server_let_go_of_is_reopened_unseen(self):
        # The server drops a connection that has been idle for longer than
        # --request-timeout. The next request finds it gone, and goes again.
        with Mock.start(request_timeout=0.2) as mock:
            self.assertEqual(mock.get("/_mock/health").status, 200)
            import time
            time.sleep(0.6)
            reply = mock.post("/edi", x12_order("PO-KEPT-2"),
                              headers={"Content-Type": "application/edi-x12"})
            self.assertEqual(reply.status, 200)
            self.assertEqual(reply.body["orders"], ["PO-KEPT-2"])
            self.assertEqual(mock.connections_opened, 2)
            # Sent once and acted on once, not once per attempt.
            self.assertEqual(len(mock.get("/_mock/orders").body), 3)

    def test_a_refusal_that_closes_the_connection_is_followed_by_an_answer(self):
        # A body over --max-body is refused with `Connection: close`.
        with Mock.start(max_body_bytes=64) as mock:
            refused = mock.post("/edi", "X" * 200)
            self.assertEqual(refused.status, 413)
            self.assertEqual(mock.get("/_mock/health").status, 200)
            self.assertEqual(mock.connections_opened, 2)

    def test_each_thread_keeps_its_own(self):
        import threading
        with Mock.start() as mock:
            mock.get("/_mock/health")
            answers = []

            def elsewhere():
                for _ in range(20):
                    answers.append(mock.get("/_mock/health").status)

            thread = threading.Thread(target=elsewhere)
            thread.start()
            for _ in range(20):
                answers.append(mock.get("/_mock/health").status)
            thread.join()
            self.assertEqual(answers, [200] * 40)
            self.assertEqual(mock.connections_opened, 2)

    def test_a_mock_that_is_not_there_raises_and_is_not_asked_twice(self):
        mock = Mock("http://127.0.0.1:1", timeout=2.0)
        with self.assertRaises(OSError):
            mock.get("/_mock/health")
        self.assertEqual(mock.connections_opened, 0)

    def test_closing_leaves_no_connection_open(self):
        mock = Mock.start()
        mock.get("/_mock/health")
        kept = list(mock._kept)
        self.assertEqual(len(kept), 1)
        mock.close()
        self.assertIsNone(kept[0].sock)
        self.assertEqual(mock._kept, [])

    def test_a_base_with_a_path_in_front_keeps_it(self):
        with Mock.start() as mock:
            behind = Mock(mock.base + "/_mock")
            self.addCleanup(behind.disconnect)
            self.assertEqual(behind.get("/health").status, 200)


class AStoppedMockAnswersNobody(unittest.TestCase):
    """The server lets go of the connections its clients kept (#220)."""

    def test_a_second_client_holding_a_connection_is_not_answered(self):
        # It went on being answered - with a 500, the database having been
        # closed - by a thread the stopped server had left serving it.
        first = Mock.start()
        second = Mock(first.base, timeout=2.0)
        self.addCleanup(second.disconnect)
        self.assertEqual(second.get("/_mock/health").status, 200)
        first.close()
        with self.assertRaises(OSError):
            second.get("/_mock/health")

    def test_no_handler_is_left_holding_a_connection(self):
        mock = Mock.start()
        server = mock.server
        other = Mock(mock.base, timeout=2.0)
        self.addCleanup(other.disconnect)
        other.get("/_mock/health")
        mock.get("/_mock/health")
        self.assertEqual(len(server._open), 2)
        mock.close()
        import time
        deadline = time.time() + 2.0
        while server._open and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(server._open), 0)


if __name__ == "__main__":
    unittest.main()
