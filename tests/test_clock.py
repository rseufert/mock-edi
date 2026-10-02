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
        self.assertRegex(db.wall_now(), STAMP)

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
        stamped = db.wall_now()
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


HOUR = 3600


def moment(stamp):
    return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


class NothingIsStampedBeforeItWasDue(MockServerCase):
    """One clock (#196): what `advance` moves is what every `at` is read from.

    Despatch after an hour and invoice after two. Before this, `due_at` came
    from the mock's clock and `at` from the host's, so a despatch released by
    an advance was stamped an hour before it was due.
    """

    config_kwargs = {"despatch_delay_ms": HOUR * 1000,
                     "invoice_delay_ms": 2 * HOUR * 1000}

    def due(self):
        _s, _h, rows = self.get("/_mock/scheduled?all")
        return {row["kind"]: row for row in rows}

    def kept_on_time(self, po_number):
        promised = self.due()
        for kind, row in promised.items():
            self.assertGreaterEqual(row["done_at"], row["due_at"], kind)
        order = self.order(po_number)
        self.assertGreaterEqual(order["shipments"][0]["at"],
                                promised["despatch"]["due_at"])
        self.assertGreaterEqual(order["invoices"][0]["at"],
                                promised["invoice"]["due_at"])
        _s, _h, sent = self.get("/_mock/documents?direction=out&reference="
                                + po_number)
        at = {row["code"]: row["at"] for row in sent}
        self.assertGreaterEqual(at["856"], promised["despatch"]["due_at"])
        self.assertGreaterEqual(at["810"], promised["invoice"]["due_at"])
        return promised

    def test_when_the_clock_is_moved_by_seconds(self):
        self.send(x12_order("CLOCK-SECONDS"))
        self.post("/_mock/advance?seconds=86400")
        self.kept_on_time("CLOCK-SECONDS")

    def test_when_everything_is_released_at_once(self):
        self.send(x12_order("CLOCK-ALL"))
        self.post("/_mock/advance?all")
        self.kept_on_time("CLOCK-ALL")

    def test_releasing_everything_leaves_the_clock_at_the_last_due_time(self):
        self.send(x12_order("CLOCK-LAST"))
        _s, _h, answer = self.post("/_mock/advance?all")
        last = self.due()["invoice"]["due_at"]
        self.assertGreaterEqual(answer["advancedSeconds"], 2 * HOUR - 5)
        self.assertLess(answer["advancedSeconds"], 2 * HOUR + 60)
        self.assertGreaterEqual(
            datetime.datetime.fromisoformat(answer["clock"]), moment(last))

    def test_each_piece_of_work_is_stamped_when_it_came_due_not_at_the_end(self):
        self.send(x12_order("CLOCK-STEPS"))
        self.post("/_mock/advance?all")
        promised = self.due()
        packed = moment(self.order("CLOCK-STEPS")["shipments"][0]["at"])
        # The despatch at the first hour, not at the second with the invoice.
        late = packed - moment(promised["despatch"]["due_at"])
        self.assertGreaterEqual(late, datetime.timedelta(0))
        self.assertLess(late, datetime.timedelta(minutes=1))

    def test_with_nothing_waiting_the_clock_stays_where_it_is(self):
        self.send(x12_order("CLOCK-TWICE"))
        _s, _h, first = self.post("/_mock/advance?all")
        _s, _h, second = self.post("/_mock/advance?all")
        self.assertEqual(second["released"], [])
        self.assertAlmostEqual(second["advancedSeconds"],
                               first["advancedSeconds"], delta=1)

    def test_the_timeline_reads_in_order_with_times_that_never_run_back(self):
        self.send(x12_order("CLOCK-LINE"))
        self.post("/_mock/advance?all")
        _s, _h, timeline = self.get("/_mock/orders/CLOCK-LINE/timeline")
        events = timeline["events"]
        self.assertEqual([event["at"] for event in events],
                         sorted(event["at"] for event in events))
        told = [(event["event"], event.get("code") or event.get("kind") or "")
                for event in events]
        self.assertEqual(told[-4:], [("packed", ""), ("sent", "856"),
                                     ("invoiced", ""), ("sent", "810")])

    def test_what_arrives_after_an_advance_is_stamped_by_the_moved_clock(self):
        self.post("/_mock/advance?seconds=86400")
        self.send(x12_order("CLOCK-AFTER"))
        ahead = moment(self.order("CLOCK-AFTER")["at"]) - db.utcnow()
        self.assertGreater(ahead, datetime.timedelta(hours=23))

    def test_the_request_log_stays_on_the_hosts_clock(self):
        self.post("/_mock/advance?seconds=86400")
        self.get("/_mock/health")
        _s, _h, rows = self.get("/_mock/requests")
        for row in rows:
            self.assertLess(abs(moment(row["at"]) - db.utcnow()),
                            datetime.timedelta(minutes=5))


class WithNoDelaysTheClockDoesNotMove(MockServerCase):
    def test_releasing_everything_advances_nothing(self):
        self.send(x12_order("CLOCK-STILL"))
        _s, _h, answer = self.post("/_mock/advance?all")
        self.assertLess(answer["advancedSeconds"], 1)


class TheUnacknowledgedCutoffFollowsTheClock(MockServerCase):
    def test_a_document_two_mock_hours_old_is_older_than_a_minute(self):
        self.send(x12_order("CLOCK-AGED"))
        _s, _h, rows = self.get("/_mock/unacknowledged?older-than=60")
        self.assertEqual(rows, [])
        self.post("/_mock/advance?seconds=7200")
        _s, _h, rows = self.get("/_mock/unacknowledged?older-than=60")
        self.assertTrue(rows, "nothing was older than a minute, two hours on")


class RetentionIsCutOffInUtc(unittest.TestCase):
    """A naive local cutoff against UTC stamps was off by the host's offset."""

    def pruned(self, zone, age_hours, keep_hours):
        previous = os.environ.get("TZ")
        os.environ["TZ"] = zone
        time.tzset()
        try:
            conn = db.connect(":memory:")
            self.addCleanup(conn.close)
            at = db.stamp(db.utcnow() - datetime.timedelta(hours=age_hours))
            conn.execute("INSERT INTO request_log (method, path, status,"
                         " bytes_in, bytes_out, partner, at)"
                         " VALUES ('GET', '/', 200, 0, 0, '', ?)", (at,))
            conn.execute("INSERT INTO mdn (partner, direction, original_id,"
                         " message_id, disposition, mic, mode, status, payload,"
                         " at) VALUES ('ACME', 'in', '', '', '', '', 'sync',"
                         " 'received', '', ?)", (at,))
            return db.prune(conn, 0, keep_hours / 24.0)
        finally:
            if previous is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = previous
            time.tzset()

    @unittest.skipUnless(hasattr(time, "tzset"), "TZ is not settable here")
    def test_east_of_greenwich_a_row_younger_than_the_limit_is_kept(self):
        self.assertEqual(self.pruned("Asia/Tokyo", age_hours=1, keep_hours=3), {})

    @unittest.skipUnless(hasattr(time, "tzset"), "TZ is not settable here")
    def test_west_of_greenwich_a_row_older_than_the_limit_goes(self):
        self.assertEqual(self.pruned("America/Los_Angeles", age_hours=5,
                                     keep_hours=3),
                         {"request_log": 1, "mdn": 1})


if __name__ == "__main__":
    unittest.main()
