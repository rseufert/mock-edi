"""A request is matched on its method and its path exactly (#188).

The doors answer under each name they always have, and refuse another method
in their own words. Everywhere else the path is the pattern, segment for
segment, and the method is the one registered: a read sent with another
method is a 405 that names what to send, with an `Allow` header, and a
trailing slash, an extra segment or a path that merely starts with `/_mock`
is a 404 that says what there is.

Until #188 the control plane answered any method on a read endpoint and
ignored what followed the segments it knew; this file pinned that while
`server.py` was split (#182), and now pins the opposite.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import MockServerCase, x12_order

X12 = {"Content-Type": "application/edi-x12"}


class TheDoorsAnswerUnderEachOfTheirNames(MockServerCase):

    def test_edi_with_a_trailing_slash_is_the_plain_door(self):
        status, _headers, data = self.post("/edi/", x12_order("PO-ROUTE-1"),
                                           headers=X12)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["orders"], ["PO-ROUTE-1"])

    def test_every_name_for_as2_is_the_as2_door(self):
        for path in ("/as2", "/as2/", "/as2/receive"):
            with self.subTest(path=path):
                status, _headers, data = self.post(path, x12_order("PO-ROUTE-2"),
                                                   headers=X12, raw=True)
                # No AS2 headers, so the door says so: which it can only do
                # if the request reached it.
                self.assertEqual(status, 400)
                self.assertIn(b"expects AS2 headers", data)

    def test_both_names_for_the_mdn_door(self):
        for path in ("/as2/mdn", "/as2/mdn/"):
            with self.subTest(path=path):
                status, _headers, data = self.post(path, b"")
                self.assertEqual(status, 200, data)
                self.assertTrue(data["recorded"])


class ADoorRefusesAnotherMethodInItsOwnWords(MockServerCase):

    def test_each_door_names_what_to_post(self):
        for method, path, line in (
                ("GET", "/edi", b"POST an interchange here\n"),
                ("PUT", "/edi/", b"POST an interchange here\n"),
                ("GET", "/as2", b"POST an interchange here\n"),
                ("DELETE", "/as2/receive", b"POST an interchange here\n"),
                ("GET", "/as2/mdn", b"POST an MDN here\n"),
                ("PATCH", "/as2/mdn/", b"POST an MDN here\n")):
            with self.subTest(method=method, path=path):
                status, headers, data = self.request(method, path, raw=True)
                self.assertEqual(status, 405)
                self.assertEqual(data, line)
                self.assertTrue(headers["Content-Type"].startswith("text/plain"))

    def test_a_head_is_refused_as_its_get_would_be(self):
        status, _headers, data = self.request("HEAD", "/edi", raw=True)
        self.assertEqual((status, data), (405, b""))


class TheIndexIsReadWithAGet(MockServerCase):

    def test_either_name_answers_a_get(self):
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, headers, data = self.request("GET", path, raw=True)
                self.assertEqual(status, 200)
                self.assertTrue(headers["Content-Type"].startswith("text/html"))
                self.assertIn(b"<h1>mock-edi</h1>", data)

    def test_another_method_is_refused(self):
        for path in ("/", "/index.html"):
            with self.subTest(path=path):
                status, headers, data = self.request("POST", path, raw=True)
                self.assertEqual((status, data), (405, b"GET the index page\n"))
                self.assertEqual(headers["Allow"], "GET, HEAD")


class AReadIsAGet(MockServerCase):
    """Each of these answered 200 to any method until #188."""

    READS = ("/_mock/health", "/_mock/state", "/_mock/behaviours",
             "/_mock/requests", "/_mock/dictionary", "/_mock/catalog",
             "/_mock/orders", "/_mock/documents", "/_mock/interchanges",
             "/_mock/mdns", "/_mock/disagreements", "/_mock/remittances",
             "/_mock/mailbox", "/_mock/outbox", "/_mock/drop",
             "/_mock/scheduled", "/_mock/unacknowledged")

    def test_each_answers_a_get(self):
        for path in self.READS:
            with self.subTest(path=path):
                status, _headers, data = self.get(path)
                self.assertEqual(status, 200, data)

    def test_each_refuses_another_method_and_names_the_one_it_takes(self):
        for path in self.READS:
            for method in ("POST", "PUT", "DELETE"):
                with self.subTest(path=path, method=method):
                    status, headers, data = self.request(method, path, raw=True)
                    self.assertEqual(status, 405)
                    self.assertEqual(data, ("GET %s\n" % path).encode())
                    self.assertEqual(headers["Allow"], "GET, HEAD")
                    self.assertTrue(
                        headers["Content-Type"].startswith("text/plain"))

    def test_a_refused_read_has_no_side_effect(self):
        # POST /_mock/mailbox used to collect the mailbox, as the GET does.
        self.send(x12_order("PO-ROUTE-4"))
        self.request("POST", "/_mock/mailbox", raw=True)
        _s, _h, waiting = self.get("/_mock/mailbox?leave")
        self.assertTrue(waiting, "a refused request emptied the mailbox")

    def test_a_head_is_answered_as_its_get(self):
        status, _headers, data = self.request("HEAD", "/_mock/health", raw=True)
        self.assertEqual((status, data), (200, b""))


class AnActionKeepsItsOwnRefusal(MockServerCase):
    """These always refused another method; the words are the same."""

    def test_each_says_what_to_do_and_what_it_allows(self):
        for method, path, line, allow in (
                ("GET", "/_mock/reset", b"POST to reset\n", "POST"),
                ("GET", "/_mock/validate",
                 b"POST an interchange to validate it\n", "POST"),
                ("GET", "/_mock/advance", b"POST to advance the queue\n", "POST"),
                ("GET", "/_mock/send", b"POST to send a document\n", "POST"),
                ("GET", "/_mock/outbox/1/retry",
                 b"POST to retry a delivery\n", "POST"),
                ("GET", "/_mock/drop/scan",
                 b"POST to scan the drop directory\n", "POST"),
                ("GET", "/_mock/purchase", b"POST an order to place it\n", "POST"),
                ("GET", "/_mock/purchase/PO-1/change",
                 b"POST an order to place it\n", "POST"),
                ("PUT", "/_mock/partners", b"GET or POST partners\n",
                 "GET, HEAD, POST"),
                ("POST", "/_mock/partners/ACME",
                 b"GET, PATCH or DELETE a partner\n",
                 "DELETE, GET, HEAD, PATCH, PUT"),
                ("PATCH", "/_mock/partners/ACME/profile",
                 b"GET, PUT or DELETE a partner's profile\n",
                 "DELETE, GET, HEAD, POST, PUT")):
            with self.subTest(method=method, path=path):
                status, headers, data = self.request(method, path, raw=True)
                self.assertEqual((status, data), (405, line))
                self.assertEqual(headers["Allow"], allow)

    def test_a_retry_of_a_delivery_that_is_not_a_number_is_a_404(self):
        status, _headers, data = self.post("/_mock/outbox/x/retry")
        self.assertEqual(status, 404)
        self.assertEqual(data, {"error": "no outbound document 'x'"})


class ThePathIsThePatternSegmentForSegment(MockServerCase):

    def test_what_a_pattern_names_is_answered(self):
        self.send(x12_order("PO-ROUTE-3"))
        for path, key in (("/_mock/orders/PO-ROUTE-3", "po_number"),
                          ("/_mock/orders/PO-ROUTE-3/timeline", "events"),
                          ("/_mock/dictionary/x12", "transactionSets"),
                          ("/_mock/dictionary/x12/850", "segments"),
                          ("/_mock/documents/1", "payload"),
                          ("/_mock/interchanges/1", "payload"),
                          ("/_mock/partners/ACME", "behaviour")):
            with self.subTest(path=path):
                status, _headers, data = self.get(path)
                self.assertEqual(status, 200, data)
                self.assertIn(key, data)

    def test_a_segment_too_many_is_a_404_listing_the_endpoints_routes(self):
        for path in ("/_mock/health/anything", "/_mock/orders/PO-ROUTE-3/other",
                     "/_mock/orders/PO-ROUTE-3/timeline/x",
                     "/_mock/dictionary/x12/850/extra", "/_mock/documents/1/x",
                     "/_mock/outbox/1", "/_mock/scheduled/x", "/_mock/drop/other",
                     "/_mock/partners/ACME/other",
                     "/_mock/purchase/PO-1/other"):
            with self.subTest(path=path):
                status, _headers, data = self.get(path)
                self.assertEqual(status, 404, data)
                self.assertEqual(data["error"], "no route for GET " + path)
                self.assertTrue(data["routes"])
                name = path.split("/")[2]
                for listed in data["routes"]:
                    self.assertIn("/_mock/%s" % name, listed)

    def test_a_trailing_or_a_doubled_slash_is_a_404(self):
        for path in ("/_mock/health/", "/_mock//health", "/_mock/orders/"):
            with self.subTest(path=path):
                status, _headers, data = self.get(path)
                self.assertEqual(status, 404, data)

    def test_a_path_that_only_starts_like_the_control_plane_is_not_it(self):
        # `/_mockery/health` was the health check.
        for path in ("/_mockery", "/_mockery/health", "/_mock-health"):
            with self.subTest(path=path):
                status, _headers, data = self.get(path)
                self.assertEqual(status, 404)
                self.assertEqual(data, {
                    "error": "no route for GET " + path,
                    "try": ["/as2", "/edi", "/_mock/health", "/"]})

    def test_an_encoded_slash_in_an_order_number_is_one_segment(self):
        status, _headers, data = self.get("/_mock/orders/PO%2F2026%2F1")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "no purchase order 'PO/2026/1'")


class AnUnknownEndpointListsTheKnownOnes(MockServerCase):

    def test_from_the_table_so_none_is_left_out(self):
        from mockedi import routes
        status, _headers, data = self.get("/_mock/nothing-here")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "no control endpoint 'nothing-here'")
        self.assertEqual(data["endpoints"], routes.endpoints())
        # The two the hand-written list had lost.
        self.assertIn("purchase", data["endpoints"])
        self.assertIn("disagreements", data["endpoints"])

    def test_the_bare_prefix_is_an_endpoint_with_no_name(self):
        for path in ("/_mock", "/_mock/"):
            with self.subTest(path=path):
                status, _headers, data = self.request("POST", path)
                self.assertEqual(status, 404)
                self.assertEqual(data["error"], "no control endpoint ''")


class ANonsensePathIsAnswered404(MockServerCase):

    def test_with_where_to_try_instead(self):
        status, _headers, data = self.get("/nowhere")
        self.assertEqual(status, 404)
        self.assertEqual(data, {"error": "no route for GET /nowhere",
                                "try": ["/as2", "/edi", "/_mock/health", "/"]})


class TheTable(unittest.TestCase):

    def test_no_two_registrations_take_one_method_and_pattern(self):
        from mockedi import routes
        taken = [(entry.method, entry.pattern) for entry in routes.TABLE]
        self.assertEqual(len(taken), len(set(taken)))

    def test_each_pattern_and_alias_finds_its_own_route(self):
        from mockedi import routes
        for entry in routes.TABLE:
            for path in (entry.pattern,) + entry.aliases:
                with self.subTest(method=entry.method, path=path):
                    found, _arguments, allowed = routes.find(entry.method, path)
                    self.assertIs(found, entry)
                    self.assertEqual(allowed, [])

    def test_no_literal_pattern_is_shadowed_by_one_with_a_placeholder(self):
        # `/_mock/drop/scan` and a `/_mock/drop/<x>` would both match one
        # path, and registration order would decide. There is no such pair.
        from mockedi import routes
        for entry in routes.TABLE:
            matches = {(other.method, other.pattern) for other in routes.TABLE
                       if other.method == entry.method
                       and routes._match(other, entry.pattern) is not None}
            self.assertEqual(matches, {(entry.method, entry.pattern)})


if __name__ == "__main__":
    unittest.main()
