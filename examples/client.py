#!/usr/bin/env python3
"""Driving mock-edi from Python, with nothing installed.

This is the shape of a test: build an order, send it, read back what the
partner did with it, and assert on the documents rather than on the mock's
own summary of them.

    python3 -m mockedi --port 8080 &
    python3 examples/client.py

    BASE=http://host:9000 python3 examples/client.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("BASE", "http://127.0.0.1:8080")

ORDER = """ISA*00*          *00*          *ZZ*ACME           *ZZ*MOCKEDI        \
*260924*1030*U*00401*000000301*0*T*>~
GS*PO*ACME*MOCKEDI*20260924*1030*301*X*004010~
ST*850*0001~
BEG*00*SA*4500000701**20260924~
CUR*BY*USD~
DTM*002*20261010~
N1*ST*Acme DC 4*92*ACME-DC4~
N3*9 Dock Road~
N4*Columbus*OH*43217*US~
PO1*1*100*EA*12.50**VP*WIDGET-001~
PO1*2*40*EA*4.15**VP*BRKT-050~
CTT*2~
SE*11*0001~
GE*1*301~
IEA*1*000000301~
"""


def call(method, path, body=None, headers=None):
    data = body.encode() if isinstance(body, str) else (
        json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(BASE + path, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request) as response:
            payload = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        with error:
            payload = error.read().decode("utf-8", "replace")
    try:
        return json.loads(payload)
    except ValueError:
        return payload


def heading(text):
    print("\n\033[1m%s\033[0m" % text)


def main():
    heading("Send an 850")
    summary = call("POST", "/edi", ORDER,
                   {"Content-Type": "application/edi-x12"})
    if not isinstance(summary, dict) or not summary.get("accepted"):
        print("the mock did not accept the order:", summary)
        return 1
    print("  accepted by %s, order %s" % (summary["partner"], summary["orders"][0]))
    print("  queued in reply: %s"
          % ", ".join(item["code"] for item in summary["queued"]))

    heading("Collect what it sent back")
    documents = call("GET", "/_mock/mailbox?partner=ACME")
    for row in documents:
        print("  %-4s %-16s %s" % (row["code"], row["kind"], row["reference"]))

    heading("Read the 855 line by line")
    response = [row for row in documents if row["code"] == "855"][0]
    for line in response["payload"].splitlines():
        if line.startswith(("PO1", "ACK")):
            print("  " + line)

    heading("What the seller now thinks the order is")
    order = call("GET", "/_mock/orders/4500000701")
    print("  status %s, total %s" % (order["status"], order["total"]))
    for line in order["lines"]:
        print("  line %s %-12s ordered %-5s confirmed %-5s %s"
              % (line["line"], line["sku"], line["quantity"],
                 line["confirmed"], line["status"]))

    heading("Now make the partner reject a line, and order again")
    call("PATCH", "/_mock/partners/ACME", {"behaviour": "reject-line"})
    call("POST", "/edi", ORDER.replace("4500000701", "4500000702"),
         {"Content-Type": "application/edi-x12"})
    order = call("GET", "/_mock/orders/4500000702")
    for line in order["lines"]:
        print("  line %s %-12s %s  %s"
              % (line["line"], line["sku"], line["status"], line["reason"]))

    heading("The invoice bills only what shipped")
    invoices = call("GET", "/_mock/mailbox?partner=ACME&kind=invoice&leave")
    for row in invoices:
        for line in row["payload"].splitlines():
            if line.startswith(("IT1", "TDS")):
                print("  %s  %s" % (row["reference"], line))

    heading("Put it back")
    call("POST", "/_mock/reset")
    print("  reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
