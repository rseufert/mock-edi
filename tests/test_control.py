"""The control plane: reading what happened and changing what happens next."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, EURODIS, MockServerCase, x12_order


class Health(MockServerCase):
    def test_it_answers_with_who_the_mock_is(self):
        status, _h, data = self.get("/_mock/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["as2Id"], "MOCKEDI")
        self.assertEqual(data["partners"], 4)

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

    def test_it_is_visible_in_the_outbox_as_pending(self):
        self.send(x12_order("PO-LATER"))
        _s, _h, rows = self.get("/_mock/outbox")
        pending = [r for r in rows if r["status"] == "pending"]
        self.assertEqual([r["code"] for r in pending], ["810"])

    def test_advance_all_releases_it_without_waiting(self):
        self.send(x12_order("PO-LATER"))
        status, _h, data = self.post("/_mock/advance?all")
        self.assertEqual(status, 200)
        self.assertEqual(data["count"], 1)
        self.assertEqual([r["code"] for r in self.mailbox(ACME, "invoice")], ["810"])

    def test_advancing_by_too_little_releases_nothing(self):
        self.send(x12_order("PO-LATER"))
        _s, _h, data = self.post("/_mock/advance?seconds=60")
        self.assertEqual(data["count"], 0)


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
        self.assertEqual(headers["Content-Type"], "application/edi-x12")
        self.assertTrue(body.decode().startswith("ISA*00*"))

    def test_requests_are_logged(self):
        _s, _h, rows = self.get("/_mock/requests")
        self.assertTrue(any(row["path"] == "/edi" for row in rows))


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


class Authentication(MockServerCase):
    config_kwargs = {"basic_auth": "edi:secret"}

    def setUp(self):
        pass        # reset itself needs credentials

    def test_without_credentials_everything_is_challenged(self):
        status, headers, _body = self.get("/_mock/health", raw=True)
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers["WWW-Authenticate"])

    def test_with_them_it_works(self):
        import base64
        token = base64.b64encode(b"edi:secret").decode()
        status, _h, data = self.get("/_mock/health",
                                    headers={"Authorization": "Basic " + token})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
