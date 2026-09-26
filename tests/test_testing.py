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


if __name__ == "__main__":
    unittest.main()
