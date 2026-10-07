"""What level A permits, which is not what its codec permits (#295).

`charsets` mapped a syntax identifier to a **codec**, and for `UNOA` that was
wrong in a way no codec can fix. Two published copies of the service
directory give level A as:

> "As defined in the basic code table of ISO 646 with the exceptions of
> **lower case letters**, alternative graphic character allocations and
> national or application-oriented graphic character allocations."

— GEFEG's JWG1 service code lists and edifactory.de's data element 0001, word
for word. Level B is the same without the lower-case exception. So the two
differ by exactly lower case, and `ascii` is the right **codec** for both and
the wrong **repertoire** for one: a codec turns characters into bytes and
fails or substitutes, where a repertoire is a set of permitted characters and
has to be checked. There is no codec for "ISO 646 minus lower case", which is
why this needed a concept beside the codec rather than a corrected value.

What is enforced here is the lower-case exclusion, which both sources state.
The twelve ISO 646 positions open to national substitution are also excluded
by that definition and are **not** enforced: the only source I could reach
for *which* twelve is one, and refusing a character level A permits is the
worse of the two errors. The gap is stated rather than guessed at.

A real level A sender **folds** lower case rather than losing it, and that is
what Zack decided on #263: `Widget Co` goes out as `WIDGET CO`. So the
repertoire states what level A permits and folding is what the mock does
about it — the two are kept apart on purpose, because the first is a fact
about the standard and the second is a choice about behaviour.

Folding is `a`–`z` to `A`–`Z` and **not** `str.upper()`. Level A's alphabet is
A–Z, so that is the whole mapping; and `upper()` would turn `ß` into `SS`,
two characters where there was one, so a value at an element's maximum would
grow past it and the mock would write a document its own dictionary reports.
Anything outside ISO 646 is substituted whether folded or not.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import charsets, edifact
from mockedi.envelope import fit, seg

from support import EURODIS, MockServerCase


def rendered(syntax, *values):
    message = edifact.message(
        "ORDERS", "1", [seg("NAD", "BY", ["ACME", "", "91"], "", *values)],
        "D:96A:UN")
    out = edifact.render(edifact.wrap([message], "MOCKEDI", "ACME", "1",
                                      syntax=syntax))
    return [line for line in out.replace("\n", "").split("'")
            if line.startswith("NAD")][0]


class TheRepertoireBesideTheCodec(unittest.TestCase):

    def test_only_level_a_excludes_anything_its_codec_allows(self):
        self.assertEqual(charsets.outside("UNOA"), charsets.LOWER_CASE)
        for syntax in set(charsets.EDIFACT_SYNTAX) - {"UNOA"}:
            with self.subTest(syntax):
                self.assertEqual(charsets.outside(syntax), "")

    def test_an_unknown_identifier_excludes_nothing(self):
        self.assertEqual(charsets.outside("UNOZ"), "")

    def test_the_exclusion_is_the_twenty_six_letters(self):
        self.assertEqual(sorted(charsets.LOWER_CASE),
                         [chr(c) for c in range(ord("a"), ord("z") + 1)])


class WhatFitDoesWithIt(unittest.TestCase):
    """`fit` is the one place substitution happens, and it stays that way."""

    def test_folding_is_what_level_a_actually_does(self):
        self.assertEqual(
            fit("Widget Co", "ascii", charsets.LOWER_CASE, fold=True),
            "WIDGET CO")

    def test_and_without_folding_the_repertoire_substitutes(self):
        # Which is what the repertoire alone means, and is kept because the
        # repertoire is the statement of what level A permits.
        self.assertEqual(fit("Widget Co", "ascii", charsets.LOWER_CASE),
                         "W????? C?")

    def test_folding_never_changes_the_length(self):
        # `str.upper()` would: "Straße" becomes "STRASSE", a character longer,
        # and a value at an element's maximum would grow past it. Nor does
        # transliteration, unless it is told the element has room (#264).
        for value in ("Widget Co", "Stra\u00dfe", "\u0131stanbul", "L\u00f3d\u017a"):
            with self.subTest(value):
                self.assertEqual(
                    len(fit(value, "ascii", charsets.LOWER_CASE, fold=True)),
                    len(value))

    def test_what_folding_leaves_for_the_codec(self):
        # The sharp case: ß folds to nothing, so the codec substitutes it
        # rather than expanding it to SS - unless the element is known to
        # have room for the second S (#264).
        self.assertEqual(fit("Stra\u00dfe", "ascii", charsets.LOWER_CASE,
                             fold=True), "STRA?E")
        self.assertEqual(fit("Stra\u00dfe", "ascii", charsets.LOWER_CASE,
                             fold=True, limit=35), "STRASSE")

    def test_and_stays_where_it_does_not(self):
        self.assertEqual(fit("Widget Co", "ascii"), "Widget Co")

    def test_upper_case_digits_and_punctuation_are_untouched(self):
        kept = "ACME-001 / 42 (A.B) = 'X'"
        self.assertEqual(fit(kept, "ascii", charsets.LOWER_CASE), kept)

    def test_the_codec_still_substitutes_what_it_cannot_carry(self):
        # Both exclusions at once: a character the codec refuses and has no
        # plain letters for, and a lower-case letter the repertoire refuses.
        self.assertEqual(fit("\u20aca", "ascii", charsets.LOWER_CASE), "??")
        # An accented capital is no longer one of those: it is said without
        # its accent (#264), and the lower case beside it still is not.
        self.assertEqual(fit("\u00c9a", "ascii", charsets.LOWER_CASE), "E?")

    def test_a_substituted_character_cannot_escape_a_separator(self):
        """The #199 property, and the reason it still matters after folding.

        Folding removes the lower case and transliteration the accents
        (#264), so under level A what is left to substitute is what has no
        Latin letter to be: a euro sign, a Greek word. `fit` substitutes
        before `escape` runs, so each `?` is doubled into a literal one. Had
        the order been the other way round those `?` would have been release
        characters and a conforming reader would have lost the separator
        after each - exactly what #199 fixed for the codec, inherited here
        rather than rebuilt.
        """
        folded = fit("\u20ac5 \u0391\u03b8", "ascii", charsets.LOWER_CASE,
                     fold=True)
        self.assertEqual(folded, "?5 ??")
        # Three substitutions, each doubled on the wire.
        self.assertIn("??5 ????", rendered("UNOA", "\u20ac5 \u0391\u03b8"))
        # And the name that used to be the example is now a name.
        self.assertEqual(fit("\u0141\u00f3d\u017a", "ascii",
                             charsets.LOWER_CASE, fold=True), "LODZ")


class WhatEachSyntaxSends(unittest.TestCase):

    def test_level_a_folds_lower_case(self):
        self.assertEqual(rendered("UNOA", "Widget Co", "", "Lodz"),
                         "NAD+BY+ACME::91++WIDGET CO++LODZ")

    def test_level_b_does_not(self):
        self.assertEqual(rendered("UNOB", "Widget Co", "", "Lodz"),
                         "NAD+BY+ACME::91++Widget Co++Lodz")

    def test_nor_do_the_others(self):
        for syntax in ("UNOC", "UNOD", "UNOY"):
            with self.subTest(syntax):
                self.assertIn("Widget Co", rendered(syntax, "Widget Co"))

    def test_level_a_still_carries_what_it_permits(self):
        self.assertEqual(rendered("UNOA", "WIDGET CO", "", "LODZ"),
                         "NAD+BY+ACME::91++WIDGET CO++LODZ")


class TheEnvelopeIsFittedToo(unittest.TestCase):
    """`UNB` and `UNZ`, not only the body. The senior found these missing.

    The envelope is the worst place to break the repertoire, because `UNB`
    S001 is the segment that *declares* it: a level A interchange whose own
    header carries lower case says one thing and does another. And a partner
    id may legally be lower case - `ID_PATTERN` allows it - so this was
    reachable rather than theoretical.
    """

    def envelope(self, syntax, sender="MOCKEDI", receiver="acme-dc"):
        message = edifact.message("ORDERS", "1", [seg("NAD", "BY", ["X", "", "91"])],
                                  "D:96A:UN")
        out = edifact.render(edifact.wrap([message], sender, receiver, "1",
                                          syntax=syntax))
        return [line for line in out.replace("\n", "").split("'")
                if line.startswith(("UNB", "UNZ"))]

    def test_a_lower_case_partner_id_is_folded_in_the_unb(self):
        # The mechanism reaches the envelope, which is what the senior found
        # missing. A lower-case id cannot be *configured* on a level A
        # partner - see below - so this is the mechanism being right rather
        # than a path the control plane allows.
        self.assertIn("UNB+UNOA:3+MOCKEDI:ZZ+ACME-DC:ZZ",
                      self.envelope("UNOA")[0])

    def test_and_is_not_under_a_syntax_that_carries_it(self):
        self.assertIn("UNB+UNOC:3+MOCKEDI:ZZ+acme-dc:ZZ",
                      self.envelope("UNOC")[0])

    def test_the_syntax_identifier_itself_survives(self):
        # S001 is upper case and punctuation, so it is inside level A - but
        # worth a test, because substituting the thing that declares the
        # repertoire would be the one unrecoverable mistake here.
        self.assertTrue(self.envelope("UNOA")[0].startswith("UNB+UNOA:3+"))

    def test_the_trailer_too(self):
        trailer = self.envelope("UNOA", receiver="acme")[1]
        self.assertEqual(trailer, "UNZ+1+1")


class WhatALevelAPartnerIsSentNow(MockServerCase):
    """Folding is in, so `UNOA` is offered — and its id is not folded.

    #296 refused level A because the mock could not hold a document to it;
    #295 gave it the repertoire; #263 then decided that it folds. With all
    three, a level A partner gets `WIDGET CO` rather than `W????? C?` and
    there is nothing left to refuse.

    Except the id. Folding prose is lossless in the sense that matters -
    `WIDGET CO` is the same description in a smaller alphabet. Folding an
    **identifier** is not: this mock holds ids case-distinctly, so `acme` and
    `ACME` can both be partners, and a folded `UNB` would address a document
    to a party the mock itself cannot tell from another. In real EDI the
    question does not arise, because a level A partner's id is upper case -
    the syntax demands it - so a lower-case one is a misconfiguration and is
    refused rather than folded.
    """

    def test_level_a_is_offered(self):
        status, _headers, body = self.patch("/_mock/partners/" + EURODIS,
                                            {"syntax": "UNOA"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["syntax"], "UNOA")

    def test_and_every_document_it_is_sent_carries_no_lower_case(self):
        from support import edifact_order
        self.patch("/_mock/partners/" + EURODIS, {"syntax": "UNOA"})
        self.send(edifact_order("UNOA-FOLD"),
                  {"Content-Type": "application/edifact"})
        self.post("/_mock/advance?all")
        rows = self.mailbox(EURODIS, leave=False)
        self.assertTrue(rows)
        for row in rows:
            with self.subTest(row["code"]):
                flat = row["payload"].replace("\n", "")
                self.assertIn("UNB+UNOA:3+", flat)
                self.assertEqual(
                    [c for c in flat.split("UNH", 1)[1] if c.islower()], [])

    def test_and_it_is_still_legible(self):
        from support import edifact_order
        self.patch("/_mock/partners/" + EURODIS, {"syntax": "UNOA"})
        self.send(edifact_order("UNOA-READ"),
                  {"Content-Type": "application/edifact"})
        self.post("/_mock/advance?all")
        descriptions = []
        for row in self.mailbox(EURODIS, leave=False):
            for segment in row["payload"].replace("\n", "").split("'"):
                if segment.startswith("IMD"):
                    descriptions.append(segment)
        self.assertTrue(descriptions)
        # The point of folding: a reader recognises this.
        self.assertTrue(any("WIDGET, BLUE, 40MM" in item
                            for item in descriptions), descriptions)

    def test_a_lower_case_id_cannot_be_set_to_level_a(self):
        status, _headers, body = self.post("/_mock/partners", {
            "id": "nordis", "name": "Nordis", "dialect": "EDIFACT",
            "version": "D:96A:UN", "role": "customer"})
        self.assertEqual(status, 201, body)
        status, _headers, body = self.patch("/_mock/partners/nordis",
                                            {"syntax": "UNOA"})
        self.assertEqual(status, 400, body)
        self.assertIn("has lower case", body["error"])
        self.assertIn("'NORDIS'", body["error"])
        self.assertIn("wrong for an id", body["error"])

    def test_nor_created_with_one(self):
        status, _headers, body = self.post("/_mock/partners", {
            "id": "sudis", "name": "Sudis", "dialect": "EDIFACT",
            "version": "D:96A:UN", "role": "customer", "syntax": "UNOA"})
        self.assertEqual(status, 400, body)
        self.assertIn("has lower case", body["error"])

    def test_but_a_lower_case_id_is_fine_on_any_other_syntax(self):
        self.post("/_mock/partners", {
            "id": "westis", "name": "Westis", "dialect": "EDIFACT",
            "version": "D:96A:UN", "role": "customer"})
        for syntax in ("UNOB", "UNOC", "UNOY"):
            with self.subTest(syntax):
                status, _headers, body = self.patch(
                    "/_mock/partners/westis", {"syntax": syntax})
                self.assertEqual(status, 200, body)


class InboundIsUnchanged(unittest.TestCase):
    """The repertoire is enforced on the way out only, and that is stated.

    `charsets.declared` picks a *codec* to decode what arrived, and `ascii`
    is the right codec for level A. So a `UNOA` document carrying lower case
    is decoded without complaint and is **not** reported. Whether it should
    be is a validator question and a separate decision: the sender broke its
    own declared repertoire, which is a syntax error rather than a reading
    problem. Written down here so the asymmetry is a decision and not a
    surprise.
    """

    def test_the_decoder_is_untouched(self):
        self.assertEqual(charsets.declared("EDIFACT", b"UNB+UNOA:3+A+B+260101:1200+1'"),
                         "ascii")

    def test_and_lower_case_in_an_inbound_unoa_document_is_read_as_sent(self):
        payload = ("UNB+UNOA:3+EURODIS+MOCKEDI+260924:1200+1'"
                   "UNH+1+ORDERS:D:96A:UN'BGM+220+lower+9'UNT+3+1'UNZ+1+1'")
        interchange = edifact.parse(payload)
        _group, message = list(interchange.messages())[0]
        bgm = [item for item in message.segments if item.tag == "BGM"][0]
        self.assertEqual(bgm.get(2), "lower")


class TheX12SideIsUntouched(unittest.TestCase):
    """`render_segment` is shared, and X12 passes no repertoire."""

    def test_an_x12_segment_keeps_its_lower_case(self):
        from mockedi import x12
        from mockedi.envelope import render_segment
        rendered_x12 = render_segment(
            seg("PID", "F", "", "", "", "Widget, blue, 40mm"),
            x12.DELIMITERS if hasattr(x12, "DELIMITERS")
            else edifact.EDIFACT_DEFAULTS)
        self.assertIn("Widget, blue, 40mm", rendered_x12)


if __name__ == "__main__":
    unittest.main()
