"""A letter the declared character set cannot carry is said in plain letters (#264).

A buyer in `Łódź` trading in `UNOC` got `?ód?` back: ISO 8859-1 has the `ó`
and neither of the other two, and each was replaced by a question mark. A
real sender does better and so now does the mock - `Lódz` there, `Lodz` in
level B, `LODZ` in level A.

There is no EDIFACT rule for which letters, so the mock follows a published
one, Unicode CLDR's `Latin-ASCII`, which is language-neutral: `Müller` is
`Muller`, never `Mueller`. Zack's choice, on the issue with the sources.

Typographic punctuation follows the same table (#319): a curled apostrophe
is an apostrophe and a dash a hyphen.

Three things are held here beside the letters themselves: a character the
set does carry is not touched; a value never outgrows its element, because
`ß` is `ss`; and what has no plain form is still a `?`.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import charsets, edifact, validate
from mockedi.envelope import fit, seg, transliterate

from support import EURODIS, MockServerCase, edifact_order

LODZ = "Łódź"            # Łódź
EDIFACT = "application/edifact"


def nad(syntax, *values):
    """The NAD of an ORDERS rendered in `syntax`, as it is on the wire."""
    message = edifact.message(
        "ORDERS", "1", [seg("NAD", "BY", ["ACME", "", "91"], "", *values)],
        "D:96A:UN")
    out = edifact.render(edifact.wrap([message], "MOCKEDI", "ACME", "1",
                                      syntax=syntax))
    return [line for line in out.replace("\n", "").split("'")
            if line.startswith("NAD")][0]


class TheLetters(unittest.TestCase):
    """What each becomes in ISO 646, by CLDR's `Latin-ASCII`."""

    def test_a_mark_comes_off_and_the_letter_stays(self):
        for said, plain in (("ü", "u"), ("é", "e"), ("ñ", "n"),
                            ("ź", "z"), ("Å", "A"), ("İ", "I"),
                            ("č", "c"), ("ő", "o")):
            with self.subTest(said):
                self.assertEqual(transliterate(said, "ascii"), plain)

    def test_a_letter_with_no_mark_to_remove_has_its_own_rule(self):
        for said, plain in (("Ł", "L"), ("ł", "l"), ("Ø", "O"),
                            ("ø", "o"), ("Đ", "D"), ("ð", "d"),
                            ("ß", "ss"), ("Æ", "AE"), ("æ", "ae"),
                            ("Œ", "OE"), ("Þ", "TH"), ("þ", "th"),
                            ("ı", "i"), ("ẞ", "SS")):
            with self.subTest(said):
                self.assertEqual(transliterate(said, "ascii"), plain)

    def test_it_is_language_neutral_and_that_is_the_choice(self):
        # Not Mueller, Oere, Aarhus: an envelope does not say which
        # language a name is in.
        self.assertEqual(transliterate("Müller", "ascii"), "Muller")
        self.assertEqual(transliterate("Øre", "ascii"), "Ore")
        self.assertEqual(transliterate("Århus", "ascii"), "Arhus")
        self.assertEqual(transliterate("Hämeenlinna", "ascii"),
                         "Hameenlinna")

    def test_a_letter_and_a_mark_sent_as_two_characters_are_one_letter(self):
        self.assertEqual(transliterate("Lódź", "ascii"), "Lodz")
        self.assertEqual(transliterate("ó", "iso-8859-1"), "ó")

    def test_what_has_no_plain_form_is_left_for_the_question_mark(self):
        for said in ("€", "Αθήνα", "中"):
            with self.subTest(said):
                self.assertEqual(transliterate(said, "ascii"), said)
                self.assertEqual(fit(said, "ascii"), "?" * len(said))

    def test_a_greek_letter_does_not_lose_its_accent_to_become_nothing_better(self):
        # Marks come off Latin letters only: one `?`, not a bare alpha and
        # then a `?`, and not two.
        self.assertEqual(fit("ά", "ascii"), "?")

    def test_plain_text_is_returned_as_it_is(self):
        text = "ACME Distribution Inc / 42 (A.B)"
        self.assertIs(transliterate(text, "ascii"), text)


class WhatTheSetCarriesIsNotTouched(unittest.TestCase):
    """`ü` is `u` only where there is no `ü`."""

    def test_by_character_set(self):
        name = "Müller Łódź Straße"
        for charset, wanted in (
                ("ascii", "Muller Lodz Stra?e"),
                ("iso-8859-1", "Müller Lódz Straße"),
                ("iso-8859-2", name),
                ("utf-8", name)):
            with self.subTest(charset):
                self.assertEqual(fit(name, charset), wanted)

    def test_and_with_no_character_set_nothing_is(self):
        self.assertEqual(fit(LODZ, ""), LODZ)


class OneTestForEachIdentifier(unittest.TestCase):
    """The same city under every syntax identifier the mock writes."""

    WANTED = {"UNOA": "LODZ", "UNOB": "Lodz", "UNOC": "Lódz",
              "UNOD": LODZ, "UNOY": LODZ}

    def test_the_city(self):
        for syntax, city in self.WANTED.items():
            with self.subTest(syntax):
                written = nad(syntax, "Poldis", "Piotrkowska 1", LODZ)
                self.assertTrue(written.endswith("+" + city), written)
                self.assertNotIn("?", written)

    def test_every_identifier_with_a_codec_is_one_of_these_or_carries_latin_2(self):
        # So a new identifier is a decision here and not an accident.
        for syntax, codec in charsets.EDIFACT_SYNTAX.items():
            with self.subTest(syntax):
                written = nad(syntax, "Poldis", "Piotrkowska 1", LODZ)
                self.assertNotIn("?", written)
                written.encode(codec)

    def test_level_a_folds_what_transliteration_gives_it(self):
        self.assertEqual(
            fit("łódź", "ascii", charsets.LOWER_CASE, fold=True),
            "LODZ")

    def test_a_character_with_no_plain_form_is_still_a_doubled_question_mark(self):
        # The #199 property: substituted before the release character is
        # applied, so the `?` is a literal one.
        self.assertIn("+??5", nad("UNOB", "Poldis", "Piotrkowska 1", "€5"))


class ALetterThatBecomesADelimiter(unittest.TestCase):
    """`\u0149` is `'n`, and `'` ends a segment.

    Every earlier path in the mock could turn a character only into `?`.
    This is the first that can turn one that is not a delimiter into one,
    and the only entry of the table that does (held below). It is safe
    because `fit` runs before `escape`: the apostrophe is released like any
    other. Reorder those two and this, not the `?` tests, is what breaks -
    a stray `?` is harmless and a stray `'` cuts the segment in two. Asked
    for by Eddie on the review of #318.
    """

    def test_the_apostrophe_it_brings_is_released(self):
        # Not through `nad`, which cuts at every apostrophe as a naive
        # reader would - and so shows what an unreleased one would cost.
        message = edifact.message(
            "ORDERS", "1", [seg("NAD", "BY", ["ACME", "", "91"], "",
                                "A\u0149B")], "D:96A:UN")
        out = edifact.render(edifact.wrap([message], "MOCKEDI", "ACME", "1",
                                          syntax="UNOB"))
        self.assertIn("NAD+BY+ACME::91++A?'nB'UNT", out.replace("\n", ""))

    def test_and_the_document_reads_back_with_the_segment_whole(self):
        message = edifact.message(
            "ORDERS", "1", [seg("NAD", "BY", ["ACME", "", "91"], "",
                                "A\u0149B", "Street", "City")], "D:96A:UN")
        out = edifact.render(edifact.wrap([message], "MOCKEDI", "ACME", "1",
                                          syntax="UNOB"))
        (read,) = [item for _group, item in edifact.parse(out).messages()]
        (party,) = [item for item in read.segments if item.tag == "NAD"]
        self.assertEqual((party.get(4), party.get(5), party.get(6)),
                         ("A'nB", "Street", "City"))

    def test_it_is_the_only_letter_that_does(self):
        from mockedi.envelope import EDIFACT_DEFAULTS, _LETTERS
        risky = sorted(letter for letter, plain in _LETTERS.items()
                       if set(plain) & set(EDIFACT_DEFAULTS.all))
        self.assertEqual(risky, ["\u0149"])


class TypographicPunctuation(unittest.TestCase):
    """The apostrophe a word processor curls, a dash, a no-break space (#319).

    `O\u2019Brien` read `O?Brien` in every set but UTF-8. The rules are CLDR's
    again, from the same file: its spaces, its quotes and apostrophes and its
    dashes, whole, and the dot leaders.
    """

    def test_what_real_names_and_addresses_contain(self):
        for said, plain in (("O\u2019Brien", "O'Brien"),
                            ("\u2018t Hooft", "'t Hooft"),
                            ("Smith\u2013Jones", "Smith-Jones"),
                            ("A \u2014 B", "A - B"),
                            ("\u201cAcme\u201d", '"Acme"'),
                            ("Rue\u00a0de\u2009Paris", "Rue de Paris"),
                            ("5\u2032 6\u2033", "5' 6\"")):
            with self.subTest(said):
                self.assertEqual(fit(said, "ascii"), plain)

    def test_the_ones_that_grow_need_room_like_any_other(self):
        for said, plain in (("\u201eHaus\u201c", ',,Haus"'),
                            ("\u00abNord\u00bb", "<<Nord>>"),
                            ("Ltd\u2026", "Ltd...")):
            with self.subTest(said):
                self.assertEqual(fit(said, "ascii", limit=35), plain)
                self.assertEqual(len(fit(said, "ascii")), len(said))
                full = said + "x" * (35 - len(said))
                self.assertEqual(len(fit(full, "ascii", limit=35)), 35)

    def test_what_the_set_carries_is_not_touched(self):
        # ISO 8859-1 has the no-break space, the guillemets and the soft
        # hyphen; it has no curly apostrophe and no dash.
        self.assertEqual(fit("\u00abRue\u00a0X\u00bb", "iso-8859-1"),
                         "\u00abRue\u00a0X\u00bb")
        self.assertEqual(fit("O\u2019Brien \u2013 Co", "iso-8859-1"),
                         "O'Brien - Co")
        self.assertEqual(fit("O\u2019Brien \u2013 Co", "utf-8"),
                         "O\u2019Brien \u2013 Co")

    def test_level_a_gets_it_folded_with_the_rest(self):
        self.assertEqual(fit("O\u2019Brien \u0141\u00f3d\u017a", "ascii",
                             charsets.LOWER_CASE, fold=True), "O'BRIEN LODZ")

    def test_nothing_in_the_table_is_a_character_level_a_excludes(self):
        """One CLDR rule is left out for this: U+02CB is a backtick there."""
        from mockedi.envelope import _PUNCTUATION
        national = set("#$@[\\]^`{|}~")
        self.assertNotIn("\u02cb", _PUNCTUATION)
        for said, plain in _PUNCTUATION.items():
            with self.subTest("U+%04X" % ord(said)):
                self.assertFalse(set(plain) & national, plain)
                plain.encode("ascii")
        self.assertEqual(fit("a\u02cbb", "ascii"), "a?b")

    def test_the_table_is_the_size_it_was_copied_at(self):
        # 12 spaces, 24 quotes (25 less the backtick), 12 dashes, 3 leaders.
        from mockedi.envelope import _PUNCTUATION
        self.assertEqual(len(_PUNCTUATION), 51)


class PunctuationThatBecomesADelimiter(unittest.TestCase):
    """Ten of the rules produce `'`, which ends a segment.

    The same hazard as `\u0149`, ten times over and far more often met:
    this is every curled apostrophe in every name. Safe for the same
    reason, `fit` before `escape`, and held the same way.
    """

    def risky(self):
        from mockedi.envelope import EDIFACT_DEFAULTS, _PUNCTUATION
        return {said: plain for said, plain in _PUNCTUATION.items()
                if set(plain) & set(EDIFACT_DEFAULTS.all)}

    def test_which_they_are(self):
        self.assertEqual(sorted("U+%04X" % ord(said) for said in self.risky()),
                         ["U+02B9", "U+02BB", "U+02BC", "U+02BD", "U+02C8",
                          "U+2018", "U+2019", "U+201B", "U+2032", "U+FF07"])
        # An apostrophe every time: no rule produces `+`, `:` or `?`.
        self.assertEqual(set(self.risky().values()), {"'"})

    def test_each_is_released_and_the_segment_reads_back_whole(self):
        for said in self.risky():
            with self.subTest("U+%04X" % ord(said)):
                message = edifact.message(
                    "ORDERS", "1",
                    [seg("NAD", "BY", ["ACME", "", "91"], "",
                         "O%sBrien" % said, "Street", "City")], "D:96A:UN")
                out = edifact.render(edifact.wrap(
                    [message], "MOCKEDI", "ACME", "1", syntax="UNOB"))
                self.assertIn("++O?'Brien+Street+City'", out.replace("\n", ""))
                (read,) = [item for _g, item in edifact.parse(out).messages()]
                (party,) = [item for item in read.segments if item.tag == "NAD"]
                self.assertEqual(
                    (party.get(4), party.get(5), party.get(6)),
                    ("O'Brien", "Street", "City"))


class AValueNeverOutgrowsItsElement(unittest.TestCase):
    """`ß` is `ss`: one character becomes two, and 35 would become 36."""

    def test_fit_alone_never_makes_a_value_longer(self):
        for value in ("Straße", "Ægir", "Œuvre", "Þór"):
            with self.subTest(value):
                self.assertEqual(len(fit(value, "ascii")), len(value))

    def test_it_grows_into_room_it_is_told_it_has(self):
        self.assertEqual(fit("Straße", "ascii", limit=35), "Strasse")
        self.assertEqual(fit("Ægir Þór", "ascii", limit=35),
                         "AEgir THor")

    def test_and_not_past_it(self):
        full = "Straße " + "x" * 28                 # 35 characters
        self.assertEqual(len(full), 35)
        fitted = fit(full, "ascii", limit=35)
        self.assertEqual(len(fitted), 35)
        self.assertTrue(fitted.startswith("Stra?e "), fitted)
        # One short of full has room for exactly the one more.
        self.assertEqual(fit(full[:-1], "ascii", limit=35),
                         "Strasse " + "x" * 27)

    def test_the_letters_that_do_not_grow_are_still_said(self):
        full = "Łódź Straße " + "x" * 23     # 35 characters
        self.assertEqual(fit(full, "ascii", limit=35),
                         "Lodz Stra?e " + "x" * 23)

    def test_the_renderer_takes_the_room_from_the_dictionary(self):
        # NAD's street is C059's 3042, 35 characters.
        self.assertTrue(nad("UNOB", "Poldis", "Hauptstraße 1", "Bonn")
                        .endswith("+Hauptstrasse 1+Bonn"))
        street = "Hauptstraße " + "1" * 23              # 35 characters
        written = nad("UNOB", "Poldis", street, "Bonn")
        self.assertIn("+Hauptstra?e " + "1" * 23 + "+Bonn", written.replace("??", "?"))

    def test_a_segment_the_dictionary_does_not_know_is_not_grown(self):
        message = edifact.message("ORDERS", "1", [seg("ZZZ", "Straße")],
                                  "D:96A:UN")
        out = edifact.render(edifact.wrap([message], "MOCKEDI", "ACME", "1",
                                          syntax="UNOB"))
        self.assertIn("ZZZ+Stra??e'", out)


class ThroughARunningMock(MockServerCase):
    """A partner with a name level B cannot carry, answered in level B."""

    FULL = "Großhandel Łódź " + "x" * 19        # 35 characters

    def setUp(self):
        super().setUp()
        status, _headers, body = self.patch("/_mock/partners/" + EURODIS, {
            "syntax": "UNOB", "name": "Müller Großhandel",
            "street": "Straße 1", "city": LODZ})
        self.assertEqual(status, 200, body)

    def answer(self, kind="response"):
        self.send(edifact_order("PO-TRANSLIT", sender=EURODIS),
                  headers={"Content-Type": EDIFACT})
        return self.mailbox(EURODIS, kind)[0]["payload"]

    def test_the_answer_names_the_partner_in_plain_letters(self):
        payload = self.answer()
        self.assertIn("UNB+UNOB:", payload)
        self.assertIn("Muller Grosshandel", payload)
        self.assertIn("Strasse 1", payload)
        self.assertIn("Lodz", payload)
        payload.encode("ascii")

    def test_and_is_clean_by_the_mocks_own_dictionary(self):
        report = validate.validate(edifact.parse(self.answer()))
        self.assertTrue(report.clean, [m.segments for m in report.messages])

    def test_a_curled_apostrophe_in_the_name_is_an_apostrophe(self):
        self.patch("/_mock/partners/" + EURODIS,
                   {"name": "O\u2019Brien \u2013 Sons"})
        payload = self.answer()
        self.assertIn("O?'Brien - Sons", payload)
        report = validate.validate(edifact.parse(payload))
        self.assertTrue(report.clean, [m.segments for m in report.messages])

    def test_a_name_that_fills_its_element_still_validates(self):
        self.assertEqual(len(self.FULL), 35)
        self.patch("/_mock/partners/" + EURODIS, {"name": self.FULL})
        payload = self.answer()
        report = validate.validate(edifact.parse(payload))
        self.assertTrue(report.clean, [m.segments for m in report.messages])
        # Every letter that fits is said; the one that would not is a `?`.
        self.assertIn("Gro??handel Lodz " + "x" * 19, payload)


if __name__ == "__main__":
    unittest.main()
