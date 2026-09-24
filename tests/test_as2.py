"""AS2: identifiers, MDNs, the MIC, and the things this mock will not do."""
import base64
import hashlib
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import as2
from support import ACME, MockServerCase, as2_headers, x12_order


class SynchronousMdn(MockServerCase):
    def setUp(self):
        super().setUp()
        self.payload = x12_order("PO-AS2")
        self.status, self.headers, self.body = self.request(
            "POST", "/as2", self.payload, headers=as2_headers(), raw=True)
        self.text = self.body.decode()

    def test_the_receipt_comes_back_on_the_same_response(self):
        self.assertEqual(self.status, 200)
        self.assertIn("multipart/report", self.headers["Content-Type"])
        self.assertIn("report-type=disposition-notification",
                      self.headers["Content-Type"])

    def test_the_as2_identifiers_are_swapped_for_the_reply(self):
        self.assertEqual(self.headers["AS2-From"], "MOCKEDI")
        self.assertEqual(self.headers["AS2-To"], ACME)

    def test_it_carries_a_machine_readable_disposition(self):
        self.assertIn("Disposition: automatic-action/MDN-sent-automatically; "
                      "processed", self.text)
        self.assertIn("Original-Message-ID: <m1@acme.example>", self.text)

    def test_the_mic_is_the_digest_the_sender_asked_for(self):
        expected = base64.b64encode(
            hashlib.sha256(self.payload.encode()).digest()).decode()
        self.assertIn("Received-Content-MIC: %s, sha256" % expected, self.text)

    def test_the_default_digest_is_sha1_when_none_is_named(self):
        headers = as2_headers(message_id="<m2@acme.example>")
        del headers["Disposition-Notification-Options"]
        _status, _h, body = self.request("POST", "/as2", x12_order("PO-AS2B"),
                                         headers=headers, raw=True)
        expected = base64.b64encode(
            hashlib.sha1(x12_order("PO-AS2B").encode()).digest()).decode()
        self.assertIn("Received-Content-MIC: %s, sha1" % expected, body.decode())

    def test_the_human_readable_part_says_what_happened(self):
        self.assertIn("1 transaction set(s), 1 accepted", self.text)
        self.assertIn("PO-AS2", self.text)
        self.assertIn("997, 855, 856, 810", self.text)

    def test_the_order_went_through_the_same_pipeline(self):
        self.assertEqual(self.order("PO-AS2")["status"], "invoiced")

    def test_the_mdn_is_recorded(self):
        _status, _h, rows = self.get("/_mock/mdns")
        self.assertEqual(rows[0]["direction"], "out")
        self.assertEqual(rows[0]["mode"], "sync")
        self.assertEqual(rows[0]["original_id"], "<m1@acme.example>")


class AsynchronousMdn(MockServerCase):
    def test_the_response_is_202_and_the_receipt_is_queued(self):
        status, _headers, _body = self.request(
            "POST", "/as2", x12_order("PO-ASYNC"),
            headers=as2_headers(async_url="http://127.0.0.1:1/never"), raw=True)
        self.assertEqual(status, 202)
        _s, _h, rows = self.get("/_mock/mdns")
        self.assertEqual(rows[0]["mode"], "async")
        self.assertEqual(rows[0]["url"], "http://127.0.0.1:1/never")


class NoMdnRequested(MockServerCase):
    def test_a_sender_that_asks_for_no_receipt_gets_plain_text(self):
        status, headers, body = self.request(
            "POST", "/as2", x12_order("PO-NOMDN"),
            headers=as2_headers(mdn=False), raw=True)
        self.assertEqual(status, 200)
        self.assertNotIn("multipart/report", headers["Content-Type"])
        self.assertIn("accepted", body.decode())


class Refusals(MockServerCase):
    def test_an_encrypted_payload_is_refused_in_so_many_words(self):
        headers = as2_headers()
        headers["Content-Type"] = "application/pkcs7-mime"
        _status, _h, body = self.request("POST", "/as2", x12_order("PO-ENC"),
                                         headers=headers, raw=True)
        text = body.decode()
        self.assertIn("insufficient-message-security", text)
        self.assertIn("does not implement S/MIME", text)

    def test_a_message_addressed_to_someone_else_is_refused(self):
        _status, _h, body = self.request(
            "POST", "/as2", x12_order("PO-WRONG"),
            headers=as2_headers(receiver="SOMEONE-ELSE"), raw=True)
        text = body.decode()
        self.assertIn("unexpected-processing-error", text)
        self.assertIn("SOMEONE-ELSE", text)

    def test_an_unregistered_sender_is_refused_by_name(self):
        _status, _h, body = self.request(
            "POST", "/as2", x12_order("PO-STRANGER", sender="STRANGER"),
            headers=as2_headers(sender="STRANGER"), raw=True)
        text = body.decode()
        self.assertIn("unexpected-processing-error", text)
        self.assertIn("STRANGER", text)

    def test_a_plain_post_to_the_as2_endpoint_is_told_where_to_go(self):
        status, _h, body = self.request("POST", "/as2", x12_order("PO-PLAIN"),
                                        raw=True)
        self.assertEqual(status, 400)
        self.assertIn("/edi", body.decode())

    def test_something_that_is_not_edi_at_all_is_refused_with_what_it_saw(self):
        status, _h, data = self.post("/edi", '{"hello": "world"}')
        self.assertEqual(status, 422)
        self.assertIn("ISA", data["error"])


class IncomingMdn(MockServerCase):
    def test_a_receipt_for_something_we_sent_is_recorded(self):
        body = ("Reporting-UA: partner\r\n"
                "Original-Message-ID: <1.997@MOCKEDI>\r\n"
                "Received-Content-MIC: abc=, sha256\r\n"
                "Disposition: automatic-action/MDN-sent-automatically; processed\r\n")
        status, _h, data = self.post(
            "/as2/mdn", body,
            headers={"AS2-From": ACME, "AS2-To": "MOCKEDI",
                     "Content-Type": "multipart/report"})
        self.assertEqual(status, 200)
        self.assertTrue(data["recorded"])
        _s, _h, rows = self.get("/_mock/mdns")
        self.assertEqual(rows[0]["direction"], "in")
        self.assertEqual(rows[0]["original_id"], "<1.997@MOCKEDI>")


class MicAlgorithms(unittest.TestCase):
    def test_each_named_digest_is_used(self):
        payload = b"ISA*00*~"
        for name, function in (("sha1", hashlib.sha1), ("sha256", hashlib.sha256),
                               ("sha512", hashlib.sha512)):
            self.assertEqual(
                as2.mic(payload, name),
                base64.b64encode(function(payload).digest()).decode())

    def test_an_unknown_digest_falls_back_rather_than_failing(self):
        self.assertEqual(as2.mic(b"x", "whirlpool"), as2.mic(b"x", "sha1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
