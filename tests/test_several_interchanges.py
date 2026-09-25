"""One payload, more than one interchange.

Files holding several interchanges are ordinary on a VAN and over SFTP, and
not rare over AS2. The mock used to read the first and drop the rest without
a word, which is the failure `docs/ARCHITECTURE.md` calls worse than not
having the feature at all: a test would pass while half its input vanished.

Each interchange is its own envelope - its own control number, its own
acknowledgment, its own verdict - so one being refused says nothing about the
others, and each is entitled to declare its own punctuation.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi.envelope import Delimiters
from mockedi import edifact, x12

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order

EDIFACT = {"Content-Type": "application/edifact"}


def repunctuate(payload, **delimiters):
    """The same X12 interchange, written with different delimiters."""
    interchange = x12.parse(payload)
    interchange.delimiters = Delimiters(**delimiters)
    return x12.render(interchange)


class SplittingAPayload(unittest.TestCase):
    """The cut itself, without a server in the way."""

    def test_one_interchange_is_one_part(self):
        self.assertEqual(len(x12.split(x12_order("SPLIT-1"))), 1)

    def test_two_are_two(self):
        parts = x12.split(x12_order("SPLIT-2A") + x12_order("SPLIT-2B"))
        self.assertEqual([x12.parse(part).codes() for part in parts],
                         [["850"], ["850"]])

    def test_each_is_cut_at_its_own_trailer(self):
        parts = x12.split(x12_order("SPLIT-3A") + x12_order("SPLIT-3B"))
        for part in parts:
            self.assertEqual(part.count("ISA*"), 1, part[:60])
            self.assertEqual(part.count("IEA*"), 1, part[:60])

    def test_the_second_may_punctuate_itself_differently(self):
        # A VAN concatenates what its senders gave it; they need not agree.
        second = repunctuate(x12_order("SPLIT-4B"), segment="\n", element="|",
                             component="^")
        parts = x12.split(x12_order("SPLIT-4A") + second)
        self.assertEqual(len(parts), 2)
        self.assertEqual(x12.parse(parts[1]).codes(), ["850"])

    def test_an_edifact_release_character_does_not_end_an_interchange(self):
        # `?` escapes the next character, so a UNZ-looking run inside data is
        # data. Cutting on the text rather than on the segments would split
        # here, and the halves would both be unreadable.
        from mockedi.envelope import seg
        payload = edifact_order(
            "SPLIT-5", extra=[seg("FTX", "AAI", "", "", ["UNZ+1+9999'"])])
        self.assertIn("?'", payload)             # the writer escaped it
        self.assertEqual(len(edifact.split(payload)), 1)

    def test_nothing_after_the_trailer_is_dropped_in_silence(self):
        parts = x12.split(x12_order("SPLIT-6") + "\n\n")
        self.assertEqual(len(parts), 1)


class TwoX12Interchanges(MockServerCase):

    def setUp(self):
        super().setUp()
        self.first, self.second = x12_order("TWO-A"), x12_order("TWO-B")
        self.summary = self.send(self.first + self.second)

    def test_both_orders_are_recorded(self):
        self.assertEqual(self.summary["orders"], ["TWO-A", "TWO-B"])
        self.assertEqual(self.order("TWO-A")["status"], "invoiced")
        self.assertEqual(self.order("TWO-B")["status"], "invoiced")

    def test_each_interchange_is_acknowledged(self):
        acknowledgments = self.mailbox(ACME, "acknowledgment")
        self.assertEqual(len(acknowledgments), 2)
        answered = {x12.parse(row["payload"]).groups[0].messages[0]
                    .find("AK1").get(2) for row in acknowledgments}
        sent = {x12.parse(part).groups[0].control
                for part in (self.first, self.second)}
        self.assertEqual(answered, {c.lstrip("0") or "0" for c in sent})

    def test_the_summary_reports_each_interchange_separately(self):
        controls = [item["interchange"] for item in self.summary["interchanges"]]
        self.assertEqual(controls, [x12.parse(self.first).control,
                                    x12.parse(self.second).control])

    def test_the_top_level_keys_cover_the_whole_payload(self):
        self.assertEqual(len(self.summary["transactionSets"]), 2)
        self.assertEqual([q["code"] for q in self.summary["queued"]].count("997"), 2)
        self.assertTrue(self.summary["accepted"])


class OneInterchangeIsStillOne(MockServerCase):
    """The shape nothing should notice changing."""

    def test_the_summary_still_describes_it_at_the_top_level(self):
        summary = self.send(x12_order("ONE-A"))
        self.assertEqual(summary["orders"], ["ONE-A"])
        self.assertEqual([q["code"] for q in summary["queued"]],
                         ["997", "855", "856", "810"])

    def test_and_lists_itself_once(self):
        summary = self.send(x12_order("ONE-B"))
        self.assertEqual(len(summary["interchanges"]), 1)
        self.assertEqual(summary["interchanges"][0]["interchange"],
                         summary["interchange"])

    def test_an_unreadable_payload_is_still_a_422(self):
        status, _headers, data = self.post(
            "/edi", "not an interchange at all",
            headers={"Content-Type": "application/edi-x12"})
        self.assertEqual(status, 422)
        self.assertFalse(data["accepted"])
        self.assertIn("ISA", data["error"])


class TwoEdifactInterchanges(MockServerCase):

    def test_both_are_read_and_both_are_answered(self):
        summary = self.send(edifact_order("EDI-A") + edifact_order("EDI-B"),
                            headers=EDIFACT)
        self.assertEqual(summary["orders"], ["EDI-A", "EDI-B"])
        self.assertEqual(len(self.mailbox(EURODIS, "acknowledgment")), 2)


class OneRefusedAmongSeveral(MockServerCase):
    """A bad interchange is its own business, and does not take the rest down."""

    def test_the_others_are_still_processed(self):
        stranger = x12_order("STRANGER", sender="NOBODY")
        summary = self.send(x12_order("GOOD-A") + stranger + x12_order("GOOD-B"))
        self.assertEqual(summary["orders"], ["GOOD-A", "GOOD-B"])
        self.assertEqual(self.order("GOOD-A")["status"], "invoiced")
        self.assertEqual(self.order("GOOD-B")["status"], "invoiced")

    def test_the_refusal_is_reported_against_its_own_interchange(self):
        stranger = x12_order("STRANGER-2", sender="NOBODY")
        summary = self.send(x12_order("GOOD-C") + stranger)
        refused = summary["interchanges"][1]
        self.assertFalse(refused["accepted"])
        self.assertIn("NOBODY", refused["error"])

    def test_the_payload_as_a_whole_is_not_accepted(self):
        stranger = x12_order("STRANGER-3", sender="NOBODY")
        summary = self.send(x12_order("GOOD-D") + stranger)
        self.assertFalse(summary["accepted"])

    def test_a_replay_inside_a_payload_is_refused_on_its_own(self):
        # The second copy is a duplicate of the first, in the same file.
        order = x12_order("REPLAYED")
        summary = self.send(order + order)
        self.assertEqual([q["code"] for q in summary["queued"]].count("TA1"), 1)
        self.assertEqual(summary["orders"], ["REPLAYED"])


class ADroppedFileHoldingSeveral(MockServerCase):
    """The drop directory reads a whole file, not the first envelope in it."""

    def test_both_interchanges_in_one_file_are_processed(self):
        payload = x12_order("DROP-A") + x12_order("DROP-B")
        status, _headers, data = self.post(
            "/_mock/validate", payload,
            headers={"Content-Type": "application/edi-x12"})
        self.assertEqual(status, 200, data)
        self.assertEqual([item["control"] for item in data["interchanges"]],
                         [x12.parse(x) .control for x in x12.split(payload)])
        self.assertTrue(data["clean"])


if __name__ == "__main__":
    unittest.main()
