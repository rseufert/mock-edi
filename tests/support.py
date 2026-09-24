"""Shared harness for the end-to-end tests.

Every test in this suite talks to a real mock over real HTTP: MockServerCase
starts one in a background thread on an ephemeral port, and subclasses point
`config_kwargs` at whatever configuration the surface under test needs.

The builders below exist so that a test can say what it is about - "an order
for two lines, one of them unknown" - without forty lines of segment
construction in the way.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mockedi import edifact, x12                      # noqa: E402
from mockedi.envelope import seg                      # noqa: E402
from mockedi.server import Config, make_server        # noqa: E402

ACME = "ACME"          # X12, accepts everything
GLOBEX = "GLOBEX"      # X12 005010, short-ships
INITECH = "INITECH"    # X12, rejects a line
EURODIS = "EURODIS"    # EDIFACT


class MockServerCase(unittest.TestCase):
    config_kwargs: dict = {}

    @classmethod
    def setUpClass(cls):
        kwargs = dict(host="127.0.0.1", port=0, db_path=":memory:", quiet=True)
        kwargs.update(cls.config_kwargs)
        cls.httpd = make_server(Config(**kwargs))
        cls.port = cls.httpd.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        # Each test starts from a freshly seeded partner, so that one test
        # changing a behaviour cannot reach the next.
        self.post("/_mock/reset")

    # -- HTTP

    def request(self, method, path, body=None, headers=None, raw=False):
        url = self.base + path.replace(" ", "%20")
        data = body
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str):
            data = data.encode()
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return resp.status, dict(resp.headers), payload if raw else _maybe_json(payload)
        except urllib.error.HTTPError as err:
            # Read *and* close: an unclosed HTTPError leaves a temporary file
            # behind and turns every negative test into a ResourceWarning.
            with err:
                payload = err.read()
            return err.code, dict(err.headers), payload if raw else _maybe_json(payload)

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def post(self, path, body=None, **kw):
        return self.request("POST", path, body, **kw)

    def patch(self, path, body=None, **kw):
        return self.request("PATCH", path, body, **kw)

    # -- convenience

    def send(self, payload, headers=None):
        """POST an interchange to the plain endpoint and return the summary."""
        status, _headers, data = self.post(
            "/edi", payload, headers=headers or {"Content-Type": "application/edi-x12"})
        self.assertEqual(status, 200, data)
        return data

    def behaviour(self, partner, name):
        status, _headers, data = self.patch("/_mock/partners/" + partner,
                                            {"behaviour": name})
        self.assertEqual(status, 200, data)
        return data

    def mailbox(self, partner="", kind="", leave=True):
        query = ["leave"] if leave else []
        if partner:
            query.append("partner=" + partner)
        if kind:
            query.append("kind=" + kind)
        path = "/_mock/mailbox" + ("?" + "&".join(query) if query else "")
        status, _headers, data = self.get(path)
        self.assertEqual(status, 200, data)
        return data

    def document(self, partner="", kind="", leave=True):
        """The one document of a kind waiting for a partner, parsed."""
        rows = self.mailbox(partner, kind, leave)
        self.assertEqual(len(rows), 1,
                         "expected one %s for %s, got %d"
                         % (kind or "document", partner or "anyone", len(rows)))
        return parse(rows[0]["payload"])

    def settle(self, timeout=30.0):
        """Wait until nothing in the outbox is still waiting to be delivered.

        Not a sleep, and not a fixed number of drains: on some platforms a
        connection to a dead port takes seconds to be refused rather than
        failing immediately, so the only reliable signal is the state itself.
        """
        deadline = time.time() + timeout
        rows = []
        self.httpd.mock.courier.drain(timeout)
        while time.time() < deadline:
            _status, _headers, rows = self.get("/_mock/outbox")
            if all(row["status"] != "ready" for row in rows):
                return rows
            time.sleep(0.05)
        self.fail("the outbox still held undelivered documents after %gs: %s"
                  % (timeout, [(r["code"], r["status"]) for r in rows]))

    def order(self, po_number):
        status, _headers, data = self.get("/_mock/orders/" + po_number)
        self.assertEqual(status, 200, data)
        return data


def _maybe_json(payload: bytes):
    try:
        return json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return payload.decode("utf-8", "replace")


def parse(payload: str):
    from mockedi.envelope import sniff
    return (x12.parse(payload) if sniff(payload) == "X12"
            else edifact.parse(payload))


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

DEFAULT_LINES = (("WIDGET-001", 100, "12.50"), ("BRKT-050", 40, "4.15"))


def x12_order(po_number="4500000001", lines=DEFAULT_LINES, sender=ACME,
              receiver="MOCKEDI", control="000000077", group="77",
              ordered_on="20260924", purpose="00", qualifier="VP", extra=()):
    """An 850, as a rendered interchange."""
    body = [seg("BEG", purpose, "SA", po_number, "", ordered_on),
            seg("CUR", "BY", "USD"),
            seg("DTM", "002", "20261010"),
            seg("N1", "ST", "Acme DC 4", "92", "ACME-DC4"),
            seg("N3", "9 Dock Road"),
            seg("N4", "Columbus", "OH", "43217", "US")]
    for index, (sku, quantity, price) in enumerate(lines, start=1):
        body.append(seg("PO1", str(index), str(quantity), "EA", price, "",
                        qualifier, sku))
    body.extend(extra)
    body.append(seg("CTT", str(len(lines))))
    return x12.render(x12.wrap([x12.message("850", "0001", body)], sender, receiver,
                               control, group, "PO"), newline=True)


def edifact_order(po_number="PO-2026-00001", lines=DEFAULT_LINES, sender=EURODIS,
                  receiver="MOCKEDI", control="9001", extra=()):
    """An ORDERS, as a rendered interchange."""
    body = [seg("BGM", ["220"], [po_number], "9"),
            seg("DTM", ["137", "20260924", "102"]),
            seg("DTM", ["2", "20261010", "102"]),
            seg("NAD", "BY", ["EURODIS", "", "92"], "", ["Eurodis Handels GmbH"],
                ["Hafenstrasse 12"], "Hamburg", "HH", "20457", "DE"),
            seg("NAD", "DP", ["EURODIS-L1", "", "92"], "", ["Eurodis Lager 1"],
                ["Am Kai 4"], "Hamburg", "HH", "20459", "DE"),
            seg("CUX", ["2", "EUR", "9"])]
    for index, (sku, quantity, price) in enumerate(lines, start=1):
        body.append(seg("LIN", str(index), "", [sku, "VP"]))
        body.append(seg("QTY", ["21", str(quantity), "PCE"]))
        body.append(seg("PRI", ["AAA", price]))
    body.extend(extra)
    body.append(seg("UNS", "S"))
    body.append(seg("CNT", ["2", str(len(lines))]))
    return edifact.render(edifact.wrap([edifact.message("ORDERS", "1", body)],
                                       sender, receiver, control), newline=True)


def x12_change(po_number="4500000001", lines=(("1", "CA", 60, "12.50"),),
               sender=ACME, receiver="MOCKEDI", control="000000078",
               group="78", purpose="04", sequence="1",
               ordered_on="20260924", qualifier="VP", skus=None):
    """An 860. Each line is `(line_number, change_code, quantity, price)`."""
    body = [seg("BCH", purpose, "SA", po_number, "", sequence, "20260925", "",
                "", "", ordered_on)]
    names = skus or {}
    for number, action, quantity, price in lines:
        body.append(seg("POC", number, action, str(quantity), "", "EA",
                        str(price), "", qualifier,
                        names.get(number, "WIDGET-001")))
    body.append(seg("CTT", str(len(lines))))
    return x12.render(x12.wrap([x12.message("860", "0001", body)], sender,
                               receiver, control, group, "PC"), newline=True)


def edifact_change(po_number="PO-2026-00001", lines=(("1", "3", 60, "12.50"),),
                   sender=EURODIS, receiver="MOCKEDI", control="9002",
                   purpose="4", skus=None):
    """An ORDCHG. Each line is `(line_number, 1229 action, quantity, price)`."""
    body = [seg("BGM", ["230"], [po_number], purpose),
            seg("DTM", ["137", "20260925", "102"]),
            seg("RFF", ["ON", po_number])]
    names = skus or {}
    for number, action, quantity, price in lines:
        body.append(seg("LIN", number, action,
                        [names.get(number, "WIDGET-001"), "VP"]))
        body.append(seg("QTY", ["21", str(quantity), "PCE"]))
        body.append(seg("PRI", ["AAA", str(price)]))
    body.append(seg("UNS", "S"))
    return edifact.render(edifact.wrap(
        [edifact.message("ORDCHG", "1", body)], sender, receiver, control),
        newline=True)


def as2_headers(sender=ACME, receiver="MOCKEDI", message_id="<m1@acme.example>",
                mdn=True, micalg="sha256", async_url=""):
    headers = {"Content-Type": "application/edi-x12",
               "AS2-From": sender, "AS2-To": receiver,
               "AS2-Version": "1.2", "Message-ID": message_id}
    if mdn:
        headers["Disposition-Notification-To"] = "edi@example.test"
        headers["Disposition-Notification-Options"] = (
            "signed-receipt-protocol=optional, pkcs7-signature; "
            "signed-receipt-micalg=optional, %s" % micalg)
    if async_url:
        headers["Receipt-Delivery-Option"] = async_url
    return headers


def acknowledge(payload: str, verdict: str = "A", errors=(), control: str = "7001"):
    """Build the acknowledgment a partner would send for `payload`.

    Reads the control numbers out of the document the mock actually sent
    rather than assuming them, which is what a real partner's translator does
    and what makes these tests prove the matching works.

    `errors` is a list of `(segment_tag, position, segment_error_code,
    element_position, element_error_code, bad_value)` tuples, rendered as
    AK3/AK4 in X12 and UCS/UCD in EDIFACT.
    """
    interchange = parse(payload)
    if interchange.dialect == "X12":
        return _x12_997(interchange, verdict, errors, control)
    return _edifact_contrl(interchange, verdict, errors, control)


def _x12_997(interchange, verdict, errors, control):
    accepted = 0
    body = []
    for group in interchange.groups:
        body.append(seg("AK1", group.functional_id, group.control, group.version))
        for message in group.messages:
            body.append(seg("AK2", message.code, message.control))
            for tag, position, segment_code, element, element_code, value in errors:
                body.append(seg("AK3", tag, str(position), "", segment_code))
                if element:
                    body.append(seg("AK4", str(element), "", element_code, value))
            body.append(seg("AK5", verdict))
            if verdict in ("A", "E"):
                accepted += 1
        body.append(seg("AK9", verdict, str(len(group.messages)),
                        str(len(group.messages)), str(accepted)))
    return x12.render(x12.wrap(
        [x12.message("997", control, body)],
        interchange.receiver, interchange.sender, control, control, "FA"),
        newline=True)


def _edifact_contrl(interchange, verdict, errors, control):
    action = "7" if verdict in ("A", "E", "7") else "4"
    body = [seg("UCI", interchange.control,
                [interchange.sender, interchange.sender_qualifier],
                [interchange.receiver, interchange.receiver_qualifier], action)]
    for group in interchange.groups:
        for message in group.messages:
            version = (message.version or "D:96A:UN").split(":")
            body.append(seg("UCM", message.control,
                            [message.code] + version[:3], action))
            for tag, position, segment_code, element, element_code, _value in errors:
                body.append(seg("UCS", str(position), segment_code))
                if element:
                    body.append(seg("UCD", element_code, [str(element), "1"]))
    return edifact.render(edifact.wrap(
        [edifact.message("CONTRL", control, body, "D:3:UN")],
        interchange.receiver, interchange.sender, control), newline=True)
