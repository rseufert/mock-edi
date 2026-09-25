"""Command line entry point: ``python -m mockedi`` / ``mock-edi``."""
from __future__ import annotations

import argparse
import sqlite3
import sys

from . import __version__, db
from .partners import BEHAVIOURS
from .server import Config, make_server


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mock-edi",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Run a mock EDI trading partner (X12 and EDIFACT over AS2).",
        epilog="partner behaviours:\n" + "\n".join(
            "  %-18s %s" % (name, text) for name, text in sorted(BEHAVIOURS.items())))
    p.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="port (default: 8080)")
    p.add_argument("--db", dest="db_path", default=":memory:",
                   help="SQLite file, or :memory: (default) for a throwaway partner")

    identity = p.add_argument_group("identity - how the mock names itself")
    identity.add_argument("--as2-id", dest="as2_id", default="MOCKEDI",
                          help="our interchange and AS2 identifier (default: MOCKEDI)")
    identity.add_argument("--name", default="Mock EDI Supply Co",
                          help="our company name, as it appears in N1/NAD")
    identity.add_argument("--qualifier", default="ZZ",
                          help="our interchange id qualifier (default: ZZ)")

    behaviour = p.add_argument_group("behaviour")
    behaviour.add_argument("--ack-delay", dest="ack_delay_ms", type=int, default=0,
                           metavar="MS", help="delay before the 997/CONTRL is released")
    behaviour.add_argument("--response-delay", dest="response_delay_ms", type=int,
                           default=0, metavar="MS", help="delay before the 855/ORDRSP")
    behaviour.add_argument("--despatch-delay", dest="despatch_delay_ms", type=int,
                           default=0, metavar="MS", help="delay before the 856/DESADV")
    behaviour.add_argument("--invoice-delay", dest="invoice_delay_ms", type=int,
                           default=0, metavar="MS", help="delay before the 810/INVOIC")
    behaviour.add_argument("--tax-rate", default="0",
                           help="tax applied to invoices, e.g. 0.0825 (default: 0)")
    behaviour.add_argument("--allow-duplicates", action="store_true",
                           help="accept an interchange control number a partner "
                                "has already used; by default a replay is "
                                "refused in the envelope's own words")
    behaviour.add_argument("--no-mdn", dest="mdn", action="store_false",
                           help="never return an MDN, whatever the sender asks for")
    behaviour.add_argument("--any-receiver", dest="strict_receiver",
                           action="store_false",
                           help="accept interchanges addressed to someone else")
    behaviour.add_argument("--compact", dest="pretty", action="store_false",
                           help="write documents without a newline per segment")

    directory = p.add_argument_group(
        "directory trading - the half of real EDI that is not AS2")
    directory.add_argument("--drop-dir", metavar="PATH",
                           help="a directory to watch for inbound interchanges; "
                                "read files are moved to processed/ or failed/")
    directory.add_argument("--pickup-dir", metavar="PATH",
                           help="write released outbound documents here as well "
                                "as queueing them for the mailbox")
    directory.add_argument("--drop-interval-ms", type=int, default=1000,
                           help="how often to look in the drop directory "
                                "(default: 1000; POST /_mock/drop/scan looks now)")
    directory.add_argument("--drop-settle-ms", type=int, default=250,
                           help="leave a file alone until it has been untouched "
                                "this long, in case it is still being written "
                                "(default: 250)")

    testing = p.add_argument_group("testing")
    testing.add_argument("--auth", dest="basic_auth", metavar="USER:PASSWORD",
                         help="require HTTP basic authentication")
    testing.add_argument("--seed", dest="seed_value", type=int, default=42,
                         help="seed for the generated demo data (default: 42)")
    testing.add_argument("--latency-ms", type=int, default=0,
                         help="artificial delay added to every request")
    testing.add_argument("--error-rate", type=float, default=0.0,
                         help="fraction of non-control requests answered with a 500")
    testing.add_argument("--no-request-log", dest="log_requests",
                         action="store_false",
                         help="do not record requests in the request_log table")
    limits = p.add_argument_group("limits")
    limits.add_argument("--max-body", dest="max_body_bytes", type=int,
                        default=16 * 1024 * 1024, metavar="BYTES",
                        help="largest request body read; larger is answered "
                             "413 (default: 16 MiB)")
    limits.add_argument("--request-timeout", type=float, default=60.0,
                        metavar="SECONDS",
                        help="how long a request may take to arrive before the "
                             "connection is closed (default: 60)")
    retention = p.add_argument_group(
        "retention - for a mock left running on a --db file")
    retention.add_argument("--keep-requests", type=int, default=5000, metavar="N",
                           help="keep the newest N rows of the request log "
                                "(default: 5000; 0 keeps them all)")
    retention.add_argument("--retention-days", type=float, default=0.0,
                           metavar="DAYS",
                           help="remove the request log, interchanges, and "
                                "finished documents and MDNs older than this "
                                "(default: 0, keep everything)")
    p.add_argument("-q", "--quiet", action="store_true", help="suppress the access log")
    p.add_argument("--version", action="version", version="mock-edi " + __version__)
    return p


def config_from_args(args: argparse.Namespace) -> Config:
    """The Config a parsed command line describes."""
    values = dict(vars(args))
    # argparse leaves an unset path as None; Config wants a string.
    values["drop_dir"] = values.get("drop_dir") or ""
    values["pickup_dir"] = values.get("pickup_dir") or ""
    return Config(**values)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    # Line-buffer the output: piped or run in a container, a block-buffered
    # stdout swallows the banner and the access log until the buffer fills.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # pragma: no cover - odd stdout
        pass

    config = config_from_args(args)
    try:
        httpd = make_server(config)
    except OSError as error:
        print("mock-edi: cannot listen on %s:%d - %s"
              % (args.host, args.port, error), file=sys.stderr)
        return 2
    except db.DatabaseError as error:
        print("mock-edi: %s" % error, file=sys.stderr)
        return 2
    except sqlite3.DatabaseError as error:
        print("mock-edi: cannot use --db %s - %s" % (args.db_path, error),
              file=sys.stderr)
        return 2
    base = "http://%s:%d" % (args.host, args.port)
    print("mock-edi %s listening on %s  (as %s, db %s)"
          % (__version__, base, args.as2_id, args.db_path))
    print("  AS2      POST %s/as2        (answers with an MDN)" % base)
    print("  Plain    POST %s/edi        (answers with a JSON summary)" % base)
    print("  Validate POST %s/_mock/validate" % base)
    print("  Mailbox  GET  %s/_mock/mailbox" % base)
    print("  Control  GET  %s/_mock/state, /_mock/partners, /_mock/orders" % base)
    print("  Index    %s/" % base)
    if config.drop_dir:
        print("  Drop     %s  (every %dms)" % (config.drop_dir, config.drop_interval_ms))
    if config.pickup_dir:
        print("  Pickup   %s" % config.pickup_dir)
    for row in httpd.mock.conn.execute(
            "SELECT id, dialect, behaviour FROM partner ORDER BY id"):
        print("  partner  %-10s %-8s %s" % (row["id"], row["dialect"], row["behaviour"]))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
