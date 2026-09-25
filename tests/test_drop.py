"""Trading over a directory: the inbox, the outbox, and the two classic traps."""
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, EURODIS, MockServerCase, edifact_order, x12_order

ROOT = tempfile.mkdtemp(prefix="mock-edi-drop-")
DROP = os.path.join(ROOT, "in")
PICKUP = os.path.join(ROOT, "out")


class DirectoryCase(MockServerCase):
    config_kwargs = {"drop_dir": DROP, "pickup_dir": PICKUP,
                     # Long enough that the poller never fires during a test:
                     # every test here drives the scan endpoint on purpose.
                     "drop_interval_ms": 3600000,
                     "drop_settle_ms": 0}

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(ROOT, ignore_errors=True)

    def setUp(self):
        for folder in (DROP, PICKUP):
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder)
        for folder in ("processed", "failed"):
            os.makedirs(os.path.join(DROP, folder), exist_ok=True)
        super().setUp()

    # -- helpers

    def drop_file(self, name, payload):
        path = os.path.join(DROP, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(payload)
        return path

    def scan(self):
        status, _headers, data = self.post("/_mock/drop/scan")
        self.assertEqual(status, 200, data)
        return data

    def pickup_files(self):
        return sorted(name for name in os.listdir(PICKUP)
                      if not name.endswith(".tmp"))

    def processed(self):
        return sorted(os.listdir(os.path.join(DROP, "processed")))

    def failed(self):
        return sorted(os.listdir(os.path.join(DROP, "failed")))


class ReadingTheDropDirectory(DirectoryCase):
    def test_a_dropped_order_goes_through_the_same_pipeline_as_a_post(self):
        self.drop_file("order.edi", x12_order("PO-DROP"))
        result = self.scan()
        self.assertEqual(result["scanned"], 1)
        self.assertTrue(result["files"][0]["ok"])
        self.assertEqual(result["files"][0]["partner"], ACME)
        self.assertEqual(result["files"][0]["orders"], ["PO-DROP"])
        self.assertEqual(result["files"][0]["produced"],
                         ["997", "855", "856", "810"])

    def test_the_order_is_recorded_and_fulfilled(self):
        self.drop_file("order.edi", x12_order("PO-DROP"))
        self.scan()
        order = self.order("PO-DROP")
        self.assertEqual(order["status"], "invoiced")
        self.assertEqual(order["total"], "1416.00")

    def test_the_interchange_records_how_it_arrived(self):
        self.drop_file("order.edi", x12_order("PO-DROP"))
        self.scan()
        _status, _headers, rows = self.get("/_mock/interchanges")
        inbound = [row for row in rows if row["direction"] == "in"]
        self.assertEqual(inbound[0]["transport"], "drop")

    def test_edifact_is_recognised_from_the_file_like_anything_else(self):
        self.drop_file("order.txt", edifact_order("PO-DROP-E"))
        result = self.scan()
        self.assertEqual(result["files"][0]["dialect"], "EDIFACT")
        self.assertEqual(result["files"][0]["partner"], EURODIS)

    def test_several_files_are_read_in_one_pass_oldest_name_first(self):
        self.drop_file("a-first.edi", x12_order("PO-A"))
        self.drop_file("b-second.edi", x12_order("PO-B"))
        result = self.scan()
        self.assertEqual([f["name"] for f in result["files"]],
                         ["a-first.edi", "b-second.edi"])
        self.assertEqual(self.order("PO-A")["status"], "invoiced")
        self.assertEqual(self.order("PO-B")["status"], "invoiced")

    def test_an_empty_directory_is_not_an_error(self):
        result = self.scan()
        self.assertEqual(result["scanned"], 0)


class NotReadingFilesTwice(DirectoryCase):
    def test_a_read_file_is_moved_to_processed(self):
        self.drop_file("order.edi", x12_order("PO-ONCE"))
        self.scan()
        self.assertEqual(self.processed(), ["order.edi"])
        self.assertNotIn("order.edi", os.listdir(DROP))

    def test_a_second_scan_finds_nothing_to_do(self):
        self.drop_file("order.edi", x12_order("PO-ONCE"))
        self.scan()
        self.assertEqual(self.scan()["scanned"], 0)

    def test_a_file_that_could_not_be_read_is_moved_to_failed(self):
        self.drop_file("rubbish.edi", "this is not an interchange")
        result = self.scan()
        self.assertFalse(result["files"][0]["ok"])
        self.assertIn("ISA", result["files"][0]["error"])
        self.assertEqual(self.failed(), ["rubbish.edi"])

    def test_an_unregistered_sender_fails_the_file_rather_than_losing_it(self):
        self.drop_file("stranger.edi", x12_order("PO-X", sender="STRANGER"))
        result = self.scan()
        self.assertFalse(result["files"][0]["ok"])
        self.assertEqual(self.failed(), ["stranger.edi"])

    def test_the_same_name_twice_does_not_overwrite_the_evidence(self):
        self.drop_file("order.edi", x12_order("PO-ONCE"))
        self.scan()
        self.drop_file("order.edi", x12_order("PO-TWICE"))
        self.scan()
        self.assertEqual(self.processed(), ["order-1.edi", "order.edi"])


class NotReadingHalfWrittenFiles(DirectoryCase):
    config_kwargs = dict(DirectoryCase.config_kwargs, drop_settle_ms=10000)

    def test_a_file_that_has_just_been_touched_is_left_for_the_next_pass(self):
        self.drop_file("order.edi", x12_order("PO-SETTLE"))
        self.assertEqual(self.scan()["scanned"], 0)
        self.assertIn("order.edi", os.listdir(DROP))

    def test_it_is_read_once_it_has_settled(self):
        path = self.drop_file("order.edi", x12_order("PO-SETTLE"))
        old = time.time() - 60
        os.utime(path, (old, old))
        self.assertEqual(self.scan()["scanned"], 1)

    def test_the_state_endpoint_shows_what_is_waiting(self):
        path = self.drop_file("order.edi", x12_order("PO-SETTLE"))
        _status, _headers, state = self.get("/_mock/drop")
        self.assertEqual(state["waiting"], [])       # not settled yet
        old = time.time() - 60
        os.utime(path, (old, old))
        _status, _headers, state = self.get("/_mock/drop")
        self.assertEqual(state["waiting"], ["order.edi"])


class IgnoredNames(DirectoryCase):
    def test_a_senders_temporary_suffix_is_never_read(self):
        for name in ("order.edi.tmp", "order.edi.part", "order.edi.filepart"):
            self.drop_file(name, x12_order("PO-TEMP"))
        self.assertEqual(self.scan()["scanned"], 0)

    def test_a_dotfile_is_never_read(self):
        self.drop_file(".order.edi", x12_order("PO-HIDDEN"))
        self.assertEqual(self.scan()["scanned"], 0)

    def test_the_processed_and_failed_folders_are_not_re_read(self):
        self.drop_file("order.edi", x12_order("PO-ONCE"))
        self.scan()
        self.assertEqual(self.scan()["scanned"], 0)


class NothingLandsOutsideThePickupDirectory(DirectoryCase):
    """Even for a partner whose id was never checked - one from an old file."""

    def test_a_partner_id_with_a_path_in_it_is_refused_not_followed(self):
        conn = self.httpd.mock.conn
        with self.httpd.mock.lock:
            row = dict(conn.execute(
                "SELECT * FROM partner WHERE id = ?", (ACME,)).fetchone())
            row["id"] = "../../trav"
            conn.execute("INSERT INTO partner (%s) VALUES (%s)" % (
                ", ".join(row), ", ".join("?" for _ in row)), list(row.values()))
            conn.commit()
        self.send(x12_order("PO-TRAVERSE", sender="../../trav"))

        above = os.path.normpath(os.path.join(PICKUP, "..", ".."))
        self.assertEqual([n for n in os.listdir(above) if n.startswith("trav-")], [])
        self.assertEqual(self.pickup_files(), [])
        _status, _headers, state = self.get("/_mock/drop")
        self.assertTrue(state["refused"])
        self.assertTrue(all(name.startswith("../../trav-")
                            for name in state["refused"]))


class WritingThePickupDirectory(DirectoryCase):
    def test_released_documents_are_written_out(self):
        self.send(x12_order("PO-PICKUP"))
        names = self.pickup_files()
        self.assertEqual(len(names), 4)
        self.assertTrue(all(name.startswith("ACME-") for name in names))

    def test_the_name_says_who_what_and_which_interchange(self):
        self.send(x12_order("PO-PICKUP"))
        self.assertTrue(any(name.startswith("ACME-810-") and name.endswith(".edi")
                            for name in self.pickup_files()),
                        self.pickup_files())

    def test_the_contents_are_the_interchange(self):
        self.send(x12_order("PO-PICKUP"))
        invoice = [n for n in self.pickup_files() if "-810-" in n][0]
        with open(os.path.join(PICKUP, invoice), encoding="utf-8") as handle:
            payload = handle.read()
        self.assertTrue(payload.startswith("ISA*00*"))
        self.assertIn("TDS*141600", payload)

    def test_nothing_is_left_half_written(self):
        self.send(x12_order("PO-PICKUP"))
        self.assertEqual([n for n in os.listdir(PICKUP) if n.endswith(".tmp")], [])

    def test_a_delayed_document_appears_only_when_it_is_released(self):
        # Nothing to assert about ordering here beyond this: the pickup
        # directory follows the queue, it does not bypass it.
        self.send(x12_order("PO-PICKUP"))
        before = len(self.pickup_files())
        self.post("/_mock/send", {"partner": ACME, "kind": "invoice",
                                  "order": "PO-PICKUP"})
        self.assertEqual(len(self.pickup_files()), before + 1)

    def test_a_dropped_order_is_answered_into_the_pickup_directory(self):
        """The whole loop, with no HTTP at all in the middle."""
        self.drop_file("order.edi", x12_order("PO-LOOP"))
        self.scan()
        names = self.pickup_files()
        self.assertEqual(sorted(n.split("-")[1] for n in names),
                         ["810", "855", "856", "997"])


class WithoutADropDirectory(MockServerCase):
    def test_scanning_says_so_rather_than_pretending(self):
        status, _headers, data = self.post("/_mock/drop/scan")
        self.assertEqual(status, 409)
        self.assertIn("--drop-dir", data["error"])

    def test_the_state_endpoint_still_answers(self):
        status, _headers, data = self.get("/_mock/drop")
        self.assertEqual(status, 200)
        self.assertEqual(data["dropDir"], "")
        self.assertFalse(data["polling"])


class ThePoller(unittest.TestCase):
    """One test for the thread. Everything else uses the scan endpoint."""

    def test_it_reads_the_directory_without_being_asked(self):
        from mockedi.server import Config, make_server
        import threading
        root = tempfile.mkdtemp(prefix="mock-edi-poll-")
        try:
            httpd = make_server(Config(
                host="127.0.0.1", port=0, quiet=True,
                drop_dir=os.path.join(root, "in"),
                drop_interval_ms=50, drop_settle_ms=0))
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                with open(os.path.join(root, "in", "order.edi"), "w",
                          encoding="utf-8") as handle:
                    handle.write(x12_order("PO-POLLED"))
                # Wait for the *finished* state, not merely for the row to
                # appear: the order is inserted as `received` and advanced to
                # `shipped` and then `invoiced` in separate commits, so a
                # reader on another thread can catch it part way through.
                # Read under the mock's lock, as every thread in it does: the
                # poller is writing through this same connection, and two
                # threads in one SQLite connection at once is API misuse.
                deadline = time.time() + 15
                row = None
                while time.time() < deadline:
                    with httpd.mock.lock:
                        row = httpd.mock.conn.execute(
                            "SELECT * FROM purchase_order WHERE po_number = ?",
                            ("PO-POLLED",)).fetchone()
                    if row is not None and row["status"] == "invoiced":
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(row, "the poller never read the file")
                self.assertEqual(row["status"], "invoiced")
            finally:
                httpd.shutdown()
                httpd.server_close()
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
