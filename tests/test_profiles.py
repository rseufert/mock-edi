"""A partner's implementation guide, as a narrowing of the dictionary.

The standard allows more than any one partner accepts. A profile says what the
partner's guide narrows - a segment required or forbidden, a code list cut
down, an element shortened - and a document that satisfies 004010 but not the
guide is rejected with a finding that names the rule and whose guide it is.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import x12
from mockedi.envelope import seg

from support import ACME, FileDatabaseCase, MockServerCase, _next_control, parse

GUIDE = {
    "name": "Acme 850 guide v2",
    "sets": {"850": {
        "require": ["REF"],
        "forbid": ["PO1/PO4"],
        "segments": {
            "REF": {"elements": {"1": {"codes": ["IA"]}, "2": {"maxLength": 15}}},
            "PO1": {"elements": {"3": {"codes": ["EA", "CS"]}}},
        },
    }},
}
WHOSE = "ACME's guide 'Acme 850 guide v2'"


def order(po_number, ref=("IA", "V-1234"), unit="EA", po4=False, line_ref=None):
    """An 850 that satisfies 004010, and the guide unless told otherwise."""
    body = [seg("BEG", "00", "SA", po_number, "", "20260924"),
            seg("CUR", "BY", "USD")]
    if ref:
        body.append(seg("REF", *ref))
    body += [seg("N1", "ST", "Acme DC 4", "92", "ACME-DC4"),
             seg("PO1", "1", "10", unit, "12.50", "", "VP", "WIDGET-001")]
    if po4:
        body.append(seg("PO4", "12"))
    if line_ref:
        body.append(seg("REF", *line_ref))
    body.append(seg("CTT", "1"))
    control = _next_control(9)
    return x12.render(x12.wrap([x12.message("850", "0001", body)], ACME, "MOCKEDI",
                               control, control.lstrip("0"), "PO"))


class ProfileCase(MockServerCase):
    def setUp(self):
        super().setUp()
        status, _h, data = self.request("PUT", "/_mock/partners/ACME/profile", GUIDE)
        self.assertEqual(status, 200, data)

    def refused(self, payload):
        """Send it; the 997 must reject it. Returns the 997 and the findings."""
        summary = self.send(payload)
        self.assertEqual(summary["orders"], [])
        ack = parse([r for r in self.mailbox(ACME, "acknowledgment")][-1]["payload"])
        message = ack.groups[0].messages[0]
        self.assertEqual(message.find("AK5").get(1), "R")
        return message, summary["transactionSets"][0]["findings"]


class AGuideNarrowsWhatIsAccepted(ProfileCase):
    def test_a_document_the_guide_allows_goes_through(self):
        summary = self.send(order("PO-GUIDE-OK"))
        self.assertEqual(summary["orders"], ["PO-GUIDE-OK"])

    def test_a_segment_it_requires_and_is_missing(self):
        message, findings = self.refused(order("PO-NO-REF", ref=None))
        ak3 = message.find("AK3")
        self.assertEqual((ak3.get(1), ak3.get(4)), ("REF", "3"))
        self.assertTrue(any(WHOSE in f for f in findings), findings)

    def test_a_segment_it_does_not_use(self):
        message, findings = self.refused(order("PO-PO4", po4=True))
        ak3 = [s for s in message.find_all("AK3") if s.get(1) == "PO4"][0]
        self.assertEqual(ak3.get(4), "2")                 # unexpected segment
        self.assertTrue(any("PO4 is not used" in f and WHOSE in f for f in findings))

    def test_a_code_it_does_not_allow(self):
        message, findings = self.refused(order("PO-DZ", unit="DZ"))
        ak4 = message.find("AK4")
        self.assertEqual((ak4.get(1), ak4.get(3), ak4.get(4)), ("3", "7", "DZ"))
        self.assertTrue(any(WHOSE in f for f in findings))

    def test_a_length_it_shortens(self):
        message, _findings = self.refused(order("PO-LONGREF", ref=("IA", "V" * 30)))
        ak4 = message.find("AK4")
        self.assertEqual((ak4.get(1), ak4.get(3)), ("2", "5"))

    def test_a_ref_on_the_line_is_not_the_headers(self):
        # REF*VN on a PO1 line is allowed: the guide narrows the header's REF.
        summary = self.send(order("PO-LINE-REF", line_ref=("VN", "SELLER-99")))
        self.assertEqual(summary["orders"], ["PO-LINE-REF"])

    def test_without_the_guide_the_same_document_is_fine(self):
        self.request("DELETE", "/_mock/partners/ACME/profile")
        summary = self.send(order("PO-NO-GUIDE", ref=None, unit="DZ", po4=True))
        self.assertEqual(summary["orders"], ["PO-NO-GUIDE"])


class TheRulesAreServed(ProfileCase):
    def test_the_dictionary_for_a_partner_is_the_narrowed_one(self):
        _s, _h, plain = self.get("/_mock/dictionary/X12/850")
        _s, _h, narrowed = self.get("/_mock/dictionary/X12/850?partner=ACME")
        self.assertIsNone(plain["profile"])
        self.assertEqual(narrowed["profile"], WHOSE)

        def use(data, tag, loop):
            return [s for s in data["segments"]
                    if s["tag"] == tag and s["loop"] == loop]

        self.assertEqual(use(plain, "REF", "")[0]["requirement"], "O")
        self.assertEqual(use(narrowed, "REF", "")[0]["requirement"], "M")
        self.assertEqual(use(narrowed, "REF", "PO1")[0]["requirement"], "O")
        self.assertTrue(use(plain, "PO4", "PO1"))
        self.assertFalse(use(narrowed, "PO4", "PO1"))
        po103 = use(narrowed, "PO1", "PO1")[0]["elements"][2]
        self.assertEqual(po103["codes"], ["CS", "EA"])
        ref02 = use(narrowed, "REF", "")[0]["elements"][1]
        self.assertEqual(ref02["length"], "1/15")

    def test_validate_can_apply_a_partners_guide(self):
        payload = order("PO-CHECK", ref=None)
        _s, _h, plain = self.post("/_mock/validate", payload)
        _s, _h, guided = self.post("/_mock/validate?partner=ACME", payload)
        self.assertTrue(plain["clean"])
        self.assertFalse(guided["clean"])
        self.assertTrue(any(WHOSE in line for line in guided["explain"]))

    def test_the_profile_can_be_read_back(self):
        _s, _h, data = self.get("/_mock/partners/ACME/profile")
        self.assertEqual(data["name"], "Acme 850 guide v2")
        # A loop's path under `segments` stands for the segment that starts it.
        self.assertIn("PO1/PO1", data["sets"]["850"]["segments"])


class AGuideAndTheDirection(MockServerCase):
    """#119 refuses a set the seller does not receive; a guide adds nothing."""

    SENT_BY_THE_SELLER = {"name": "855 rules", "sets": {"855": {"require": ["REF"]}}}

    def test_a_misdirected_set_is_refused_once_and_not_by_the_guide(self):
        # An 855 arriving from a buyer is refused for being the wrong
        # document. A guide for 855s must not pile findings on top of that.
        status, _h, data = self.request("PUT", "/_mock/partners/ACME/profile",
                                        self.SENT_BY_THE_SELLER)
        self.assertEqual(status, 200, data)
        body = [seg("BAK", "00", "AD", "PO-WRONG-WAY", "20260924"), seg("CTT", "0")]
        control = _next_control(9)
        summary = self.send(x12.render(x12.wrap(
            [x12.message("855", "0001", body)], ACME, "MOCKEDI", control,
            control.lstrip("0"), "PR")))
        findings = summary["transactionSets"][0]["findings"]
        self.assertFalse(any("855 rules" in f for f in findings), findings)
        message = parse(self.mailbox(ACME, "acknowledgment")[-1]["payload"])
        ak5 = message.groups[0].messages[0].find("AK5")
        self.assertEqual(ak5.elements, ["R", "1"])

    def test_validate_applies_a_guide_without_taking_a_side(self):
        # /_mock/validate reads a document on its own, so it can check the
        # mock's own output: with ?partner= it applies the guide, not a role.
        self.request("PUT", "/_mock/partners/ACME/profile", GUIDE)
        self.send(order("PO-OWN-855"))
        own = self.mailbox(ACME, "response")[-1]["payload"]
        _s, _h, data = self.post("/_mock/validate?partner=ACME", own)
        self.assertTrue(data["clean"], data["explain"])


class AProfileMayNotWiden(MockServerCase):
    def put(self, sets):
        return self.request("PUT", "/_mock/partners/ACME/profile",
                            {"name": "wider", "sets": sets})

    def assertRefused(self, sets, *fragments):
        status, _h, data = self.put(sets)
        self.assertEqual(status, 400, data)
        text = " ".join(data["problems"])
        for fragment in fragments:
            self.assertIn(fragment, text)

    def test_a_code_the_dictionary_does_not_have(self):
        self.assertRefused({"850": {"segments": {"PO1": {"elements": {
            "3": {"codes": ["EA", "XX"]}}}}}}, "XX", "only allow fewer")

    def test_a_longer_maximum(self):
        self.assertRefused({"850": {"segments": {"REF": {"elements": {
            "2": {"maxLength": 60}}}}}}, "REF02", "not lengthen")

    def test_forbidding_a_mandatory_segment(self):
        self.assertRefused({"850": {"forbid": ["BEG"]}}, "BEG is mandatory")

    def test_an_optional_mandatory_element(self):
        self.assertRefused({"850": {"segments": {"BEG": {"elements": {
            "3": {"required": False}}}}}}, "cannot make it optional")

    def test_more_uses_or_repeats_than_the_dictionary(self):
        self.assertRefused({"850": {"segments": {"CUR": {"maxUse": 5}},
                                    "loops": {"PO1": {"repeat": 999999999}}}},
                           "CUR may be used 1", "the PO1 loop repeats at most")

    def test_a_path_that_is_not_there_names_the_ones_that_are(self):
        self.assertRefused({"850": {"require": ["PO4"]}}, "PO4 is nowhere",
                           "PO1/PO4")

    def test_every_problem_is_named_at_once(self):
        status, _h, data = self.put({"850": {"forbid": ["BEG"], "oops": 1},
                                     "999X": {}})
        self.assertEqual(status, 400)
        self.assertEqual(len(data["problems"]), 3, data["problems"])

    def test_and_nothing_is_stored(self):
        self.put({"850": {"forbid": ["BEG"]}})
        status, _h, _data = self.get("/_mock/partners/ACME/profile")
        self.assertEqual(status, 404)


class AProfileBelongsToItsPartner(FileDatabaseCase):
    def test_it_survives_a_restart(self):
        self.request("PUT", "/_mock/partners/ACME/profile", GUIDE)
        self.restart()
        _s, _h, data = self.get("/_mock/partners/ACME/profile")
        self.assertEqual(data["name"], "Acme 850 guide v2")

    def test_it_goes_with_the_partner_and_with_a_reset(self):
        self.request("PUT", "/_mock/partners/ACME/profile", GUIDE)
        self.post("/_mock/reset")
        status, _h, _data = self.get("/_mock/partners/ACME/profile")
        self.assertEqual(status, 404)
        self.post("/_mock/partners", {"id": "NEWCO", "dialect": "X12"})
        self.request("PUT", "/_mock/partners/NEWCO/profile", GUIDE)
        self.request("DELETE", "/_mock/partners/NEWCO")
        self.post("/_mock/partners", {"id": "NEWCO", "dialect": "X12"})
        status, _h, _data = self.get("/_mock/partners/NEWCO/profile")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
