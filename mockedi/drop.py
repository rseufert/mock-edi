"""Trading over a directory instead of over HTTP.

AS2 gets the attention, but a great deal of real EDI is still a folder: the
partner writes a file into it, you pick the file up; you write a file back,
they pick it up.  An integration whose inbound path is "watch a directory"
cannot be tested against an HTTP endpoint, so the mock grows the same two
halves a real drop has - an inbox it reads and an outbox it writes.

Two problems come with every directory-based integration, and both are
handled here rather than left to bite:

**Partial writes.**  A file still being written must not be read.  The
convention is write-then-rename, which makes the file appear complete or not
at all, but not every sender follows it - so a file whose modification time is
within the last few hundred milliseconds is left for the next pass, and
`.tmp`, `.part` and dotfiles are never read at all.  The mock writes its own
files with the rename, because a mock that does not follow the convention it
recommends is not much of an example.

**Reprocessing.**  A file that has been read must not be read again.  Read
files are moved into `processed/`, and files that could not be read at all
into `failed/` - moved rather than deleted, because a mock that eats the
evidence is no use when a test fails.

The poller is not the only way in.  `POST /_mock/drop/scan` scans once and
says what it found, for the same reason `POST /_mock/advance` exists: a test
that has to wait for a poll interval is slow and flaky, and one that asks for
a scan is neither.
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import db

PROCESSED = "processed"
FAILED = "failed"

# Never read: a sender's own temporary names, and anything hidden.
IGNORED_SUFFIXES = (".tmp", ".part", ".filepart", ".writing", ".swp")


@dataclass
class Scanned:
    """What became of one file."""
    name: str
    ok: bool
    moved_to: str = ""
    partner: str = ""
    dialect: str = ""
    orders: List[str] = field(default_factory=list)
    produced: List[str] = field(default_factory=list)
    error: str = ""


class DropBox:
    """An inbound directory the mock reads, and an outbound one it writes."""

    def __init__(self, pipeline, drop_dir: str = "", pickup_dir: str = "",
                 settle_ms: int = 250, interval_ms: int = 1000):
        self.pipeline = pipeline
        self.drop_dir = os.path.abspath(drop_dir) if drop_dir else ""
        self.pickup_dir = os.path.abspath(pickup_dir) if pickup_dir else ""
        self.settle_ms = settle_ms
        self.interval_ms = interval_ms
        # Replaced by the server with the mock's own lock.
        self.lock = threading.RLock()
        self.last_scan: List[Scanned] = []
        self.scans = 0
        self.written: List[str] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def active(self) -> bool:
        return bool(self.drop_dir or self.pickup_dir)

    # -- lifecycle

    def prepare(self) -> None:
        """Create the directories, so that a misspelled path fails loudly now."""
        for path in (self.drop_dir, self.pickup_dir):
            if path:
                os.makedirs(path, exist_ok=True)
        if self.drop_dir:
            for name in (PROCESSED, FAILED):
                os.makedirs(os.path.join(self.drop_dir, name), exist_ok=True)

    def start(self) -> None:
        if self._thread is not None or not self.drop_dir:
            return
        self._thread = threading.Thread(target=self._poll, daemon=True,
                                        name="mock-edi-dropbox")
        self._thread.start()

    def stop(self, wait: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=wait)
            self._thread = None

    def _poll(self) -> None:
        while not self._stop.wait(self.interval_ms / 1000.0):
            try:
                self.scan()
            except Exception:  # pragma: no cover - a poller must not die
                pass

    # -- inbound

    def ready(self) -> List[str]:
        """The files worth reading on this pass, oldest first.

        A file is skipped when it is hidden, carries a sender's temporary
        suffix, or was modified so recently that it may still be open.
        """
        if not self.drop_dir or not os.path.isdir(self.drop_dir):
            return []
        cutoff = time.time() - (self.settle_ms / 1000.0)
        out = []
        for name in sorted(os.listdir(self.drop_dir)):
            path = os.path.join(self.drop_dir, name)
            if not os.path.isfile(path):
                continue
            if name.startswith(".") or name.lower().endswith(IGNORED_SUFFIXES):
                continue
            try:
                if os.path.getmtime(path) > cutoff:
                    continue
            except OSError:              # vanished between listing and stat
                continue
            out.append(name)
        return out

    def scan(self) -> List[Scanned]:
        """Read every settled file in the drop directory, once."""
        results: List[Scanned] = []
        for name in self.ready():
            results.append(self._take(name))
        with self.lock:
            self.scans += 1
            if results:
                self.last_scan = results
        return results

    def _take(self, name: str) -> Scanned:
        path = os.path.join(self.drop_dir, name)
        try:
            with open(path, "rb") as handle:
                payload = handle.read()
        except OSError as error:
            return Scanned(name=name, ok=False, error=str(error))

        with self.lock:
            receipt = self.pipeline.receive(payload, transport="drop")

        if receipt.ok:
            result = Scanned(name=name, ok=True, partner=receipt.partner,
                             dialect=receipt.dialect, orders=list(receipt.orders),
                             produced=[q.code for q in receipt.queued])
        else:
            result = Scanned(name=name, ok=False, error=receipt.error,
                             partner=receipt.partner, dialect=receipt.dialect)
        result.moved_to = self._file_away(path, name,
                                          PROCESSED if receipt.ok else FAILED)
        return result

    def _file_away(self, path: str, name: str, folder: str) -> str:
        """Move a read file out of the way, without ever overwriting one."""
        target_dir = os.path.join(self.drop_dir, folder)
        try:
            os.makedirs(target_dir, exist_ok=True)
            stem, extension = os.path.splitext(name)
            candidate = os.path.join(target_dir, name)
            counter = 1
            while os.path.exists(candidate):
                candidate = os.path.join(
                    target_dir, "%s-%d%s" % (stem, counter, extension))
                counter += 1
            os.replace(path, candidate)
            return os.path.join(folder, os.path.basename(candidate))
        except OSError as error:         # pragma: no cover - permissions
            return "could not move: %s" % error

    # -- outbound

    def write(self, outbound_ids: List[int]) -> List[str]:
        """Write released documents into the pickup directory.

        Written to a temporary name and renamed, so that whatever is watching
        the directory never sees a half-written file - the convention this
        module asks senders to follow.
        """
        if not self.pickup_dir:
            return []
        written: List[str] = []
        for outbound_id in outbound_ids:
            with self.lock:
                row = db.one(self.pipeline.conn,
                             "SELECT * FROM outbound WHERE id = ?", (outbound_id,))
            if row is None:
                continue
            name = "%s-%s-%s.edi" % (row["partner"], row["code"], row["control"])
            final = os.path.join(self.pickup_dir, name)
            temporary = final + ".tmp"
            try:
                os.makedirs(self.pickup_dir, exist_ok=True)
                with open(temporary, "w", encoding="utf-8") as handle:
                    handle.write(row["payload"])
                os.replace(temporary, final)
            except OSError:              # pragma: no cover - permissions
                continue
            written.append(name)
        with self.lock:
            self.written.extend(written)
        return written

    # -- reporting

    def state(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "dropDir": self.drop_dir,
                "pickupDir": self.pickup_dir,
                "polling": self._thread is not None,
                "intervalMs": self.interval_ms,
                "settleMs": self.settle_ms,
                "scans": self.scans,
                "waiting": self.ready(),
                "written": list(self.written[-20:]),
                "lastScan": [vars(item) for item in self.last_scan],
            }
