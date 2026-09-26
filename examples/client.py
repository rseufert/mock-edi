#!/usr/bin/env python3
"""Driving mock-edi from Python, with nothing installed.

This is the shape of a test: build an order, send it, read back what the
partner did with it, and assert on the documents rather than on the mock's
own summary of them.

    python3 -m mockedi --port 8080 &
    python3 examples/client.py

    BASE=http://host:9000 python3 examples/client.py

The HTTP is `mockedi.testing.Mock`, which the package ships so that nobody
has to write it: no urllib, no deciding whether the body is JSON, no
remembering to close an HTTPError. What is left below is about EDI.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mockedi.testing import Mock                       # noqa: E402

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


# A 997 accepting one transaction set, with the two control numbers left to
# be filled in from the document being acknowledged.
FUNCTIONAL_ACKNOWLEDGMENT = """\
ISA*00*          *00*          *ZZ*ACME           *ZZ*MOCKEDI        \
*260924*1040*U*00401*000000302*0*T*>~
GS*FA*ACME*MOCKEDI*20260924*1040*302*X*004010~
ST*997*0001~
AK1*PR*%s*004010~
AK2*855*%s~
AK5*A~
AK9*A*1*1*1~
SE*6*0001~
GE*1*302~
IEA*1*000000302~
"""


def heading(text):
    print("\n\033[1m%s\033[0m" % text)


def main():
    mock = Mock(BASE)

    heading("Send an 850")
    summary = mock.send(ORDER)
    print("  accepted by %s, order %s" % (summary["partner"], summary["orders"][0]))
    print("  queued in reply: %s"
          % ", ".join(item["code"] for item in summary["queued"]))

    heading("Collect what it sent back")
    documents = mock.mailbox(partner="ACME", leave=False)
    for row in documents:
        print("  %-4s %-16s %s" % (row["code"], row["kind"], row["reference"]))

    heading("Read the 855 line by line")
    response = [row for row in documents if row["code"] == "855"][0]
    for line in response["payload"].splitlines():
        if line.startswith(("PO1", "ACK")):
            print("  " + line)

    heading("What the seller now thinks the order is")
    order = mock.order("4500000701")
    print("  status %s, total %s" % (order["status"], order["total"]))
    for line in order["lines"]:
        print("  line %s %-12s ordered %-5s confirmed %-5s %s"
              % (line["line"], line["sku"], line["quantity"],
                 line["confirmed"], line["status"]))

    heading("Now make the partner reject a line, and order again")
    mock.behaviour("ACME", "reject-line")
    # A second order is a second interchange, and carries its own control
    # number: the mock refuses a replay of one it has already taken in, the
    # way a real partner does. 301 appears in ISA13, GS06, GE02 and IEA02.
    again = (ORDER.replace("4500000701", "4500000702")
                  .replace("000000301", "000000303")
                  .replace("*1030*301*", "*1030*303*")
                  .replace("GE*1*301~", "GE*1*303~"))
    mock.send(again)
    for line in mock.order("4500000702")["lines"]:
        print("  line %s %-12s %s  %s"
              % (line["line"], line["sku"], line["status"], line["reason"]))

    heading("Send a 997 back for the acknowledgment we were sent")
    # Building a receipt is fiddly in curl and half a dozen lines here: read
    # the control numbers out of the document the partner actually sent, which
    # is what a real translator does. Both are needed - ST02 is only unique
    # within its functional group.
    sent = mock.documents(direction="out", code="855", limit=1)[0]
    receipt = FUNCTIONAL_ACKNOWLEDGMENT % (sent["group_control"], sent["control"])
    answer = mock.send(receipt)
    for entry in answer["acknowledged"]:
        print("  %s %s -> %s (matched %s)"
              % (entry["code"], entry["control"], entry["status"], entry["matched"]))
    print("  still unacknowledged: %s"
          % (", ".join("%s for %s" % (row["code"], row["reference"])
                       for row in mock.unacknowledged()) or "nothing"))

    heading("The invoice bills only what shipped")
    for row in mock.mailbox(partner="ACME", kind="invoice"):
        for line in row["payload"].splitlines():
            if line.startswith(("IT1", "TDS")):
                print("  %s  %s" % (row["reference"], line))

    heading("Everything that happened to one order")
    for event in mock.timeline("4500000701")["events"]:
        print("  %s  %s" % (event["at"], event["summary"]))

    heading("Put it back")
    mock.reset()
    print("  reset")
    return 0


if __name__ == "__main__":
    sys.exit(main())
