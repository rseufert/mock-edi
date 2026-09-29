"""The route table answers every path as the old `if` chain did (#182).

The HTTP cases were written against the chain before the table replaced it,
and pass on both: the door aliases, the line each door refuses another method with, the index
under either name and any method, and the control plane matched as loosely as
it always was - `POST /_mock/catalog` is the catalog. The suite covered the
endpoints but not these edges, so a move that lost one would have passed it.
The last case is about the table itself.

Tightening the control plane to exact methods and paths is #188; when it
lands, the loose half of this file is what it changes.
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


class TheIndexAnswersWhateverItIsAsked(MockServerCase):

    def test_either_name_and_any_method(self):
        for method in ("GET", "POST"):
            for path in ("/", "/index.html"):
                with self.subTest(method=method, path=path):
                    status, headers, data = self.request(method, path, raw=True)
                    self.assertEqual(status, 200)
                    self.assertTrue(headers["Content-Type"].startswith("text/html"))
                    self.assertIn(b"<h1>mock-edi</h1>", data)


class TheControlPlaneMatchesAsLooselyAsItAlwaysHas(MockServerCase):
    """Each of these is what #188 would change, and not before."""

    def test_a_read_endpoint_ignores_the_method(self):
        _status, _headers, listed = self.get("/_mock/catalog")
        status, _headers, data = self.post("/_mock/catalog")
        self.assertEqual(status, 200)
        self.assertEqual(data, listed)

    def test_segments_past_the_endpoint_are_ignored(self):
        for path in ("/_mock/health/", "/_mock/health/anything"):
            with self.subTest(path=path):
                status, _headers, data = self.get(path)
                self.assertEqual(status, 200, data)
                self.assertEqual(data["status"], "ok")

    def test_an_unknown_endpoint_lists_the_known_ones(self):
        status, _headers, data = self.get("/_mock/nothing-here")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "no control endpoint 'nothing-here'")
        self.assertIn("health", data["endpoints"])


class ANonsensePathIsAnswered404(MockServerCase):

    def test_with_where_to_try_instead(self):
        status, _headers, data = self.get("/nowhere")
        self.assertEqual(status, 404)
        self.assertEqual(data, {"error": "no route for GET /nowhere",
                                "try": ["/as2", "/edi", "/_mock/health", "/"]})


class TheTableHasNoOverlaps(unittest.TestCase):
    """Order in the table never decides who answers, as its docstring says."""

    def test_each_name_a_route_is_registered_under_finds_that_route(self):
        from mockedi import routes
        for entry in routes.TABLE:
            method = "POST" if entry.method == routes.ANY else entry.method
            for path in (entry.pattern,) + entry.aliases:
                with self.subTest(path=path):
                    found, _arguments = routes.find(method, path)
                    self.assertIs(found, entry)


if __name__ == "__main__":
    unittest.main()
