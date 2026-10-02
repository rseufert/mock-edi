"""A mock restarted on a file database comes back at the time it was stopped (#228).

The advance lived only in the running process. A mock that was moved a day
ahead and restarted came back at real time, with a file full of stamps and
due times from a clock a day ahead of it: its next event was stamped before
its last one, and what was due tomorrow was a day away again.
"""
import datetime
import os
import sqlite3
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import FileDatabaseCase, x12_order

DAY = 86400


def moment(stamp):
    return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=datetime.timezone.utc)


class _Restarted(FileDatabaseCase):

    def clock(self):
        """Where the clock is, read by an advance of nothing."""
        _s, _h, answer = self.post("/_mock/advance?seconds=0")
        return answer

    def ahead(self):
        return self.clock()["advancedSeconds"]


class AnAdvanceSurvivesARestart(_Restarted):

    def test_the_clock_is_as_far_ahead_as_it_was_left(self):
        self.post("/_mock/advance?seconds=%d" % DAY)
        self.restart()
        self.assertAlmostEqual(self.ahead(), DAY, delta=1)

    def test_two_advances_add_up_across_it(self):
        self.post("/_mock/advance?seconds=%d" % DAY)
        self.restart()
        self.post("/_mock/advance?seconds=%d" % DAY)
        self.restart()
        self.assertAlmostEqual(self.ahead(), 2 * DAY, delta=1)

    def test_nothing_is_stamped_before_what_the_file_already_holds(self):
        self.post("/_mock/advance?seconds=%d" % DAY)
        self.send(x12_order("PO-BEFORE"))
        before = self.order("PO-BEFORE")["at"]
        self.restart()
        self.send(x12_order("PO-AFTER"))
        self.assertGreaterEqual(self.order("PO-AFTER")["at"], before)
        _s, _h, timeline = self.get("/_mock/orders/PO-AFTER/timeline")
        for event in timeline["events"]:
            self.assertGreaterEqual(event["at"], before, event["summary"])

    def test_a_reset_puts_the_clock_back_and_a_restart_leaves_it_there(self):
        self.post("/_mock/advance?seconds=%d" % DAY)
        self.post("/_mock/reset")
        self.assertLess(self.ahead(), 1)
        self.restart()
        self.assertLess(self.ahead(), 1)

    def test_a_file_that_never_recorded_an_advance_starts_at_real_time(self):
        self.post("/_mock/advance?seconds=%d" % DAY)
        self.stop()
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM control_number WHERE scope = 'clock'")
        conn.commit()
        conn.close()
        self.start()
        self.assertLess(self.ahead(), 1)


class WorkStillWaitingSurvivesIt(_Restarted):
    config_kwargs = {"despatch_delay_ms": 3600 * 1000,
                     "invoice_delay_ms": DAY * 1000}

    def test_what_was_due_tomorrow_is_still_due_when_it_was(self):
        self.send(x12_order("PO-WAITING"))
        self.post("/_mock/advance?seconds=3700")      # packed, not yet billed
        self.restart()
        _s, _h, waiting = self.get("/_mock/scheduled")
        self.assertEqual([row["kind"] for row in waiting], ["invoice"])
        left = (moment(waiting[0]["due_at"])
                - datetime.datetime.fromisoformat(self.clock()["clock"]))
        # A day less the 3,700 seconds already gone; back at real time it
        # would be the whole day again.
        self.assertAlmostEqual(left.total_seconds(), DAY - 3700, delta=5)
        _s, _h, answer = self.post("/_mock/advance?seconds=%d" % (DAY - 3600))
        self.assertTrue(answer["released"], "the invoice did not come due")

    def test_releasing_everything_leaves_a_clock_a_restart_keeps(self):
        self.send(x12_order("PO-ALL"))
        self.post("/_mock/advance?all")
        _s, _h, done = self.get("/_mock/scheduled?all")
        last = max(row["due_at"] for row in done)
        self.restart()
        self.assertGreaterEqual(
            datetime.datetime.fromisoformat(self.clock()["clock"]), moment(last))
        self.assertAlmostEqual(self.ahead(), DAY, delta=5)


del _Restarted

if __name__ == "__main__":
    unittest.main()
