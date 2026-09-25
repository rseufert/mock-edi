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
evidence is no use when a test fails.  Two things used to break that promise,
and both are closed:

* *Two scanners.*  The poller and `POST /_mock/drop/scan` could read the same
  file at once.  A file is now *claimed* before it is read - renamed to
  `<name>.processing`, which only one caller can win - and scans take turns.
* *A file that cannot be moved.*  If `processed/` is not writable the file
  used to stay put and be read again on every pass.  It is now remembered by
  name, size and modification time and left alone until it changes, and it
  is reported in `GET /_mock/drop` and on stderr rather than retried in
  silence.

The poller is not the only way in.  `POST /_mock/drop/scan` scans once and
says what it found, for the same reason `POST /_mock/advance` exists: a test
that has to wait for a poll interval is slow and flaky, and one that asks for
a scan is neither.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import db

PROCESSED = "processed"
FAILED = "failed"
NOT_MOVED = "could not move: "

# A file this mock has claimed and is reading.
CLAIMED = ".processing"

# Never read: a sender's own temporary names, the mock's claim, and anything
# hidden.
IGNORED_SUFFIXES = (".tmp", ".part", ".filepart", ".writing", ".swp", CLAIMED)


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
        # Names that would have landed outside the pickup directory.
        self.refused: List[str] = []
        # Files read once that could not be moved out of the way, keyed by
        # name, with the size and modification time they had and why. Left
        # alone until either changes.
        self.stuck: Dict[str, Tuple[int, float, str]] = {}
        # Names that were already taken in the pickup directory, and what was
        # written instead: the wire-visible sign of a control number reused,
        # after a reset or by a partner's behaviour.
        self.renamed: List[Dict[str, str]] = []
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

    def stop(self, wait: float = 2.0) -> bool:
        """Ask the poller to stop, and say whether it did within `wait`."""
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout=wait)
        if thread.is_alive():
            return False
        self._thread = None
        return True

    def _poll(self) -> None:
        while not self._stop.wait(self.interval_ms / 1000.0):
            try:
                self.scan()
            except Exception as error:  # pragma: no cover - must not die
                # Said, not swallowed: a poller that fails in silence is how
                # a file got read eleven times with nothing in the log.
                sys.stderr.write("mock-edi: drop scan failed: %s\n" % error)

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
            # A settle time of zero means no waiting at all, and is checked
            # for explicitly rather than falling out of the arithmetic: a
            # file's modification time can read as very slightly *ahead* of
            # the clock, so `mtime > now` is true for a file just written and
            # zero would otherwise mean "never ready" rather than "always".
            try:
                stat = os.stat(path)
            except OSError:              # vanished between listing and stat
                continue
            if self.settle_ms and stat.st_mtime > cutoff:
                continue
            if name in self.stuck:
                size, mtime, _reason = self.stuck[name]
                if (stat.st_size, stat.st_mtime) == (size, mtime):
                    continue             # read once already; unchanged since
                del self.stuck[name]     # changed: a new file by the same name
            out.append(name)
        return out

    def scan(self) -> List[Scanned]:
        """Read every settled file in the drop directory, once.

        Scans take turns under the mock's own lock. The scan endpoint already
        holds it - every HTTP request does - so a second lock for scans alone
        would be taken in the opposite order by the poller, and the two would
        deadlock the first time they met.
        """
        with self.lock:
            results: List[Scanned] = []
            for name in self.ready():
                taken = self._take(name)
                if taken is not None:
                    results.append(taken)
            with self.lock:
                self.scans += 1
                if results:
                    self.last_scan = results
            return results

    def _take(self, name: str) -> Optional[Scanned]:
        path = os.path.join(self.drop_dir, name)
        # Claim it first. Whoever wins the rename owns the file; anyone else
        # finds it gone. A directory the mock may not rename in cannot be
        # claimed, and is read where it is: scans taking turns and the stuck
        # list still see to it that it is read once.
        working = path + CLAIMED
        try:
            os.replace(path, working)
        except FileNotFoundError:
            return None
        except OSError:
            working = path
        try:
            with open(working, "rb") as handle:
                payload = handle.read()
        except OSError as error:
            self._release(working, path)
            return Scanned(name=name, ok=False, error=str(error))

        with self.lock:
            receipts = self.pipeline.receive(payload, transport="drop")

        # A dropped file may hold several interchanges. It is filed as
        # processed only when every one of them was read: a file with one bad
        # interchange in it is a file somebody needs to look at.
        refused = [receipt for receipt in receipts if not receipt.ok]
        if not refused:
            result = Scanned(name=name, ok=True, partner=receipts[0].partner,
                             dialect=receipts[0].dialect,
                             orders=[po for r in receipts for po in r.orders],
                             produced=[q.code for r in receipts for q in r.queued])
        else:
            result = Scanned(name=name, ok=False, error=refused[0].error,
                             partner=refused[0].partner, dialect=refused[0].dialect)
        result.moved_to = self._file_away(working, name,
                                          PROCESSED if not refused else FAILED)
        if result.moved_to.startswith(NOT_MOVED):
            self._remember_stuck(working, path, name, result.moved_to)
        return result

    def _release(self, working: str, path: str) -> None:
        """Give a claimed file its own name back."""
        if working != path:
            try:
                os.replace(working, path)
            except OSError:              # pragma: no cover - left claimed,
                pass                     # which is ignored: still read once

    def _remember_stuck(self, working: str, path: str, name: str,
                        reason: str) -> None:
        """A file read once that could not be put away: never read it again
        until it changes, and say so."""
        self._release(working, path)
        try:
            stat = os.stat(path if os.path.exists(path) else working)
        except OSError:                  # pragma: no cover - gone after all
            return
        with self.lock:
            self.stuck[name] = (stat.st_size, stat.st_mtime, reason)
        sys.stderr.write("mock-edi: read %s but %s; it will not be read again "
                         "until it changes\n" % (name, reason))

    def _file_away(self, path: str, name: str, folder: str) -> str:
        """Move a read file out of the way, without ever overwriting one."""
        target_dir = os.path.join(self.drop_dir, folder)
        try:
            os.makedirs(target_dir, exist_ok=True)
            candidate = _free_name(os.path.join(target_dir, name))
            os.replace(path, candidate)
            return os.path.join(folder, os.path.basename(candidate))
        except OSError as error:         # pragma: no cover - permissions
            return "%s%s" % (NOT_MOVED, error)

    # -- outbound

    def write(self, outbound_ids: List[int]) -> List[str]:
        """Write released documents into the pickup directory.

        Written to a temporary name and renamed, so that whatever is watching
        the directory never sees a half-written file - the convention this
        module asks senders to follow.

        Nothing already there is overwritten. A reset starts the control
        numbers again, so a name can recur while the file from before is
        still waiting to be collected; the new one is suffixed, as a filed-away
        drop is, and the collision is reported in `renamed`.
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
            final = self._inside_pickup(name)
            if final is None:
                self.refused.append(name)
                continue
            temporary = final + ".tmp"
            try:
                os.makedirs(self.pickup_dir, exist_ok=True)
                with self.lock:
                    body, _charset = self.pipeline.wire(row)
                with open(temporary, "wb") as handle:
                    handle.write(body)
                target = _free_name(final)
                os.replace(temporary, target)
            except OSError:              # pragma: no cover - permissions
                continue
            written_as = os.path.basename(target)
            if written_as != name:
                with self.lock:
                    self.renamed.append({"name": name, "writtenAs": written_as})
            written.append(written_as)
        with self.lock:
            self.written.extend(written)
        return written

    def _inside_pickup(self, name: str) -> Optional[str]:
        """The path to write `name` to, or None if it would leave the directory.

        Partner ids are checked when a partner is created, but a row from an
        older database, or one written some other way, has not been: a name
        with a separator in it is refused rather than followed.
        """
        if os.path.basename(name) != name or name in ("", ".", ".."):
            return None
        root = os.path.realpath(self.pickup_dir)
        final = os.path.realpath(os.path.join(root, name))
        if os.path.dirname(final) != root:
            return None
        return final

    def forget(self) -> None:
        """Drop what the dropbox remembers of past scans and writes, for a reset.

        The directories themselves are left alone: what is on disk is the
        user's, and a file written before the reset may not be collected yet.
        """
        with self.lock:
            self.last_scan = []
            self.scans = 0
            self.written.clear()
            self.refused.clear()
            self.renamed.clear()

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
                "refused": list(self.refused[-20:]),
                "stuck": [{"name": name, "reason": reason}
                          for name, (_size, _mtime, reason)
                          in sorted(self.stuck.items())],
                "renamed": list(self.renamed[-20:]),
                "lastScan": [vars(item) for item in self.last_scan],
            }


def _free_name(path: str) -> str:
    """`path`, or the first `stem-N.ext` beside it that does not exist yet."""
    stem, extension = os.path.splitext(path)
    candidate, counter = path, 1
    while os.path.exists(candidate):
        candidate = "%s-%d%s" % (stem, counter, extension)
        counter += 1
    return candidate
