"""A clock that starts where it is told and stands there: `--start-at`.

The mock's clock was the host's, moved forward by `/_mock/advance` and never
set. So no two runs of one script carried the same dates, and a capture made
on Monday could not be compared with one made on Tuesday.

Started at a time, the clock reads that time until it is advanced and never
follows the host. Two runs of one script then write the same documents, byte
for byte, and the same timeline - on any day and in any time zone, because
the documents are dated in the zone the start time was written in and not in
the machine's (#280).

What is left out of "the same" is not asserted here because it is not true:
the request log's times, `started` in `/_mock/health`, the HTTP and AS2 `Date`
headers, and an MDN's random `Message-ID` and MIME boundary.
"""
import contextlib
import io
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import db
from mockedi.__main__ import main
from mockedi.server import BadConfig
from mockedi.testing import Document, Mock

from support import ACME, x12_order

START = "2026-11-02T09:00:00Z"
DAY = 86400


def script(mock):
    """One order, its documents a day apart, and a second order after them."""
    mock.send(x12_order("PIN-A", control="000000901"))
    mock.advance(everything=True)
    mock.send(x12_order("PIN-B", control="000000902"))
    mock.advance(seconds=3600)
    return {
        "documents": [row["payload"] for row in mock.mailbox(ACME)],
        "timelines": [mock.timeline("PIN-A"), mock.timeline("PIN-B")],
        "orders": mock.expect("GET", "/_mock/orders"),
        "outbox": mock.outbox(),
    }


def captured(**config):
    settings = dict(start_at=START, despatch_delay_ms=3600 * 1000,
                    invoice_delay_ms=DAY * 1000)
    settings.update(config)
    with Mock.start(**settings) as mock:
        return script(mock)


@contextlib.contextmanager
def host_zone(name):
    """The process in another time zone, where the platform can be told."""
    before = os.environ.get("TZ")
    os.environ["TZ"] = name
    time.tzset()
    try:
        yield
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()


class TwoRunsOfOneScript(unittest.TestCase):

    def test_they_write_the_same_documents_and_the_same_timeline(self):
        first = captured()
        time.sleep(1.1)                  # the host's clock has moved on
        second = captured()
        self.assertEqual(first["documents"], second["documents"])
        self.assertEqual(first["timelines"], second["timelines"])
        self.assertEqual(first["orders"], second["orders"])
        self.assertEqual(first["outbox"], second["outbox"])
        self.assertGreaterEqual(len(first["documents"]), 6)

    @unittest.skipUnless(hasattr(time, "tzset"),
                         "this platform cannot change the process's time zone")
    def test_and_the_same_in_another_time_zone(self):
        with host_zone("Pacific/Auckland"):
            east = captured()
        with host_zone("America/Los_Angeles"):
            west = captured()
        self.assertEqual(east["documents"], west["documents"])
        self.assertEqual(east["timelines"], west["timelines"])

    @unittest.skipUnless(hasattr(time, "tzset"),
                         "this platform cannot change the process's time zone")
    def test_which_an_unpinned_mock_does_not_manage(self):
        # The premise of the test above: without a start time the zone shows.
        def interchange_time(zone):
            with host_zone(zone), Mock.start() as mock:
                mock.send(x12_order("PIN-ZONE"))
                first = mock.mailbox(ACME)[0]["payload"]
                return first.split("*")[9:11]
        self.assertNotEqual(interchange_time("Pacific/Auckland"),
                            interchange_time("America/Los_Angeles"))


class TheClockStandsWhereItWasPut(unittest.TestCase):

    def setUp(self):
        self.mock = Mock.start(start_at=START, invoice_delay_ms=DAY * 1000)
        self.addCleanup(self.mock.close)

    def clock(self):
        return self.mock.expect("GET", "/_mock/state")["clock"]

    def test_it_reads_the_start_time_however_long_the_host_waits(self):
        time.sleep(1.1)
        self.assertEqual(self.clock(), {
            "now": "2026-11-02T09:00:00+00:00", "startAt": START,
            "advancedSeconds": 0.0})

    def test_everything_between_two_advances_shares_one_time(self):
        self.mock.send(x12_order("PIN-STILL"))
        time.sleep(1.1)
        self.mock.send(x12_order("PIN-STILL-2"))
        stamps = {event["at"]
                  for po in ("PIN-STILL", "PIN-STILL-2")
                  for event in self.mock.timeline(po)["events"]}
        self.assertEqual(stamps, {"2026-11-02T09:00:00Z"})

    def test_an_advance_moves_it_and_it_stands_again(self):
        answer = self.mock.advance(seconds=90)
        self.assertEqual(answer["clock"], "2026-11-02T09:01:30+00:00")
        time.sleep(1.1)
        self.assertEqual(self.clock()["now"], "2026-11-02T09:01:30+00:00")
        self.assertEqual(self.clock()["advancedSeconds"], 90.0)

    def test_advancing_to_everything_due_stops_at_the_last_due_time(self):
        self.mock.send(x12_order("PIN-DUE"))
        self.mock.advance(everything=True)
        self.assertEqual(self.clock()["now"], "2026-11-03T09:00:00+00:00")
        invoice = self.mock.document(ACME, "invoice")
        self.assertEqual(invoice.find("BIG").get(1), "20261103")

    def test_a_reset_puts_it_back_to_the_start_time(self):
        seeded = self.mock.expect("GET", "/_mock/orders")
        self.mock.advance(seconds=7200)
        self.mock.reset()
        self.assertEqual(self.clock()["now"], "2026-11-02T09:00:00+00:00")
        self.assertEqual(self.mock.expect("GET", "/_mock/orders"), seeded)

    def test_what_the_mock_dates_on_its_own_initiative_follows_it(self):
        self.mock.expect("POST", "/_mock/partners", {
            "id": "SELLCO", "name": "Sell Co", "role": "supplier"}, status=201)
        self.mock.expect("POST", "/_mock/purchase", {
            "partner": "SELLCO", "po_number": "PIN-BUY",
            "lines": [{"sku": "WIDGET-001", "quantity": "10", "uom": "EA",
                       "price": "12.50"}]}, status=201)
        order = Document(self.mock.mailbox("SELLCO")[0]["payload"])
        self.assertEqual(order.find("BEG").get(5), "20261102")

    def test_the_seeded_orders_are_dated_from_it_too(self):
        dates = sorted(row["ordered_on"]
                       for row in self.mock.expect("GET", "/_mock/orders"))
        self.assertEqual(dates, ["2026-10-19", "2026-10-24"])


class OrderWhenEveryTimeTies(unittest.TestCase):
    """A standing clock must not blur what happened first."""

    def events(self, **config):
        with Mock.start(**config) as mock:
            mock.send(x12_order("PIN-ORDER"))
            return [(event["event"], event.get("code", ""))
                    for event in mock.timeline("PIN-ORDER")["events"]]

    def test_the_timeline_keeps_the_order_things_happened_in(self):
        pinned = self.events(start_at=START)
        self.assertEqual(pinned, self.events())
        self.assertGreater(len(pinned), 6)


class TheZoneTheStartTimeIsWrittenIn(unittest.TestCase):
    """It is the zone the documents are dated in."""

    def interchange(self, start_at):
        with Mock.start(start_at=start_at) as mock:
            mock.send(x12_order("PIN-ZONE"))
            fields = mock.mailbox(ACME)[0]["payload"].split("*")
            return fields[9], fields[10], mock.timeline("PIN-ZONE")["events"][0]["at"]

    def test_z_dates_documents_in_utc(self):
        self.assertEqual(self.interchange("2026-11-02T23:30:00Z"),
                         ("261102", "2330", "2026-11-02T23:30:00Z"))

    def test_an_offset_dates_them_in_that_zone(self):
        # The same instant as above, written an hour ahead: tomorrow there.
        self.assertEqual(self.interchange("2026-11-03T00:30:00+01:00"),
                         ("261103", "0030", "2026-11-02T23:30:00Z"))

    def test_state_says_the_start_time_as_it_was_given(self):
        with Mock.start(start_at="2026-11-03T00:30:00+01:00") as mock:
            clock = mock.expect("GET", "/_mock/state")["clock"]
        self.assertEqual(clock["startAt"], "2026-11-03T00:30:00+01:00")
        self.assertEqual(clock["now"], "2026-11-03T00:30:00+01:00")


class AStartTimeThatIsRefused(unittest.TestCase):

    def test_one_with_no_zone_is_not_guessed_at(self):
        with self.assertRaises(BadConfig) as caught:
            Mock.start(start_at="2026-11-02T09:00:00")
        self.assertIn("says no zone", str(caught.exception))
        self.assertIn("2026-11-02T09:00:00Z", str(caught.exception))

    def test_nor_is_something_that_is_not_a_time(self):
        with self.assertRaises(BadConfig) as caught:
            Mock.start(start_at="next monday")
        self.assertIn("not an ISO 8601 time", str(caught.exception))

    def test_the_command_line_says_so_in_one_line_and_does_not_start(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = main(["--port", "0", "--start-at", "2026-11-02 09:00"])
        self.assertEqual(code, 2)
        self.assertEqual(len(stderr.getvalue().strip().splitlines()), 1)
        self.assertIn("says no zone", stderr.getvalue())


class WithNoStartTime(unittest.TestCase):

    def test_the_clock_is_the_hosts_as_it_always_was(self):
        with Mock.start() as mock:
            clock = mock.expect("GET", "/_mock/state")["clock"]
            self.assertIsNone(clock["startAt"])
            self.assertEqual(clock["advancedSeconds"], 0.0)
            self.assertEqual(clock["now"][:4], str(db.utcnow().year))


class AFileKeepsTheClockItWasStartedOn(unittest.TestCase):

    def setUp(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        self.path = os.path.join(folder, "mock.db")

    def start(self, **config):
        mock = Mock.start(db_path=self.path, **config)
        self.addCleanup(mock.close)
        return mock

    def test_restarted_at_the_same_time_it_is_as_far_on_as_it_was_left(self):
        mock = self.start(start_at=START)
        mock.advance(seconds=7200)
        mock.close()
        again = self.start(start_at=START)
        clock = again.expect("GET", "/_mock/state")["clock"]
        self.assertEqual(clock["now"], "2026-11-02T11:00:00+00:00")
        self.assertEqual(clock["advancedSeconds"], 7200.0)

    def test_a_different_start_time_is_refused_and_names_the_files(self):
        self.start(start_at=START).close()
        with self.assertRaises(db.DatabaseError) as caught:
            self.start(start_at="2027-01-04T09:00:00Z")
        self.assertIn("was started at 2026-11-02T09:00:00Z, not "
                      "2027-01-04T09:00:00Z", str(caught.exception))

    def test_the_same_instant_in_another_zone_is_a_different_start_time(self):
        # It would date the documents differently, so it is not the same.
        self.start(start_at=START).close()
        with self.assertRaises(db.DatabaseError):
            self.start(start_at="2026-11-02T10:00:00+01:00")

    def test_no_start_time_is_refused_for_a_file_that_was_pinned(self):
        self.start(start_at=START).close()
        with self.assertRaises(db.DatabaseError) as caught:
            self.start()
        self.assertIn("--start-at 2026-11-02T09:00:00Z", str(caught.exception))

    def test_a_file_that_has_traded_by_the_hosts_clock_cannot_be_pinned(self):
        mock = self.start()
        mock.send(x12_order("PIN-LATE"))
        mock.close()
        with self.assertRaises(db.DatabaseError) as caught:
            self.start(start_at=START)
        self.assertIn("stamped by the host's clock", str(caught.exception))

    def test_a_refusal_leaves_the_file_usable(self):
        self.start(start_at=START).close()
        with self.assertRaises(db.DatabaseError):
            self.start()
        again = self.start(start_at=START)
        self.assertEqual(again.expect("GET", "/_mock/health")["status"], "ok")

    def test_a_reset_keeps_the_start_time_in_the_file(self):
        mock = self.start(start_at=START)
        mock.reset()
        mock.close()
        again = self.start(start_at=START)
        self.assertEqual(again.expect("GET", "/_mock/state")["clock"]["startAt"],
                         START)


if __name__ == "__main__":
    unittest.main()
