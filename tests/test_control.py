"""The control plane: reading what happened and changing what happens next."""
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, EURODIS, FileDatabaseCase, MockServerCase, x12_order


class Health(MockServerCase):
    def test_it_answers_with_who_the_mock_is(self):
        status, _h, data = self.get("/_mock/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["as2Id"], "MOCKEDI")
        self.assertEqual(data["partners"], 5)       # four customers, one supplier

    def test_state_counts_everything_and_shows_the_queue(self):
        self.send(x12_order("PO-STATE"))
        _s, _h, data = self.get("/_mock/state")
        self.assertEqual(data["counts"]["purchase_order"], 3)   # 2 seeded + 1
        self.assertEqual(data["queue"]["ready"], 4)
        self.assertEqual(data["delays"]["invoice"], 0)


class Partners(MockServerCase):
    def test_the_seeded_partners_cover_the_interesting_behaviours(self):
        _s, _h, rows = self.get("/_mock/partners")
        behaviours = {row["id"]: row["behaviour"] for row in rows}
        self.assertEqual(behaviours["ACME"], "accept")
        self.assertEqual(behaviours["GLOBEX"], "short-ship")
        self.assertEqual(behaviours["EURODIS"], "accept")

    def test_a_behaviour_can_be_changed_at_runtime(self):
        data = self.behaviour(ACME, "reject-all")
        self.assertEqual(data["behaviour"], "reject-all")

    def test_an_unknown_behaviour_is_refused_with_the_known_ones(self):
        status, _h, data = self.patch("/_mock/partners/ACME",
                                      {"behaviour": "be-nice"})
        self.assertEqual(status, 400)
        self.assertIn("short-ship", data["error"])

    def test_a_partner_can_be_added(self):
        status, _h, data = self.post("/_mock/partners", {
            "id": "NEWCO", "name": "Newco Ltd", "dialect": "EDIFACT",
            "behaviour": "short-ship"})
        self.assertEqual(status, 201)
        self.assertEqual(data["dialect"], "EDIFACT")
        summary = self.send(x12_order("PO-NEW", sender="NEWCO"))
        self.assertTrue(summary["accepted"])

    def test_an_unknown_dialect_is_refused(self):
        status, _h, data = self.post("/_mock/partners",
                                     {"id": "BADCO", "dialect": "TRADACOMS"})
        self.assertEqual(status, 400)
        self.assertIn("X12", data["error"])

    def test_a_partner_can_be_removed_and_then_is_a_stranger(self):
        status, _h, data = self.request("DELETE", "/_mock/partners/ACME")
        self.assertEqual(status, 200)
        self.assertTrue(data["deleted"])
        status, _h, data = self.post("/edi", x12_order("PO-GONE"))
        self.assertEqual(status, 422)
        self.assertIn("ACME", data["error"])

    def test_an_unknown_partner_is_a_404_not_a_500(self):
        status, _h, _data = self.get("/_mock/partners/NOBODY")
        self.assertEqual(status, 404)


class Mailbox(MockServerCase):
    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-BOX"))

    def test_peeking_leaves_everything_where_it_was(self):
        self.assertEqual(len(self.mailbox(ACME, leave=True)), 4)
        self.assertEqual(len(self.mailbox(ACME, leave=True)), 4)

    def test_collecting_empties_it(self):
        self.assertEqual(len(self.mailbox(ACME, leave=False)), 4)
        self.assertEqual(self.mailbox(ACME, leave=False), [])

    def test_it_can_be_filtered_by_kind(self):
        rows = self.mailbox(ACME, kind="invoice")
        self.assertEqual([r["code"] for r in rows], ["810"])

    def test_raw_gives_the_payloads_and_nothing_else(self):
        _s, headers, body = self.get("/_mock/mailbox?leave&raw", raw=True)
        self.assertIn("text/plain", headers["Content-Type"])
        self.assertIn("ISA*00*", body.decode())

    def test_a_collected_document_is_marked_collected(self):
        self.mailbox(ACME, leave=False)
        _s, _h, rows = self.get("/_mock/outbox")
        self.assertTrue(all(row["status"] == "collected" for row in rows))


class Delays(MockServerCase):
    config_kwargs = {"invoice_delay_ms": 3600000}   # an hour away

    def test_a_delayed_document_is_not_in_the_mailbox_yet(self):
        self.send(x12_order("PO-LATER"))
        self.assertEqual([r["code"] for r in self.mailbox(ACME)],
                         ["997", "855", "856"])

    def test_the_work_not_done_yet_is_visible_as_scheduled(self):
        """A delayed invoice is not an unsent document; it is unwritten.

        Delaying the despatch has to delay the *packing*, not merely the
        posting, or a change arriving in the meantime could never affect it.
        So a delayed invoice does not exist yet, and shows up as promised
        work rather than as a document waiting in the outbox.
        """
        self.send(x12_order("PO-LATER"))
        _s, _h, rows = self.get("/_mock/scheduled")
        self.assertEqual([r["kind"] for r in rows], ["invoice"])
        self.assertEqual(rows[0]["po_number"], "PO-LATER")
        _s, _h, state = self.get("/_mock/state")
        self.assertEqual(state["scheduled"]["waiting"], 1)

    def test_advance_all_does_the_work_and_releases_it(self):
        self.send(x12_order("PO-LATER"))
        status, _h, data = self.post("/_mock/advance?all")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual([r["code"] for r in self.mailbox(ACME, "invoice")], ["810"])
        _s, _h, rows = self.get("/_mock/scheduled")
        self.assertEqual(rows, [])

    def test_advancing_by_too_little_does_nothing(self):
        self.send(x12_order("PO-LATER"))
        _s, _h, data = self.post("/_mock/advance?seconds=60")
        self.assertEqual(data["count"], 0)
        _s, _h, rows = self.get("/_mock/scheduled")
        self.assertEqual([r["kind"] for r in rows], ["invoice"])


class TheClockMoves(MockServerCase):
    """`advance?seconds=N` moves the mock's clock, and it stays moved (#47)."""
    config_kwargs = {"invoice_delay_ms": 90000}     # ninety seconds away

    def test_two_small_advances_add_up(self):
        self.send(x12_order("PO-CLOCK"))
        _s, _h, first = self.post("/_mock/advance?seconds=60")
        self.assertEqual(first["count"], 0)
        _s, _h, second = self.post("/_mock/advance?seconds=60")
        self.assertEqual(second["count"], 1)
        self.assertEqual([r["code"] for r in self.mailbox(ACME, "invoice")], ["810"])

    def test_it_says_where_the_clock_is(self):
        self.post("/_mock/advance?seconds=60")
        _s, _h, data = self.post("/_mock/advance?seconds=30")
        self.assertEqual(data["advancedSeconds"], 90.0)
        self.assertIn("T", data["clock"])

    def test_documents_are_dated_by_the_moved_clock(self):
        import datetime
        self.post("/_mock/advance?seconds=%d" % (3 * 86400))
        before = datetime.date.today()
        self.send(x12_order("PO-LATER-DATE"))
        after = datetime.date.today()
        self.post("/_mock/advance?seconds=90")
        invoice = self.document(ACME, "invoice").groups[0].messages[0]
        dated = invoice.find("BIG").get(1)
        expected = {(day + datetime.timedelta(days=3)).strftime("%Y%m%d")
                    for day in (before, after)}
        self.assertIn(dated, expected)

    def test_a_reset_puts_the_clock_back(self):
        self.post("/_mock/advance?seconds=3600")
        self.post("/_mock/reset")
        _s, _h, data = self.post("/_mock/advance?seconds=0")
        self.assertEqual(data["advancedSeconds"], 0.0)

    def test_a_parameter_it_does_not_take_moves_and_releases_nothing(self):
        # `?all&days=30` would release the invoice if the refusal came after
        # the work; `?seconds=60&second=1` would move the clock (#203).
        self.send(x12_order("PO-UNKNOWN-PARAMETER"))
        for query in ("days=30", "all&days=30", "seconds=60&second=1",
                      "failed&Partner=ACME"):
            with self.subTest(query=query):
                status, _h, data = self.post("/_mock/advance?" + query)
                self.assertEqual(status, 400, data)
        self.assertEqual(self.mailbox(ACME, "invoice"), [])
        _s, _h, data = self.post("/_mock/advance?seconds=0")
        self.assertEqual(data["advancedSeconds"], 0.0)

    def test_it_does_not_go_backwards(self):
        status, _h, data = self.post("/_mock/advance?seconds=-60")
        self.assertEqual(status, 400, data)
        self.assertIn("negative", data["error"])


class TheClockHasALimit(MockServerCase):
    """A `seconds` the clock cannot hold is refused, and moves nothing (#203).

    The offset used to be added to before anything checked that `now()` could
    still be computed, so one oversized advance made every request after it a
    500 until `/_mock/reset`.
    """
    LIMIT = 100 * 365 * 86400       # a hundred years of seconds

    def advanced(self):
        _s, _h, data = self.post("/_mock/advance?seconds=0")
        return data["advancedSeconds"]

    def test_more_than_the_clock_can_hold_is_refused_naming_the_limit(self):
        for seconds in ("300000000000", "1e300", str(self.LIMIT + 1)):
            with self.subTest(seconds=seconds):
                status, _h, data = self.post("/_mock/advance?seconds=" + seconds)
                self.assertEqual(status, 400, data)
                self.assertEqual(data["parameter"], "seconds")
                self.assertIn(str(self.LIMIT), data["error"])
                self.assertIn("/_mock/reset", data["error"])

    def test_a_refused_advance_leaves_the_clock_where_it_was(self):
        self.post("/_mock/advance?seconds=3600")
        for seconds in ("300000000000", "1e300", "inf"):
            self.post("/_mock/advance?seconds=" + seconds)
        self.assertEqual(self.advanced(), 3600.0)

    def test_the_mock_still_answers_an_order_afterwards(self):
        self.post("/_mock/advance?seconds=300000000000")
        self.send(x12_order("PO-AFTER-OVERFLOW"))
        status, _h, data = self.post("/_mock/advance?all")
        self.assertEqual(status, 200, data)
        self.assertEqual([r["code"] for r in self.mailbox(ACME, "invoice")],
                         ["810"])

    def test_the_pipeline_refuses_what_is_not_a_finite_number(self):
        # The endpoint turns these away before they get here; a caller of
        # `advance` itself would otherwise meet an OverflowError.
        import datetime
        from mockedi import db, pipeline
        from mockedi.server import Config
        conn = db.connect(":memory:")
        self.addCleanup(conn.close)
        clock = pipeline.Pipeline(conn, Config(db_path=":memory:"))
        for seconds in (float("inf"), float("-inf"), float("nan"), 1e300):
            with self.subTest(seconds=seconds):
                with self.assertRaises(ValueError):
                    clock.advance(seconds)
                self.assertEqual(clock.offset, datetime.timedelta(0))

    def test_the_limit_is_on_the_total_not_on_one_call(self):
        self.post("/_mock/advance?seconds=%d" % (self.LIMIT - 60))
        status, _h, data = self.post("/_mock/advance?seconds=61")
        self.assertEqual(status, 400, data)
        # It says how much room is left, which is what the caller can act on.
        self.assertIn("60", data["error"])
        self.assertEqual(self.advanced(), float(self.LIMIT - 60))

    def test_exactly_the_limit_is_allowed_and_the_mock_still_works(self):
        status, _h, data = self.post("/_mock/advance?seconds=%d" % self.LIMIT)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["advancedSeconds"], float(self.LIMIT))
        self.send(x12_order("PO-A-CENTURY-ON"))
        status, _h, data = self.post("/_mock/advance?all")
        self.assertEqual(status, 200, data)
        self.assertEqual([r["code"] for r in self.mailbox(ACME, "invoice")],
                         ["810"])


class SendOnDemand(MockServerCase):
    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-REPLAY"))
        self.mailbox(ACME, leave=False)

    def test_an_invoice_can_be_replayed(self):
        status, _h, data = self.post("/_mock/send",
                                     {"partner": ACME, "kind": "invoice",
                                      "order": "PO-REPLAY"})
        self.assertEqual(status, 201)
        self.assertEqual(data["code"], "810")
        self.assertEqual([r["code"] for r in self.mailbox(ACME)], ["810"])

    def test_an_unknown_order_is_refused(self):
        status, _h, data = self.post("/_mock/send",
                                     {"partner": ACME, "kind": "invoice",
                                      "order": "NOPE"})
        self.assertEqual(status, 400)
        self.assertIn("NOPE", data["error"])

    def test_an_unknown_partner_is_a_404(self):
        status, _h, _data = self.post("/_mock/send",
                                      {"partner": "NOBODY", "kind": "invoice",
                                       "order": "PO-REPLAY"})
        self.assertEqual(status, 404)

    def test_an_acknowledgment_cannot_be_conjured_out_of_nothing(self):
        status, _h, data = self.post("/_mock/send",
                                     {"partner": ACME, "kind": "acknowledgment",
                                      "order": "PO-REPLAY"})
        self.assertEqual(status, 400)
        self.assertIn("interchange", data["error"])


class Archive(MockServerCase):
    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-LOG"))

    def test_every_transaction_set_is_listed_in_and_out(self):
        _s, _h, rows = self.get("/_mock/documents")
        codes = [(row["direction"], row["code"]) for row in rows]
        self.assertIn(("in", "850"), codes)
        self.assertIn(("out", "997"), codes)
        self.assertIn(("out", "810"), codes)

    def test_documents_can_be_filtered(self):
        _s, _h, rows = self.get("/_mock/documents?direction=in")
        self.assertEqual([row["code"] for row in rows], ["850"])

    def test_one_document_comes_with_the_interchange_it_arrived_in(self):
        _s, _h, rows = self.get("/_mock/documents?direction=in")
        _s, _h, row = self.get("/_mock/documents/%d" % rows[0]["id"])
        self.assertIn("ISA*00*", row["payload"])
        self.assertEqual(row["reference"], "PO-LOG")

    def test_an_interchange_can_be_fetched_raw(self):
        _s, _h, rows = self.get("/_mock/interchanges")
        _s, headers, body = self.get("/_mock/interchanges/%d?raw" % rows[-1]["id"],
                                     raw=True)
        # The charset it arrived in: X12 declares none, so ISO 8859-1.
        self.assertEqual(headers["Content-Type"],
                         "application/edi-x12; charset=iso-8859-1")
        self.assertTrue(body.decode().startswith("ISA*00*"))

    def test_requests_are_logged(self):
        _s, _h, rows = self.get("/_mock/requests")
        self.assertTrue(any(row["path"] == "/edi" for row in rows))


class LoggedBeforeItIsAnswered(FileDatabaseCase):
    """The request log holds a request by the time its answer arrives.

    The row used to be written after the response, so a client asking
    `/_mock/requests` the moment its answer came back could miss it - which
    is what `Archive.test_requests_are_logged` did on a slow CI runner. Asked
    that way, a second request has to be scheduled first and nearly always
    loses the race. So these read the file directly, on a connection of
    their own, the instant each answer arrives: there, the old order missed
    the row 291 times in 300.
    """

    def setUp(self):
        super().setUp()
        # Opened once: opening it per check takes long enough to hide the
        # race this is here to catch.
        self.reader = sqlite3.connect(self.db_path)
        self.addCleanup(self.reader.close)

    def logged(self, path):
        return self.reader.execute(
            "SELECT method, status, bytes_in, bytes_out FROM request_log"
            " WHERE path = ?", (path,)).fetchall()

    def test_every_answer_is_logged_before_it_arrives(self):
        for index in range(100):
            path = "/nowhere-%d" % index
            status, headers, _data = self.get(path)
            self.assertEqual(status, 404)
            self.assertEqual(self.logged(path),
                             [("GET", 404, 0, int(headers["Content-Length"]))], path)

    def test_a_post_is_logged_with_what_came_in_and_went_out(self):
        order = x12_order("PO-LOGGED").encode()
        status, headers, _data = self.post("/edi", order,
                                           headers={"Content-Type": "application/edi-x12"})
        self.assertEqual(status, 200)
        self.assertEqual(self.logged("/edi"),
                         [("POST", 200, len(order), int(headers["Content-Length"]))])

    def test_an_options_request_is_logged_before_it_is_answered(self):
        for index in range(20):
            path = "/edi/%d" % index
            status, _h, _data = self.request("OPTIONS", path)
            self.assertEqual(status, 204)
            self.assertEqual(self.logged(path), [("OPTIONS", 204, 0, 0)], path)


class LoggedBeforeTheChallenge(FileDatabaseCase):
    config_kwargs = {"basic_auth": "tester:secret"}

    def test_a_401_is_logged_before_it_is_answered(self):
        reader = sqlite3.connect(self.db_path)
        self.addCleanup(reader.close)
        for index in range(20):
            path = "/_mock/state/%d" % index
            status, _h, _data = self.get(path)
            self.assertEqual(status, 401)
            self.assertEqual(reader.execute(
                "SELECT status FROM request_log WHERE path = ?", (path,)).fetchall(),
                [(401,)], path)


class Reset(MockServerCase):
    def test_it_puts_everything_back(self):
        self.send(x12_order("PO-GONE-SOON"))
        self.behaviour(ACME, "reject-all")
        self.post("/_mock/reset")
        status, _h, _data = self.get("/_mock/orders/PO-GONE-SOON")
        self.assertEqual(status, 404)
        _s, _h, row = self.get("/_mock/partners/ACME")
        self.assertEqual(row["behaviour"], "accept")


class NotFound(MockServerCase):
    def test_an_unknown_control_endpoint_lists_the_real_ones(self):
        status, _h, data = self.get("/_mock/nonsense")
        self.assertEqual(status, 404)
        self.assertIn("mailbox", data["endpoints"])

    def test_an_unknown_path_suggests_where_to_start(self):
        status, _h, data = self.get("/nothing/here")
        self.assertEqual(status, 404)
        self.assertIn("/edi", data["try"])

    def test_the_index_page_renders(self):
        status, headers, body = self.get("/", raw=True)
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn("mock-edi", body.decode())


class EncodedPathSegments(MockServerCase):
    """A path is split on `/` first, and each segment is decoded once (#25)."""

    def test_an_order_number_with_a_slash_is_reachable(self):
        self.send(x12_order("PO/2026/1"))
        status, _h, data = self.get("/_mock/orders/PO%2F2026%2F1")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["po_number"], "PO/2026/1")

    def test_a_segment_is_decoded_exactly_once(self):
        # `%2541` is `%41` decoded once, and `A` decoded twice.
        self.send(x12_order("PO%41"))
        status, _h, data = self.get("/_mock/orders/PO%2541")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["po_number"], "PO%41")

    def test_a_missing_order_is_named_as_decoded(self):
        status, _h, data = self.get("/_mock/orders/PO%2FNONE")
        self.assertEqual(status, 404)
        self.assertIn("'PO/NONE'", data["error"])


class AuthenticationOutsideAscii(MockServerCase):
    """A password with an umlaut made every request a 500, right or wrong (#207)."""
    config_kwargs = {"basic_auth": "edi:p\u00e4ssw\u00f6rd"}

    def setUp(self):
        pass        # reset itself needs credentials

    def health(self, credential: bytes):
        import base64
        token = base64.b64encode(credential).decode()
        status, _h, _body = self.get(
            "/_mock/health", headers={"Authorization": "Basic " + token}, raw=True)
        return status

    def test_the_right_one_works_as_utf8(self):
        self.assertEqual(self.health("edi:p\u00e4ssw\u00f6rd".encode("utf-8")), 200)

    def test_and_as_latin1_which_a_client_may_send_instead(self):
        self.assertEqual(self.health("edi:p\u00e4ssw\u00f6rd".encode("latin-1")), 200)

    def test_a_wrong_one_is_challenged(self):
        for attempt in (b"edi:password", "edi:p\u00e4ssword".encode("utf-8"),
                        b"", b"\xff"):
            with self.subTest(attempt=attempt):
                self.assertEqual(self.health(attempt), 401)


class BadQueryValues(MockServerCase):
    """A value that cannot be read is the client's mistake: 400, named (#31)."""

    def assertRefused(self, path, parameter):
        status, _h, data = self.request("POST" if "advance" in path else "GET", path)
        self.assertEqual(status, 400, data)
        self.assertEqual(data["parameter"], parameter)
        self.assertIn(parameter, data["error"])

    def test_advance_seconds(self):
        self.assertRefused("/_mock/advance?seconds=abc", "seconds")

    def test_a_number_that_is_not_finite(self):
        self.assertRefused("/_mock/advance?seconds=nan", "seconds")

    def test_advance_refuses_a_parameter_it_does_not_take(self):
        # It used to answer 200 and advance nothing: mock-bank's clock takes
        # days, and a tester moving between the mocks was told all was well.
        status, _h, data = self.post("/_mock/advance?days=30")
        self.assertEqual(status, 400, data)
        self.assertEqual(data["parameter"], "days")
        for name in ("seconds", "all", "failed", "partner"):
            self.assertIn(name, data["error"])

    def test_and_says_what_days_would_be_in_seconds(self):
        _s, _h, data = self.post("/_mock/advance?days=30")
        self.assertIn("seconds=2592000", data["error"])
        # Only where there is a figure to give, and one `seconds` would take.
        for days in ("soon", "nan", "-1", "1e300"):
            with self.subTest(days=days):
                status, _h, data = self.post("/_mock/advance?days=" + days)
                self.assertEqual(status, 400, data)
                self.assertNotIn("seconds=", data["error"])

    def test_every_parameter_it_does_take_still_works(self):
        for query in ("", "seconds=1", "all", "failed", "failed&partner=ACME"):
            with self.subTest(query=query):
                status, _h, data = self.post("/_mock/advance?" + query)
                self.assertEqual(status, 200, data)

    def test_unacknowledged_older_than(self):
        self.assertRefused("/_mock/unacknowledged?older-than=soon", "older-than")

    def test_limit(self):
        self.assertRefused("/_mock/documents?limit=ten", "limit")

    def test_a_good_value_still_works(self):
        status, _h, data = self.post("/_mock/advance?seconds=1.5")
        self.assertEqual(status, 200, data)


class HeadAndOptions(MockServerCase):
    """What a liveness probe and a CORS preflight send (#31)."""

    def test_head_is_a_get_without_the_body(self):
        status, headers, body = self.request("HEAD", "/_mock/health", raw=True)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        _s, _h, full = self.get("/_mock/health", raw=True)
        self.assertEqual(int(headers["Content-Length"]), len(full))

    def test_head_of_a_missing_path_is_a_404(self):
        status, _h, body = self.request("HEAD", "/nothing/here", raw=True)
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")

    def test_options_lists_the_methods(self):
        status, headers, body = self.request("OPTIONS", "/_mock/health", raw=True)
        self.assertEqual(status, 204)
        self.assertEqual(body, b"")
        for method in ("GET", "HEAD", "POST", "OPTIONS"):
            self.assertIn(method, headers["Allow"])


class Authentication(MockServerCase):
    config_kwargs = {"basic_auth": "edi:secret"}

    def setUp(self):
        pass        # reset itself needs credentials

    def test_without_credentials_everything_is_challenged(self):
        status, headers, _body = self.get("/_mock/health", raw=True)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])

    def test_wrong_ones_are_challenged_too(self):
        import base64
        token = base64.b64encode(b"edi:guess").decode()
        status, headers, _body = self.get(
            "/_mock/health", headers={"Authorization": "Basic " + token}, raw=True)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])

    def test_with_them_it_works(self):
        import base64
        token = base64.b64encode(b"edi:secret").decode()
        status, _h, data = self.get("/_mock/health",
                                    headers={"Authorization": "Basic " + token})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_an_attempt_outside_ascii_is_wrong_and_not_a_500(self):
        # `compare_digest` on two strings refuses anything outside ASCII, so
        # this was a 500 where a 401 belongs (#207).
        import base64
        for attempt in ("edi:s\u00e9cret".encode("utf-8"),
                        "edi:s\u00e9cret".encode("latin-1"), b"\xff\xfe:\x80"):
            with self.subTest(attempt=attempt):
                token = base64.b64encode(attempt).decode()
                status, _h, _body = self.get(
                    "/_mock/health", headers={"Authorization": "Basic " + token},
                    raw=True)
                self.assertEqual(status, 401)

    def test_a_preflight_is_answered_without_them(self):
        # A CORS preflight never carries credentials.
        status, headers, _body = self.request("OPTIONS", "/_mock/health", raw=True)
        self.assertEqual(status, 204)
        self.assertIn("GET", headers["Allow"])

    def test_head_is_challenged_like_get(self):
        status, _h, _body = self.request("HEAD", "/_mock/health", raw=True)
        self.assertEqual(status, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
