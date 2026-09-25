"""Envelopes with nothing in them.

An interchange can be well formed and still carry no work: a functional group
whose transaction sets are missing, or an ISA with no group at all.  None of
it is a message-level fault, so none of it has a finding to report - which is
exactly how these shapes came to be answered with silence, the one outcome
`docs/ARCHITECTURE.md` rules out.

The rule these tests hold the mock to: every X12 interchange it reads gets an
acknowledgment of some kind, and every acknowledgment it writes validates
against the dictionary it validates yours with.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import ack, validate, x12

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order

EDIFACT = {"Content-Type": "application/edifact"}


def segments(text):
    return [line for line in text.replace("\n", "").split("~") if line]


def rebuild(lines):
    return "~".join(lines) + "~"


def empty_the_group(text):
    """The same interchange with its transaction sets removed, GE01 saying 0.

    The envelope stays consistent: a sender whose translator produced nothing
    to send, rather than a sender who miscounted.
    """
    kept = []
    inside = False
    for line in segments(text):
        if line.startswith("ST*"):
            inside = True
        if not inside:
            kept.append(line)
        if line.startswith("SE*"):
            inside = False
            continue
        if line.startswith("GE*"):
            kept.append("GE*0*" + line.split("*")[2])
            kept.pop(-2)
    return rebuild(kept)


def drop_the_group(text):
    """The interchange with GS/GE and everything between them removed."""
    kept, inside = [], False
    for line in segments(text):
        if line.startswith("GS*"):
            inside = True
        if not inside:
            kept.append(line)
        if line.startswith("GE*"):
            inside = False
    return rebuild([line if not line.startswith("IEA*")
                    else "IEA*0*" + line.split("*")[2] for line in kept])


def unwrap_the_group(text):
    """The interchange with its GS and GE removed, leaving the ST in the open.

    IEA01 is corrected to 0 so that the group count is not *also* wrong: this
    test is about the set that sits outside any group, not about a trailer.
    """
    kept = [line for line in segments(text)
            if not line.startswith(("GS*", "GE*"))]
    return rebuild([line if not line.startswith("IEA*")
                    else "IEA*0*" + line.split("*")[2] for line in kept])


def ta1(payload):
    for line in segments(payload):
        if line.startswith("TA1*"):
            return line.split("*")
    return None


class AGroupWithNoTransactionSet(MockServerCase):
    """GS ... GE with nothing between them is a group fault, not silence."""

    def setUp(self):
        super().setUp()
        self.payload = empty_the_group(x12_order("EMPTY-GROUP"))

    def test_it_is_acknowledged_at_all(self):
        summary = self.send(self.payload)
        self.assertEqual([q["code"] for q in summary["queued"]], ["997"])

    def test_ak1_names_the_group_rather_than_question_marks(self):
        self.send(self.payload)
        ak1 = self.document(ACME, "acknowledgment").groups[0].messages[0].find("AK1")
        self.assertEqual(ak1.get(1), "PO")
        self.assertTrue(ak1.get(2).isdigit(), ak1.elements)

    def test_ak9_rejects_it_and_counts_nothing(self):
        self.send(self.payload)
        ak9 = self.document(ACME, "acknowledgment").groups[0].messages[0].find("AK9")
        self.assertEqual(ak9.elements[:4], ["R", "0", "0", "0"])

    def test_the_997_validates_against_the_mocks_own_dictionary(self):
        self.send(self.payload)
        rows = self.mailbox(ACME, "acknowledgment")
        report = validate.validate(x12.parse(rows[0]["payload"]))
        self.assertEqual(ack.explain(report), ["997/0001: accepted"])

    def test_a_group_that_also_miscounts_still_says_so(self):
        # GE01 = 1 with nothing in the group is two faults; the count is the
        # one with a code, and it is the more useful thing to be told.
        payload = self.payload.replace("GE*0*", "GE*1*")
        self.send(payload)
        ak9 = self.document(ACME, "acknowledgment").groups[0].messages[0].find("AK9")
        self.assertEqual((ak9.get(1), ak9.get(5)), ("R", "5"))

    def test_nothing_is_fulfilled(self):
        summary = self.send(self.payload)
        self.assertFalse(summary["accepted"])
        self.assertEqual(self.mailbox(ACME, "response"), [])


class OneEmptyGroupBesideAGoodOne(MockServerCase):
    """The case that was worst: the interchange succeeded and a group vanished."""

    def payload(self):
        good = x12_order("TWO-GROUPS")
        empty = empty_the_group(x12_order("TWO-GROUPS-B"))
        head = segments(good)[:-1]                  # everything up to IEA
        middle = [line for line in segments(empty)
                  if line.startswith(("GS*", "GE*"))]
        return rebuild(head + middle + ["IEA*2*" + segments(good)[-1].split("*")[2]])

    def test_each_group_gets_its_own_997(self):
        summary = self.send(self.payload())
        self.assertEqual([q["code"] for q in summary["queued"]].count("997"), 2)

    def test_the_empty_group_is_rejected_in_its_own_997(self):
        self.send(self.payload())
        rows = self.mailbox(ACME, "acknowledgment")
        verdicts = []
        for row in rows:
            ak9 = x12.parse(row["payload"]).groups[0].messages[0].find("AK9")
            verdicts.append(tuple(ak9.elements[:4]))
        self.assertIn(("R", "0", "0", "0"), verdicts)
        self.assertIn(("A", "1", "1", "1"), verdicts)

    def test_the_good_group_is_still_fulfilled(self):
        self.send(self.payload())
        self.assertEqual(self.order("TWO-GROUPS")["status"], "invoiced")


class AnInterchangeWithNoGroup(MockServerCase):
    """ISA straight to IEA: there is no group to acknowledge, so a TA1."""

    def test_it_is_answered_with_a_ta1(self):
        summary = self.send(drop_the_group(x12_order("NO-GROUP")))
        self.assertEqual([q["code"] for q in summary["queued"]], ["TA1"])

    def test_the_ta1_rejects_it_as_invalid_interchange_content(self):
        self.send(drop_the_group(x12_order("NO-GROUP")))
        row = self.mailbox(ACME, "interchange-acknowledgment")[0]
        self.assertEqual(ta1(row["payload"])[4:6], ["R", "024"])

    def test_no_997_is_sent_for_a_group_that_does_not_exist(self):
        self.send(drop_the_group(x12_order("NO-GROUP")))
        self.assertEqual(self.mailbox(ACME, "acknowledgment"), [])


class ATransactionSetOutsideAnyGroup(MockServerCase):
    """An ST with no GS around it: an interchange fault, not a group one."""

    def test_it_is_answered_with_a_ta1(self):
        summary = self.send(unwrap_the_group(x12_order("NO-GS")))
        self.assertEqual([q["code"] for q in summary["queued"]], ["TA1"])

    def test_the_ta1_says_invalid_interchange_content(self):
        self.send(unwrap_the_group(x12_order("NO-GS")))
        row = self.mailbox(ACME, "interchange-acknowledgment")[0]
        self.assertEqual(ta1(row["payload"])[4:6], ["R", "024"])

    def test_no_997_invents_a_group_to_acknowledge(self):
        # AK1*??*0 was the old answer, and `??` is not a code AK101 accepts:
        # the mock's own dictionary rejected the acknowledgment it had written.
        self.send(unwrap_the_group(x12_order("NO-GS")))
        self.assertEqual(self.mailbox(ACME, "acknowledgment"), [])

    def test_the_order_is_not_acted_on(self):
        self.send(unwrap_the_group(x12_order("NO-GS")))
        status, _headers, _data = self.get("/_mock/orders/NO-GS")
        self.assertEqual(status, 404)


class TheEdifactSideIsUnchanged(MockServerCase):
    """CONTRL already answered an empty interchange; it still does."""

    def test_an_interchange_with_no_message_is_rejected_by_contrl(self):
        payload = edifact_order("EDI-EMPTY")
        head = [line for line in payload.replace("\n", "").split("'") if line]
        kept = [line for line in head
                if not line.startswith(("UNH", "BGM", "DTM", "NAD", "LIN",
                                        "QTY", "PRI", "IMD", "UNS", "CNT",
                                        "UNT"))]
        kept = [line if not line.startswith("UNE") else "UNE+0+" + line.split("+")[2]
                for line in kept]
        summary = self.send("'".join(kept) + "'", headers=EDIFACT)
        self.assertEqual([q["code"] for q in summary["queued"]], ["CONTRL"])
        self.assertFalse(summary["accepted"])


class EveryAcknowledgmentValidates(MockServerCase):
    """Not only the ones written for clean input.

    `GeneratedDocumentsAreValid` covers the happy path; the acknowledgments
    written for broken envelopes are the ones nobody looked at, and the ones
    where an invented AK1 could hide.
    """

    def test_the_acknowledgments_for_every_broken_shape_are_well_formed(self):
        shapes = {
            "empty group": empty_the_group(x12_order("V-EMPTY")),
            "miscounted empty group": empty_the_group(
                x12_order("V-MISCOUNT")).replace("GE*0*", "GE*1*"),
            "no group": drop_the_group(x12_order("V-NOGROUP")),
            "set outside a group": unwrap_the_group(x12_order("V-NOGS")),
        }
        for name, payload in shapes.items():
            self.post("/_mock/reset")
            self.send(payload)
            written = self.mailbox(ACME)
            self.assertTrue(written, "%s was answered with silence" % name)
            for row in written:
                parsed = x12.parse(row["payload"])
                report = validate.validate(parsed)
                problems = [line for line in ack.explain(report)
                            if "accepted" not in line]
                self.assertEqual(problems, [],
                                 "the %s for %s does not validate: %s"
                                 % (row["code"], name, problems))


if __name__ == "__main__":
    unittest.main()
