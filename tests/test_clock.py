"""One clock, one format, and a host that is not on UTC.

Every timestamp the control plane returned used to be a naive local-time
string, and they did not all carry the same precision. The Docker image runs
on UTC and the test driving it usually does not, so a test comparing `due_at`
against its own clock was wrong by the host's offset - a failure that passes
on a laptop and fails in CI, or the reverse.

It matters twice over, because `release()` and the `unacknowledged` cutoff
compare these as *strings*. That is only correct while every producer formats
identically, which these tests hold it to rather than assume.

The dates on the wire are the opposite case and are checked here too: ISA09
carries no zone and is the sender's local time by convention, so it stays
local however the clock underneath is kept.
"""
import datetime
import os
import re
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import db, x12
from mockedi.envelope import local

from support import ACME, MockServerCase, x12_order

# Second precision, and a zone.
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TheFormat(unittest.TestCase):
    def test_a_stamp_names_its_zone_and_stops_at_seconds(self):
        self.assertRegex(db.now(), STAMP)

    def test_an_aware_moment_is_converted_rather_than_relabelled(self):
        moment = datetime.datetime(2026, 9, 25, 7, 34, 19,
                                   tzinfo=datetime.timezone(
                                       datetime.timedelta(hours=-7)))
        self.assertEqual(db.stamp(moment), "2026-09-25T14:34:19Z")

    def test_a_naive_moment_is_read_as_the_host_meant_it(self):
        naive = datetime.datetime(2026, 9, 25, 7, 34, 19)
        expected = naive.astimezone(datetime.timezone.utc)
        self.assertEqual(db.stamp(naive),
                         expected.replace(tzinfo=None).isoformat() + "Z")

    def test_microseconds_are_dropped_so_that_strings_sort(self):
        moment = db.utcnow().replace(microsecond=999999)
        self.assertNotIn(".", db.stamp(moment))

    def test_two_stamps_sort_the_way_their_moments_do(self):
        first = db.utcnow()
        second = first + datetime.timedelta(seconds=1)
        self.assertLess(db.stamp(first), db.stamp(second))

    def test_the_clock_is_aware_and_in_utc(self):
        self.assertEqual(db.utcnow().tzinfo, datetime.timezone.utc)


class EveryTimestampTheControlPlaneReturns(MockServerCase):
    """Same shape, everywhere, so that a caller can parse one of them."""

    KEYS = ("at", "due_at", "released_at", "delivered_at", "done_at",
            "last_attempt_at", "started")

    def stamps(self, rows):
        found = []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            for key in self.KEYS:
                value = row.get(key) if isinstance(row, dict) else None
                if value:
                    found.append((key, value))
        return found

    def test_health_mailbox_outbox_and_scheduled_all_agree(self):
        self.send(x12_order("CLOCK-A"))
        seen = []
        for path in ("/_mock/health", "/_mock/mailbox?leave", "/_mock/outbox",
                     "/_mock/scheduled?all", "/_mock/documents"):
            _status, _headers, data = self.get(path)
            seen.extend(self.stamps(data))
        self.assertTrue(seen, "no timestamps were found to check")
        for key, value in seen:
            self.assertRegex(value, STAMP, "%s is %r" % (key, value))

    def test_a_timestamp_can_be_parsed_by_a_caller_in_another_zone(self):
        self.send(x12_order("CLOCK-B"))
        _status, _headers, rows = self.get("/_mock/outbox")
        parsed = datetime.datetime.strptime(
            rows[0]["at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        self.assertLess(abs((db.utcnow() - parsed).total_seconds()), 300)


class WhenTheHostIsNotOnUtc(MockServerCase):
    """The failure that passes on a laptop and fails in CI.

    `TZ` is set far from UTC before the server's own clock is read. On POSIX
    `time.tzset()` makes that take effect in this process, which is the whole
    point: everything below reads the same clock the server does.
    """

    OFFSET = "Pacific/Kiritimati"      # UTC+14, further than anywhere else
    # An hour's delay on the invoice, so that "due later" has something to
    # mean. It is a server flag rather than a partner field.
    config_kwargs = {"invoice_delay_ms": 3600 * 1000}

    @classmethod
    def setUpClass(cls):
        cls.previous_tz = os.environ.get("TZ")
        os.environ["TZ"] = cls.OFFSET
        if hasattr(time, "tzset"):
            time.tzset()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if cls.previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls.previous_tz
        if hasattr(time, "tzset"):
            time.tzset()

    @unittest.skipUnless(hasattr(time, "tzset"), "TZ is not settable here")
    def test_the_stamp_is_still_utc(self):
        stamped = db.now()
        self.assertRegex(stamped, STAMP)
        difference = abs(
            (datetime.datetime.strptime(stamped, "%Y-%m-%dT%H:%M:%SZ")
             .replace(tzinfo=datetime.timezone.utc) - db.utcnow())
            .total_seconds())
        self.assertLess(difference, 5, "the stamp drifted with the host's zone")

    @unittest.skipUnless(hasattr(time, "tzset"), "TZ is not settable here")
    def test_a_delay_is_due_where_a_caller_in_utc_expects_it(self):
        # The arithmetic the issue is about: a document due in an hour is due
        # an hour from now, not an hour plus the host's offset.
        # A delay postpones the *work*, so the invoice is promised rather
        # than written: its due date is on the schedule, not in the outbox.
        self.send(x12_order("CLOCK-TZ"))
        _status, _headers, rows = self.get("/_mock/scheduled?all")
        invoice = [row for row in rows if row["kind"] == "invoice"][0]
        due = datetime.datetime.strptime(
            invoice["due_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=datetime.timezone.utc)
        ahead = (due - db.utcnow()).total_seconds()
        self.assertTrue(3500 < ahead < 3700,
                        "due in %.0fs, expected about an hour" % ahead)

    @unittest.skipUnless(hasattr(time, "tzset"), "TZ is not settable here")
    def test_nothing_due_later_is_released_early(self):
        self.send(x12_order("CLOCK-TZ-2"))
        _status, _headers, data = self.post("/_mock/advance?seconds=60")
        released = [row for row in self.mailbox(ACME) if row["code"] == "810"]
        self.assertEqual(released, [],
                         "an invoice due in an hour was released after a minute")

    @unittest.skipUnless(hasattr(time, "tzset"), "TZ is not settable here")
    def test_the_wire_date_stays_local(self):
        # ISA09 carries no zone and is local by convention, so on a host
        # fourteen hours ahead it reads as the host's date, not UTC's.
        self.send(x12_order("CLOCK-WIRE"))
        row = self.mailbox(ACME)[0]
        isa = row["payload"].replace("\n", "").split("~")[0].split("*")
        self.assertEqual(isa[9], local(db.utcnow()).strftime("%y%m%d"))


class TheUnacknowledgedCutoff(MockServerCase):
    """Compared as strings, so both sides must come from the one formatter."""

    def test_nothing_is_older_than_an_hour_a_minute_after_it_was_sent(self):
        self.send(x12_order("CLOCK-OLD"))
        _status, _headers, rows = self.get("/_mock/unacknowledged?older-than=3600")
        self.assertEqual(rows, [])

    def test_everything_is_older_than_zero_seconds(self):
        self.send(x12_order("CLOCK-OLD-2"))
        _status, _headers, rows = self.get("/_mock/unacknowledged?older-than=0")
        self.assertTrue(rows, "nothing was older than no time at all")


if __name__ == "__main__":
    unittest.main()
