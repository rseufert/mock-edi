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

from mockedi.envelope import EDIFACT_DEFAULTS, split_elements

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


# Ł and ź are not in ISO 8859-1; ó is. A Polish partner in a UNOC interchange
# is the plainest case of a name the declared charset cannot carry.
POLISH = {"id": "POLDIS", "name": "Poldis Sp. z o.o.", "dialect": "EDIFACT",
          "street": "Piotrkowska 1", "city": "\u0141\u00f3d\u017a",
          "region": "LD", "postal": "90-001", "country": "PL"}


class WhatTheCharsetCannotCarry(CharsetCase):
    """It is substituted, and the substitute is escaped (#199).

    `?` is EDIFACT's release character. Substituting it after the segment had
    been rendered left it escaping the separator that followed: a reader got
    eight elements where nine were written, with the region swallowed into the
    city, the postcode in the region's place and the country gone. The data a
    charset cannot carry is lost either way - that is what a downgrade is -
    but the structure must survive, or the loss spreads to every element after
    it.
    """

    def setUp(self):
        super().setUp()
        status, _headers, data = self.post("/_mock/partners", POLISH)
        self.assertEqual(status, 201, data)

    def nad(self, code="ORDRSP"):
        """The Polish party's NAD from a document the mock wrote.

        Found by its postcode rather than by its qualifier, because the party
        this partner is differs by document - `BY` on an ORDRSP, the delivery
        party on a DESADV - and the test is about the address, not the role.
        """
        body = self.collected("POLDIS", code)
        for chunk in body.split(b"'"):
            segment = chunk.lstrip(b"\r\n")
            if segment.startswith(b"NAD") and b"90-001" in segment:
                text = segment.decode("iso-8859-1")
                return text, split_elements(text, EDIFACT_DEFAULTS)
        self.fail("no NAD with the postcode in the %s: %r" % (code, body[:300]))

    def order(self):
        self.send(edifact_order("PO-CHARSET", sender="POLDIS"),
                  headers={"Content-Type": EDIFACT})

    def test_the_segment_keeps_every_element_it_was_written_with(self):
        self.order()
        _text, elements = self.nad()
        self.assertEqual(len(elements), 9)

    def test_and_each_one_is_still_the_element_it_was(self):
        self.order()
        _text, elements = self.nad()
        self.assertEqual(elements[6], "LD")
        self.assertEqual(elements[7], "90-001")
        self.assertEqual(elements[8], "PL")

    def test_the_city_reads_as_a_name_and_not_as_an_escape(self):
        """It read `?\u00f3d?`, a literal question mark for each letter UNOC
        lacks. Those letters are now said plainly (#264): the `\u00f3` UNOC
        has is untouched, and nothing is left to be mistaken for a release
        character. What still cannot be said at all is held, with the
        doubling, in `test_unoa_repertoire`."""
        self.order()
        text, elements = self.nad()
        self.assertEqual(elements[5], "L\u00f3dz")
        self.assertNotIn("?", text)

    def test_every_document_that_names_this_partner_is_the_same(self):
        # Not the DESADV: its delivery party is the order's ship-to, which
        # this fixture sets to a German warehouse, so the Polish address is
        # not in it to be mangled.
        self.order()
        self.post("/_mock/advance?all")
        for code in ("ORDRSP", "INVOIC"):
            with self.subTest(code=code):
                _text, elements = self.nad(code)
                self.assertEqual(len(elements), 9)
                self.assertEqual(elements[8], "PL")

    def test_the_bytes_are_the_charset_the_envelope_declares(self):
        self.order()
        body = self.collected("POLDIS", "ORDRSP")
        self.assertIn(b"UNB+UNOC", body)
        # Nothing is left for the encoder to replace: the document already
        # holds only what UNOC can carry, so a strict encode of it succeeds.
        body.decode("iso-8859-1").encode("iso-8859-1")

    def test_a_name_the_charset_does_carry_is_untouched(self):
        # The guard on the substitution: ó survives, and so does Müller.
        self.order()
        _text, elements = self.nad()
        self.assertIn("\u00f3", elements[5])


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
