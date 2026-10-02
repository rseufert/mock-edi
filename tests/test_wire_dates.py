"""Every date in a document is the mock's clock in the host's zone (#196).

The envelope always was: ISA, GS and UNB carry no zone and are the sender's
local time by convention. The body was written in UTC, so on a host away from
Greenwich, in the hours when the two dates differ, one 856 said one day in
BSN03 and another in its envelope and its ship date.

The zone is chosen when the tests start so that the local date and the UTC
date differ at that moment, whatever the time of day: fourteen hours ahead
after 11:00 UTC, twelve behind before it. On a host where they agreed these
would pass against the old code, and prove nothing.
"""
import datetime
import os
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from mockedi import db
from support import (ACME, EURODIS, MockServerCase, edifact_order, parse,
                     x12_change, x12_order)
from test_remittance import x12_remittance

X12 = {"Content-Type": "application/edi-x12"}
EDIFACT = {"Content-Type": "application/edifact"}


@unittest.skipUnless(hasattr(time, "tzset"), "TZ is not settable here")
class _AwayFromGreenwich(MockServerCase):
    # Long enough that an order can still be changed after it has arrived.
    config_kwargs = {"invoice_delay_ms": 3600 * 1000}

    @classmethod
    def setUpClass(cls):
        cls.previous_tz = os.environ.get("TZ")
        # POSIX signs run backwards: `Etc/GMT-14` is fourteen hours ahead.
        ahead = db.utcnow().hour >= 11
        os.environ["TZ"] = "Etc/GMT-14" if ahead else "Etc/GMT+12"
        time.tzset()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if cls.previous_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = cls.previous_tz
        time.tzset()

    def setUp(self):
        super().setUp()
        self.local_day = datetime.datetime.now().strftime("%Y%m%d")
        self.utc_day = db.utcnow().strftime("%Y%m%d")
        self.assertNotEqual(self.local_day, self.utc_day,
                            "the zone was meant to put the host on another day")

    def sent(self, partner):
        self.post("/_mock/advance?all")
        return {row["code"]: parse(row["payload"])
                for row in self.mailbox(partner)}


class AnX12DocumentIsDatedAsItsEnvelopeIs(_AwayFromGreenwich):

    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-DATES"))
        self.send(x12_change("PO-DATES", [("1", "QD", 60, "12.50")]))
        self.documents = self.sent(ACME)

    def body(self, code):
        return self.documents[code].groups[0].messages[0]

    def group_date(self, code):
        return self.documents[code].groups[0].date

    def test_the_855_is_acknowledged_on_the_envelopes_day(self):
        self.assertEqual(self.body("855").find("BAK").get(9),
                         self.group_date("855"))

    def test_the_865_is_dated_on_the_envelopes_day(self):
        bca = self.body("865").find("BCA")
        self.assertIn(self.group_date("865"), bca.elements)
        self.assertNotIn(self.utc_day, bca.elements)

    def test_the_856_has_one_date_not_two(self):
        despatch = self.body("856")
        shipped = [d.get(2) for d in despatch.find_all("DTM")
                   if d.get(1) == "011"][0]
        self.assertEqual(despatch.find("BSN").get(3), self.group_date("856"))
        self.assertEqual(despatch.find("BSN").get(3), shipped)

    def test_the_856s_time_is_the_envelopes_too(self):
        despatch = self.documents["856"]
        written = despatch.groups[0].messages[0].find("BSN").get(4)
        # Within a minute of GS05, and so in the same zone.
        gap = abs(int(written[:2]) * 60 + int(written[2:])
                  - int(despatch.groups[0].time[:2]) * 60
                  - int(despatch.groups[0].time[2:]))
        self.assertLessEqual(min(gap, 1440 - gap), 1)

    def test_no_document_carries_the_utc_day(self):
        for code in ("855", "865", "856"):
            with self.subTest(code=code):
                for segment in self.body(code).body:
                    self.assertNotIn(self.utc_day, segment.elements, segment.tag)


class AnEdifactDocumentIsDatedAsItsEnvelopeIs(_AwayFromGreenwich):

    def test_the_document_date_is_the_interchanges(self):
        self.send(edifact_order("PO-E-DATES"), headers=EDIFACT)
        documents = self.sent(EURODIS)
        for code in ("ORDRSP", "DESADV", "INVOIC"):
            with self.subTest(code=code):
                interchange = documents[code]
                message = list(interchange.messages())[0][1]
                written = [d.comp(1, 2) for d in message.find_all("DTM")
                           if d.comp(1, 1) == "137"][0]
                self.assertEqual(written, self.local_day)
                self.assertEqual(written[2:], interchange.date)


class AnAdviceSettlesByTheMocksOwnCalendar(_AwayFromGreenwich):
    """BPR16 names no zone: it is the day the mock would write today."""

    def rules(self, settles):
        status, _h, data = self.post(
            "/edi", x12_remittance(settles=settles), headers=X12)
        self.assertEqual(status, 200, data)
        return [d["rule"] for d in data["transactionSets"][0]["disagreements"]]

    def test_one_settling_today_by_the_hosts_calendar_has_settled(self):
        self.assertEqual(self.rules(self.local_day), [])

    def test_one_settling_tomorrow_by_the_hosts_calendar_has_not(self):
        tomorrow = (datetime.datetime.now().date()
                    + datetime.timedelta(days=1)).strftime("%Y%m%d")
        self.assertEqual(self.rules(tomorrow), ["remitted-before-settlement"])


del _AwayFromGreenwich

if __name__ == "__main__":
    unittest.main()
