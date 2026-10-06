"""The syntax a partner is answered in, and so what its documents can carry.

Whatever an ORDERS declared in `UNB` S001, the mock answered `UNOC:3`. It was
circular rather than decided: `charsets.for_outbound` read the syntax off the
UNB of the payload the mock had just written, and `edifact.wrap` wrote `UNOC`
by default, so the answer was always the question the mock had asked itself
(#263).

It matters in two directions. A partner that declared a restricted set may not
be able to read what it is sent. And a partner that declared `UNOY` - UTF-8 -
is told it cannot have what it asked for: since #199 a character the declared
set cannot carry is substituted, so a buyer in `Łódź` got `?ód?` back from a
mock answering in `UNOC`, when its own translator could have taken the name
whole. That loss was entirely the mock's choice.

So the syntax is a field on the partner, defaulting to `UNOC` so nothing
changes for anyone who does not set one. **Configured, not mirrored from the
last inbound interchange:** a partner the mock has only ever sent to has
nothing to mirror, and an answer that depends on which document arrived first
is the opposite of what #280 bought.

`UNOA` is offered. It was excluded twice before, for two different reasons,
and both are gone: the mock could not hold a document to level A (#295), and
then what it would have written was a row of question marks rather than
words (#263, answered: level A folds case). A level A partner whose *id* has
lower case is still refused, because an id is a key and not prose. All of
that is in `tests/test_unoa_repertoire.py`.

Sources for the identifiers: GEFEG's JWG1 service code lists and
edifactory.de's data element 0001, which agree word for word.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import charsets, edifact, partners
from mockedi.envelope import seg

from support import EURODIS, MockServerCase, edifact_order

CITY = "Łódź"            # Łódź


def unb(payload):
    """The UNB of a rendered interchange, UNA or no UNA."""
    for segment in payload.replace("\n", "").split("'"):
        if segment.startswith("UNB"):
            return segment
    return ""


class WhatTheMockAnswersIn(MockServerCase):
    """The partner's field, on the wire."""

    def answer_for(self, syntax):
        status, _headers, body = self.patch(
            "/_mock/partners/" + EURODIS, {"syntax": syntax})
        self.assertEqual(status, 200, body)
        self.send(edifact_order("SYN-" + syntax),
                  {"Content-Type": "application/edifact"})
        self.post("/_mock/advance?all")
        rows = self.mailbox(EURODIS, leave=False)
        self.assertTrue(rows, "nothing was written for %s" % syntax)
        return [unb(row["payload"]) for row in rows]

    def test_each_syntax_reaches_the_envelope(self):
        for syntax in ("UNOC", "UNOB", "UNOY", "UNOD"):
            with self.subTest(syntax):
                for header in self.answer_for(syntax):
                    self.assertTrue(header.startswith("UNB+%s:3+" % syntax),
                                    header)

    def test_the_default_is_unoc_and_nothing_need_be_set(self):
        _s, _h, partner = self.get("/_mock/partners/" + EURODIS)
        self.assertEqual(partner["syntax"], "UNOC")
        for header in self.answer_for("UNOC"):
            self.assertTrue(header.startswith("UNB+UNOC:3+"), header)

    def test_an_x12_partner_is_unaffected(self):
        from support import ACME, x12_order
        self.send(x12_order("SYN-X12"))
        self.post("/_mock/advance?all")
        for row in self.mailbox(ACME, leave=False):
            self.assertTrue(row["payload"].lstrip().startswith("ISA*"))


class WhatEachSyntaxCanCarry(unittest.TestCase):
    """The point of the field: `UNOY` keeps what `UNOC` substitutes.

    At the layer the substitution happens, because the business path overwrites
    a partner's description from the catalogue and would test the catalogue
    rather than this.
    """

    def rendered(self, syntax):
        message = edifact.message(
            "ORDERS", "1", [seg("NAD", "BY", ["EURODIS", "", "91"], "",
                                "Eurodis", "", CITY)], "D:96A:UN")
        return edifact.render(edifact.wrap([message], "MOCKEDI", "EURODIS",
                                           "1", syntax=syntax))

    def test_unoc_cannot_carry_it(self):
        # iso-8859-1 has ó and neither Ł nor ź.
        written = self.rendered("UNOC")
        self.assertIn("UNB+UNOC:3+", written)
        self.assertIn("?ód?", written)
        self.assertNotIn(CITY, written)

    def test_unoy_carries_it_whole(self):
        written = self.rendered("UNOY")
        self.assertIn("UNB+UNOY:3+", written)
        self.assertIn(CITY, written)

    def test_unob_carries_none_of_it(self):
        # Level B is ISO 646: no accents at all.
        written = self.rendered("UNOB")
        self.assertIn("UNB+UNOB:3+", written)
        self.assertIn("???d?", written)


class AFileFromBeforeTheColumn(unittest.TestCase):
    """Schema 14 adds `partner.syntax`; an older file gains it on upgrade.

    The column has a default, so `db.upgrade`'s derived step covers it - but a
    partner that existed before the column has to end up `UNOC` rather than
    empty, because an empty syntax would reach `edifact.wrap`.
    """

    def test_an_older_partner_comes_back_as_unoc(self):
        import sqlite3
        import tempfile
        from mockedi import db
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "old.db")
            conn = sqlite3.connect(path)
            try:
                conn.executescript(db.SCHEMA.replace(
                    "    syntax       TEXT NOT NULL DEFAULT 'UNOC',\n", ""))
                conn.execute(
                    "INSERT INTO partner (id, name, dialect, version)"
                    " VALUES ('OLDIS', 'Oldis', 'EDIFACT', 'D:96A:UN')")
                conn.execute("PRAGMA user_version = 13")
                conn.commit()
                self.assertNotIn("syntax", {row[1] for row in conn.execute(
                    "PRAGMA table_info(partner)")})
                added = db.upgrade(conn, path)
                self.assertIn("partner.syntax", added)
                row = conn.execute(
                    "SELECT syntax FROM partner WHERE id = 'OLDIS'").fetchone()
                self.assertEqual(row[0], "UNOC")
            finally:
                conn.close()


class WhatAPartnerMayBeSetTo(MockServerCase):

    def test_every_syntax_charsets_has_a_codec_for(self):
        # UNOA joined them once #263 settled that level A folds case. It was
        # excluded twice before that, for two different reasons, and both are
        # gone: the mock can hold a document to level A (#295) and what it
        # writes is now legible rather than a row of question marks.
        self.assertEqual(set(partners.SYNTAXES), set(charsets.EDIFACT_SYNTAX))
        self.assertEqual(partners.UNFAITHFUL, {})

    def test_unoa_is_offered_and_folds(self):
        status, _headers, body = self.patch(
            "/_mock/partners/" + EURODIS, {"syntax": "UNOA"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["syntax"], "UNOA")

    def test_but_not_for_a_partner_whose_id_has_lower_case(self):
        # An id is a key, not prose: see tests/test_unoa_repertoire.py.
        self.post("/_mock/partners", {
            "id": "lowdis", "name": "Lowdis", "dialect": "EDIFACT",
            "version": "D:96A:UN", "role": "customer"})
        status, _headers, body = self.patch("/_mock/partners/lowdis",
                                            {"syntax": "UNOA"})
        self.assertEqual(status, 400, body)
        self.assertIn("wrong for an id", body["error"])

    def test_an_unknown_identifier_is_refused_with_the_list(self):
        status, _headers, body = self.patch(
            "/_mock/partners/" + EURODIS, {"syntax": "UNOZ"})
        self.assertEqual(status, 400, body)
        self.assertIn("unknown syntax identifier", body["error"])
        self.assertIn("UNOY", body["error"])

    def test_it_is_not_a_number(self):
        status, _headers, body = self.patch(
            "/_mock/partners/" + EURODIS, {"syntax": 3})
        self.assertEqual(status, 400, body)
        self.assertIn("must be a string", body["error"])

    def test_a_partner_can_be_created_with_one(self):
        status, _headers, body = self.post("/_mock/partners", {
            "id": "NORDIS", "name": "Nordis", "dialect": "EDIFACT",
            "version": "D:96A:UN", "syntax": "UNOY", "role": "customer"})
        self.assertEqual(status, 201, body)
        self.assertEqual(body["syntax"], "UNOY")


if __name__ == "__main__":
    unittest.main()
