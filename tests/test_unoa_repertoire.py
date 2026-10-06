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

The result is legal and not pretty — `Widget Co` goes out as `W?????????? C??`
— because a real level A sender folds case instead, which changes data rather
than encoding and is the question still open on #263. This file is the
argument for answering it.
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

    def test_lower_case_goes_where_the_repertoire_excludes_it(self):
        # One `?` per excluded letter: "idget" and "o" are six of them.
        self.assertEqual(fit("Widget Co", "ascii", charsets.LOWER_CASE),
                         "W????? C?")

    def test_and_stays_where_it_does_not(self):
        self.assertEqual(fit("Widget Co", "ascii"), "Widget Co")

    def test_upper_case_digits_and_punctuation_are_untouched(self):
        kept = "ACME-001 / 42 (A.B) = 'X'"
        self.assertEqual(fit(kept, "ascii", charsets.LOWER_CASE), kept)

    def test_the_codec_still_substitutes_what_it_cannot_carry(self):
        # Both exclusions at once: an accented capital the codec refuses and
        # a lower-case letter the repertoire does.
        self.assertEqual(fit("Éa", "ascii", charsets.LOWER_CASE), "??")

    def test_a_character_the_repertoire_excludes_cannot_escape_a_separator(self):
        """The #199 property, one level down, and the reason the wire looks odd.

        `fit` substitutes first and `escape` runs after, so each `?` the
        repertoire produced is doubled into a literal `?`: six substitutions
        become twelve characters. Had the order been the other way round,
        those `?` would have been release characters and a conforming reader
        would have lost the separators after them - which is exactly what
        #199 fixed for the codec and what this inherits for free by
        substituting in the same place.
        """
        self.assertEqual(fit("Widget Co", "ascii", charsets.LOWER_CASE),
                         "W????? C?")
        self.assertIn("W?????????? C??", rendered("UNOA", "Widget Co"))


class WhatEachSyntaxSends(unittest.TestCase):

    def test_level_a_substitutes_lower_case(self):
        self.assertEqual(rendered("UNOA", "Widget Co", "", "Lodz"),
                         "NAD+BY+ACME::91++W?????????? C??++L??????")

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

    def test_a_lower_case_partner_id_is_fitted_in_the_unb(self):
        self.assertIn("UNB+UNOA:3+MOCKEDI:ZZ+????????-????:ZZ",
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


class WhyLevelAIsStillRefusedOnAPartner(MockServerCase):
    """The mechanism works; what it produces is the reason it is not offered.

    #296 refused `UNOA` because the mock could not hold a document to level
    A. It can now. The refusal stays because of what level A then *writes* -
    every lower-case letter substituted - until #263 decides whether a level
    A answer folds case instead. Switching it on and changing it later would
    surprise anyone who built against the substitution twice.

    The envelope test above is the sharper argument: a partner id of
    `acme-dc` becomes `????????-????`, which is not a partner anything can
    route to. Whenever level A is offered, a lower-case partner id will want
    refusing rather than substituting, and that is a decision for whichever
    change offers it.
    """

    def test_the_refusal_says_what_it_would_do(self):
        status, _headers, body = self.patch("/_mock/partners/" + EURODIS,
                                            {"syntax": "UNOA"})
        self.assertEqual(status, 400, body)
        self.assertIn("could write it", body["error"])
        self.assertIn("substitute every lower-case letter", body["error"])
        self.assertIn("#263", body["error"])

    def test_and_every_other_identifier_is_offered(self):
        for syntax in ("UNOB", "UNOC", "UNOY"):
            with self.subTest(syntax):
                status, _headers, body = self.patch(
                    "/_mock/partners/" + EURODIS, {"syntax": syntax})
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
