"""Documents in the character set they declare, and the bytes that arrived.

Real EDI is mostly not UTF-8. `UNOC` - the syntax identifier the mock itself
writes - is ISO 8859-1, and X12 from a Windows translator is usually Latin-1
or cp1252 with nothing in the envelope to say so. Every inbound payload used
to be decoded as UTF-8: `Müller` became `M�ller`, and the archive stopped
holding the bytes that were received.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order

NAME = "Müller Lager 1"                 # Müller, with a real u-umlaut
EDIFACT = "application/edifact"


class CharsetCase(MockServerCase):
    def post_bytes(self, body, content_type):
        status, _headers, data = self.post("/edi", body,
                                           headers={"Content-Type": content_type})
        self.assertEqual(status, 200, data)
        return data

    def archived(self):
        """The newest inbound interchange, as `?raw` returns it."""
        _s, _h, rows = self.get("/_mock/interchanges")
        inbound = [row for row in rows if row["direction"] == "in"][0]
        _s, headers, body = self.get("/_mock/interchanges/%d?raw" % inbound["id"],
                                     raw=True)
        return body, headers["Content-Type"]

    def collected(self, partner, code):
        """What the mock would send for `code`, as bytes on the wire."""
        rows = [r for r in self.mailbox(partner) if r["code"] == code]
        self.assertEqual(len(rows), 1)
        _s, _h, body = self.get("/_mock/mailbox?leave&raw&partner=%s&kind=%s"
                                % (partner, rows[0]["kind"]), raw=True)
        return body


class Latin1Edifact(CharsetCase):
    """UNOC is ISO 8859-1 by definition."""

    def setUp(self):
        super().setUp()
        self.sent = edifact_order("PO-UNOC").replace(
            "Eurodis Lager 1", NAME).encode("iso-8859-1")
        self.post_bytes(self.sent, EDIFACT)

    def test_the_archive_returns_exactly_the_bytes_received(self):
        body, content_type = self.archived()
        self.assertEqual(body, self.sent)
        self.assertEqual(content_type, "application/edifact; charset=iso-8859-1")

    def test_the_party_name_is_read_correctly(self):
        self.assertEqual(self.order("PO-UNOC")["ship_to_name"], NAME)

    def test_and_written_back_in_the_charset_the_mock_declares(self):
        body = self.collected(EURODIS, "DESADV")
        self.assertIn(b"UNB+UNOC:3", body)
        self.assertIn(NAME.encode("iso-8859-1"), body)
        self.assertNotIn(NAME.encode("utf-8"), body)


class Utf8Edifact(CharsetCase):
    """UNOY is UTF-8, and says so."""

    def test_a_unoy_interchange_is_read_as_utf8(self):
        sent = edifact_order("PO-UNOY").replace("UNOC", "UNOY", 1).replace(
            "Eurodis Lager 1", NAME).encode("utf-8")
        self.post_bytes(sent, EDIFACT)
        self.assertEqual(self.order("PO-UNOY")["ship_to_name"], NAME)
        body, _content_type = self.archived()
        self.assertEqual(body, sent)


class X12Charsets(CharsetCase):
    """X12 declares nothing: HTTP's charset if given, else ISO 8859-1."""

    def order_bytes(self, po_number, encoding):
        return x12_order(po_number).replace("Acme DC 4", NAME).encode(encoding)

    def test_undeclared_is_read_as_latin1_and_answered_in_it(self):
        sent = self.order_bytes("PO-X-LATIN", "iso-8859-1")
        self.post_bytes(sent, "application/edi-x12")
        self.assertEqual(self.order("PO-X-LATIN")["ship_to_name"], NAME)
        self.assertEqual(self.archived()[0], sent)
        self.assertIn(NAME.encode("iso-8859-1"), self.collected(ACME, "855"))

    def test_a_utf8_charset_on_the_request_is_honoured_both_ways(self):
        sent = self.order_bytes("PO-X-UTF8", "utf-8")
        self.post_bytes(sent, "application/edi-x12; charset=utf-8")
        self.assertEqual(self.order("PO-X-UTF8")["ship_to_name"], NAME)
        self.assertEqual(self.archived()[0], sent)
        # The 855 goes back in what the 850 came in.
        self.assertIn(NAME.encode("utf-8"), self.collected(ACME, "855"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
