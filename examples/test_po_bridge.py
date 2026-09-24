"""Integration tests for po_bridge, against mock-sap and mock-edi.

    pip install mock-sap
    mock-sap --port 8000 &
    python3 -m mockedi --port 8080 &
    cd examples && python3 -m unittest -v test_po_bridge

The walkthrough: https://rickseufert.com/blog/2026/09/24/testing-an-sap-to-edi-integration
"""
import json
import os
import unittest
import urllib.request

from po_bridge import PO_SERVICE, Bridge, Sap

SAP = os.environ.get("SAP_URL", "http://127.0.0.1:8000")
EDI = os.environ.get("EDI_URL", "http://127.0.0.1:8080")


def control(base, method, path, body=None):
    """Talk to a mock's /_mock control plane."""
    req = urllib.request.Request(base + path, method=method,
                                 data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as response:
        return json.loads(response.read() or "null")


class PurchaseOrderToSupplier(unittest.TestCase):

    def setUp(self):
        control(SAP, "POST", "/_mock/reset")
        control(EDI, "POST", "/_mock/reset")
        self.sap = Sap(SAP)
        self.bridge = Bridge(self.sap, EDI, our_id="ACME", supplier_id="MOCKEDI")
        # a fresh PO in SAP: 100 blue widgets and 40 brackets
        self.po = self.sap.request("POST", PO_SERVICE + "/A_PurchaseOrder", {
            "PurchaseOrderType": "NB", "CompanyCode": "1710",
            "PurchasingOrganization": "1710", "PurchasingGroup": "001",
            "Supplier": "1000012", "DocumentCurrency": "USD",
            "to_PurchaseOrderItem": [
                {"Material": "TG11", "OrderQuantity": "100", "NetPriceAmount": "12.50",
                 "PurchaseOrderQuantityUnit": "PC", "Plant": "1010"},
                {"Material": "TG12", "OrderQuantity": "40", "NetPriceAmount": "4.15",
                 "PurchaseOrderQuantityUnit": "PC", "Plant": "1010"},
            ]})["d"]["PurchaseOrder"]

    def supplier_behaves(self, behaviour):
        control(EDI, "PATCH", "/_mock/partners/ACME", {"behaviour": behaviour})

    def ordrsp_idocs(self):
        return control(SAP, "GET", "/_mock/idocs?mestyp=ORDRSP")["results"]

    def test_supplier_confirms_everything(self):
        summary = self.bridge.send(self.po)
        self.assertTrue(summary["accepted"])
        self.assertEqual(summary["transactionSets"][0]["findings"], [])

        result = self.bridge.receive()[self.po]
        self.assertEqual(result["lines"], {"00010": ("confirmed", 100.0),
                                           "00020": ("confirmed", 40.0)})
        self.assertEqual(result["exceptions"], [])
        self.assertEqual(len(self.ordrsp_idocs()), 1)

    def test_short_shipment_is_raised_as_an_exception(self):
        self.supplier_behaves("short-ship")
        self.bridge.send(self.po)

        result = self.bridge.receive()[self.po]
        self.assertNotEqual(result["exceptions"], [])
        for item in result["exceptions"]:
            status, quantity = result["lines"][item]
            self.assertEqual(status, "short")
            self.assertLess(quantity, {"00010": 100, "00020": 40}[item])

    def test_rejected_line_reaches_sap(self):
        self.supplier_behaves("reject-line")
        self.bridge.send(self.po)

        result = self.bridge.receive()[self.po]
        rejected = [i for i, (status, _) in result["lines"].items() if status == "rejected"]
        self.assertEqual(len(rejected), 1)
        # the IDoc SAP received says so too (E1EDP01 ACTION 003)
        idoc = control(SAP, "GET", "/sap/bc/idoc/" + result["idoc"])
        self.assertIn("<POSEX>%s</POSEX><ACTION>003</ACTION>" % rejected[0], idoc["payload"])

    def test_silent_supplier_leaves_nothing_to_post(self):
        self.supplier_behaves("no-ack")
        self.bridge.send(self.po)

        self.assertEqual(self.bridge.receive(), {})
        self.assertEqual(self.ordrsp_idocs(), [])

    def test_sap_outage_does_not_lose_the_confirmation(self):
        control(SAP, "POST", "/_mock/faults", {
            "match": "/sap/bc/idoc", "method": "POST", "status": 503,
            "message": "No dialog work process available", "count": 1})
        self.bridge.send(self.po)

        self.assertEqual(self.bridge.receive(), {})     # SAP was down
        self.assertEqual(len(self.bridge.pending), 1)   # but we kept the 855

        result = self.bridge.receive()                  # next run: SAP is back
        self.assertIn(self.po, result)
        self.assertEqual(self.bridge.pending, [])
        self.assertEqual(len(self.ordrsp_idocs()), 1)


if __name__ == "__main__":
    unittest.main()
