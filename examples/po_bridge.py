"""po_bridge: send SAP purchase orders to a supplier over X12, and bring
the supplier's answer back into SAP.

    SAP PO (OData)  ->  850  ->  supplier
    SAP  <-  ORDRSP IDoc  <-  855  <-  supplier

An example of the code mock-edi exists to test, with mock-sap
(https://github.com/rseufert/mock-sap) standing in for SAP. The tests are in
test_po_bridge.py.
"""
import datetime
import http.cookiejar
import json
import urllib.error
import urllib.request

PO_SERVICE = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"

# SAP material -> the supplier's part number (in SAP this lives in the
# purchasing info record; here it is a dict)
SUPPLIER_PART = {"TG11": "WIDGET-001", "TG12": "BRKT-050", "TG14": "GEAR-100"}

# 855 ACK01 line status -> what we tell SAP
ACK_STATUS = {"IA": "confirmed", "IQ": "short", "IR": "rejected"}


class Sap:
    """Just enough of an OData/IDoc client: a session, a CSRF token."""

    def __init__(self, base):
        self.base = base
        jar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.token = None

    def request(self, method, path, body=None, content_type="application/json"):
        headers = {"Accept": "application/json"}
        if method != "GET":
            if self.token is None:
                self.token = self._fetch_token()
            headers["X-CSRF-Token"] = self.token
            headers["Content-Type"] = content_type
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data, headers, method=method)
        with self.http.open(req) as response:
            return json.loads(response.read())

    def _fetch_token(self):
        req = urllib.request.Request(self.base + PO_SERVICE + "/",
                                     headers={"X-CSRF-Token": "Fetch"})
        with self.http.open(req) as response:
            return response.headers["X-CSRF-Token"]

    def purchase_order(self, number):
        path = "%s/A_PurchaseOrder('%s')?$expand=to_PurchaseOrderItem" % (PO_SERVICE, number)
        return self.request("GET", path)["d"]


def build_850(po, sender, receiver, control, now=None):
    """Map an SAP purchase order onto an X12 004010 850."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    items = po["to_PurchaseOrderItem"]["results"]
    body = ["ST*850*0001",
            "BEG*00*SA*%s**%s" % (po["PurchaseOrder"], now.strftime("%Y%m%d")),
            "CUR*BY*%s" % po["DocumentCurrency"]]
    for item in items:
        # PO101 carries the SAP item number, so the 855 can be matched back
        body.append("PO1*%s*%d*EA*%s**VP*%s" % (
            item["PurchaseOrderItem"], float(item["OrderQuantity"]),
            item["NetPriceAmount"], SUPPLIER_PART[item["Material"]]))
    body.append("CTT*%d" % len(items))
    body.append("SE*%d*0001" % (len(body) + 1))
    isa = "ISA*00*%-10s*00*%-10s*ZZ*%-15s*ZZ*%-15s*%s*%s*U*00401*%09d*0*T*>" % (
        "", "", sender, receiver, now.strftime("%y%m%d"), now.strftime("%H%M"), control)
    gs = "GS*PO*%s*%s*%s*%s*%d*X*004010" % (
        sender, receiver, now.strftime("%Y%m%d"), now.strftime("%H%M"), control)
    trailer = ["GE*1*%d" % control, "IEA*1*%09d" % control]
    return "~\n".join([isa, gs] + body + trailer) + "~\n"


def parse_855(payload):
    """Read an 855 into {sap_item: (status, quantity)}."""
    lines, current = {}, None
    for segment in payload.replace("\n", "").split("~"):
        fields = segment.split("*")
        if fields[0] == "PO1":
            current = fields[1]
        elif fields[0] == "ACK" and current:
            lines[current] = (ACK_STATUS.get(fields[1], fields[1]),
                              float(fields[2] or 0))
    return lines


def ordrsp_idoc(po_number, lines):
    """The supplier's confirmation, as the ORDRSP IDoc SAP expects."""
    items = "".join(
        "<E1EDP01><POSEX>%s</POSEX><ACTION>%s</ACTION><MENGE>%.3f</MENGE></E1EDP01>"
        % (item, {"confirmed": "000", "short": "002", "rejected": "003"}[status], qty)
        for item, (status, qty) in sorted(lines.items()))
    return ("<ORDERS05><IDOC BEGIN=\"1\"><EDI_DC40 SEGMENT=\"1\">"
            "<IDOCTYP>ORDERS05</IDOCTYP><MESTYP>ORDRSP</MESTYP><DIRECT>2</DIRECT>"
            "</EDI_DC40><E1EDK01 SEGMENT=\"1\"><BELNR>%s</BELNR></E1EDK01>%s"
            "</IDOC></ORDERS05>" % (po_number, items))


class Bridge:
    def __init__(self, sap, edi_base, our_id, supplier_id):
        self.sap, self.edi_base = sap, edi_base
        self.our_id, self.supplier_id = our_id, supplier_id
        self.control = 0
        self.pending = []

    def _edi(self, method, path, body=None):
        req = urllib.request.Request(self.edi_base + path, method=method,
                                     data=body.encode() if body else None,
                                     headers={"Content-Type": "application/edi-x12"})
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read())

    def send(self, po_number):
        """Read the PO from SAP and send it to the supplier as an 850."""
        po = self.sap.purchase_order(po_number)
        self.control += 1
        return self._edi("POST", "/edi",
                         build_850(po, self.our_id, self.supplier_id, self.control))

    def receive(self):
        """Pick up the supplier's 855s and post each one into SAP.

        Collecting from the mailbox is destructive, so every 855 is kept in
        self.pending until SAP has taken it: an SAP outage delays a
        confirmation instead of losing it.

        Returns {po_number: {"lines": ..., "idoc": ..., "exceptions": [...]}}.
        """
        self.pending += [doc for doc in self._edi(
            "GET", "/_mock/mailbox?partner=%s" % self.our_id) if doc["code"] == "855"]
        results, still_pending = {}, []
        for doc in self.pending:
            po_number = doc["reference"]
            lines = parse_855(doc["payload"])
            try:
                receipt = self.sap.request("POST", "/sap/bc/idoc",
                                           ordrsp_idoc(po_number, lines), "application/xml")
            except urllib.error.HTTPError:
                still_pending.append(doc)          # try again next run
                continue
            results[po_number] = {
                "lines": lines,
                "idoc": receipt["DOCNUM"],
                "exceptions": [item for item, (status, _) in sorted(lines.items())
                               if status != "confirmed"],
            }
        self.pending = still_pending
        return results
