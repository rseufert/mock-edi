"""The asynchronous MDN, as a MIME entity rather than as a blob of text.

The synchronous MDN goes out with the headers `as2.build_mdn` produced; the
asynchronous one was posted with headers rebuilt by hand, which lost the
boundary. A `multipart/*` without a `boundary` parameter cannot be parsed by
any MIME library, so the partner logs a malformed MDN and the message stays
unacknowledged on their side - the opposite of what `Receipt-Delivery-Option`
asked for.

These tests parse what the mock posted with the standard library's `email`
package, which is what a real listener does and what the old assertions on
substrings of the body never did.
"""
import email
import os
import sys
import threading
import unittest
from http.server import HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import as2

from support import ACME, MockServerCase, as2_headers, x12_order
from test_delivery import Listener


def as_mime(posted):
    """The posted MDN reassembled from its headers and body, as `email` sees it."""
    headers = "".join("%s: %s\r\n" % (name, posted["headers"][name])
                      for name in posted["names"])
    return email.message_from_string(headers + "\r\n" + posted["body"])


class AnAsynchronousMdn(MockServerCase):
    """What the partner's listener actually receives."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.listener = HTTPServer(("127.0.0.1", 0), Listener)
        cls.listener_port = cls.listener.server_address[1]
        cls.listener_thread = threading.Thread(
            target=cls.listener.serve_forever, daemon=True)
        cls.listener_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.listener.shutdown()
        cls.listener.server_close()
        super().tearDownClass()

    def post(self, po_number="PO-ASYNC", **kwargs):
        Listener.received = []
        url = "http://127.0.0.1:%d/mdn" % self.listener_port
        status, _headers, _body = self.request(
            "POST", "/as2", x12_order(po_number),
            headers=as2_headers(async_url=url, **kwargs), raw=True)
        self.assertEqual(status, 202)
        self.httpd.mock.courier.drain(20.0)
        self.assertEqual(len(Listener.received), 1)
        return Listener.received[0]

    def test_it_parses_as_a_multipart_report(self):
        message = as_mime(self.post("PO-MIME"))
        self.assertTrue(message.is_multipart(),
                        "a MIME library cannot read what the mock posted")
        self.assertEqual(message.get_content_type(), "multipart/report")

    def test_the_content_type_carries_the_boundary(self):
        posted = self.post("PO-BOUNDARY")
        boundary = as_mime(posted).get_boundary()
        self.assertTrue(boundary, "multipart/report without a boundary parameter")
        self.assertIn("--" + boundary, posted["body"])

    def test_it_is_a_disposition_notification_report(self):
        message = as_mime(self.post("PO-REPORT"))
        self.assertEqual(message.get_param("report-type"),
                         "disposition-notification")

    def test_both_parts_are_there_and_readable(self):
        message = as_mime(self.post("PO-PARTS"))
        types = [part.get_content_type() for part in message.get_payload()]
        self.assertEqual(types, ["text/plain", "message/disposition-notification"])

    def test_the_machine_part_still_says_what_happened(self):
        message = as_mime(self.post("PO-MACHINE"))
        # `email` reads a message/disposition-notification as an embedded
        # message rather than as text, which it can only do because the entity
        # parses at all now.
        machine = str(message.get_payload()[1].get_payload()[0])
        self.assertIn("Disposition: automatic-action", machine)
        self.assertIn("Received-Content-MIC:", machine)

    def test_the_headers_a_partner_needs_are_all_sent(self):
        posted = self.post("PO-HEADERS")
        names = {name.lower() for name in posted["names"]}
        for required in ("content-type", "message-id", "as2-from", "as2-to",
                         "as2-version", "date", "mime-version"):
            self.assertIn(required, names)

    def test_the_message_id_is_the_one_the_mock_recorded(self):
        posted = self.post("PO-ID")
        _status, _headers, rows = self.get("/_mock/mdns")
        sent = [row for row in rows if row["status"] == "sent"]
        self.assertEqual(posted["headers"]["Message-ID"], sent[0]["message_id"])


class AnMdnWrittenByAnOlderVersion(unittest.TestCase):
    """A file database may hold a pending MDN from before the headers were kept.

    Its boundary is in its body and nowhere else, so it is read back from
    there. Nothing is guessed: an MDN that cannot say what its boundary is
    would be as unparsable as the ones this change is about.
    """

    def row(self, payload, headers=""):
        return {"headers": headers, "payload": payload, "partner": ACME,
                "message_id": "<m1@mock.example>"}

    def test_the_boundary_is_recovered_from_the_body(self):
        from mockedi.delivery import _mdn_headers
        payload = ("This is a multi-part message in MIME format.\r\n"
                   "\r\n--=_MDN_abc123\r\n"
                   "Content-Type: text/plain; charset=us-ascii\r\n")
        headers = _mdn_headers(self.row(payload), "MOCKEDI")
        self.assertIn('boundary="=_MDN_abc123"', headers["Content-Type"])

    def test_stored_headers_win_when_they_are_there(self):
        from mockedi.delivery import _mdn_headers
        headers = _mdn_headers(
            self.row("--=_MDN_ignored\r\n",
                     '{"Content-Type": "multipart/report; boundary=\\"kept\\""}'),
            "MOCKEDI")
        self.assertIn('boundary="kept"', headers["Content-Type"])


class TheTextPartDeclaresWhatItIs(unittest.TestCase):
    """The charset in the header has to match the bytes under it."""

    def build(self, sender):
        inbound = as2.Inbound(sender=sender, receiver="MOCKEDI",
                              message_id="<m@x.example>",
                              notify_to="edi@example.test")
        return as2.build_mdn(inbound, b"ISA*00*~", "MOCKEDI", as2.PROCESSED,
                             "", user_agent="mock-edi")

    def test_an_ascii_explanation_is_seven_bit(self):
        headers, payload = self.build("ACME")
        message = email.message_from_string(
            "Content-Type: %s\r\n\r\n" % headers["Content-Type"]
            + payload.decode("utf-8"))
        text = message.get_payload()[0]
        self.assertEqual(text.get_content_charset(), "us-ascii")
        self.assertEqual(text.get("Content-Transfer-Encoding"), "7bit")

    def test_a_non_ascii_explanation_is_not_called_us_ascii(self):
        # A partner id with a diaeresis in it puts 8-bit bytes in the part.
        # Declaring them us-ascii and 7bit is a lie a strict client acts on.
        headers, payload = self.build("KÖLN-EDI")
        message = email.message_from_string(
            "Content-Type: %s\r\n\r\n" % headers["Content-Type"]
            + payload.decode("utf-8"))
        text = message.get_payload()[0]
        self.assertEqual(text.get_content_charset(), "utf-8")
        self.assertEqual(text.get("Content-Transfer-Encoding"), "8bit")
        self.assertIn("KÖLN-EDI", text.get_payload())


class ASignedReceiptIsRefused(MockServerCase):
    """The mock does not sign, and says so rather than pretending.

    RFC 4130 is explicit: a receiver that cannot produce the signed receipt
    the sender required answers with a failure, not with an unsigned success.
    Refusing what it cannot do is this project's policy for S/MIME generally.
    """

    def required(self, micalg="sha256"):
        return ("signed-receipt-protocol=required, pkcs7-signature; "
                "signed-receipt-micalg=required, %s" % micalg)

    def test_a_required_signed_receipt_gets_a_failed_mdn(self):
        headers = as2_headers()
        headers["Disposition-Notification-Options"] = self.required()
        status, _h, body = self.request("POST", "/as2", x12_order("PO-SIGNED"),
                                        headers=headers, raw=True)
        self.assertEqual(status, 200)
        text = body.decode("utf-8", "replace")
        self.assertIn("failed/Failure", text)
        self.assertNotIn("; processed", text)

    def test_the_failure_says_why(self):
        headers = as2_headers()
        headers["Disposition-Notification-Options"] = self.required()
        _s, _h, body = self.request("POST", "/as2", x12_order("PO-SIGNED-WHY"),
                                    headers=headers, raw=True)
        self.assertIn("sign", body.decode("utf-8", "replace").lower())

    def test_an_optional_signed_receipt_is_still_processed(self):
        status, _h, body = self.request("POST", "/as2", x12_order("PO-OPTIONAL"),
                                        headers=as2_headers(), raw=True)
        self.assertEqual(status, 200)
        self.assertIn("; processed", body.decode("utf-8", "replace"))

    def test_the_order_is_not_acted_on_when_the_receipt_is_refused(self):
        headers = as2_headers()
        headers["Disposition-Notification-Options"] = self.required()
        self.request("POST", "/as2", x12_order("PO-SIGNED-IGNORED"),
                     headers=headers, raw=True)
        status, _headers, _data = self.get("/_mock/orders/PO-SIGNED-IGNORED")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
