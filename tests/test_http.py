"""The HTTP underneath: chunked bodies, lengths that lie, clients that stall.

AS2 is HTTP/1.1, and a client streaming a large interchange sends it chunked.
The mock used to read `Content-Length` bytes and nothing else, on a keep-alive
connection with no timeout: a chunked body was answered as empty and its size
line parsed as the next request, `Content-Length: -1` read until the client
hung up, and a client that sent headers and then nothing held a thread for
good. These talk to the socket directly, because urllib will not send most of
what they need to.
"""
import json
import os
import socket
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import MockServerCase, x12_order


class RawSocketCase(MockServerCase):
    config_kwargs = {"request_timeout": 1.0, "max_body_bytes": 64 * 1024}

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(sock.close)
        return sock

    def read_response(self, sock):
        """One response: (status, headers, body). Empty status if closed."""
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                return 0, {}, b""
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = head.decode("iso-8859-1").split("\r\n")
        status = int(lines[0].split()[1])
        headers = {k.strip().lower(): v.strip() for k, v in
                   (line.split(":", 1) for line in lines[1:])}
        length = int(headers.get("content-length", "0"))
        while len(rest) < length:
            chunk = sock.recv(65536)
            if not chunk:
                break
            rest += chunk
        return status, headers, rest[:length]


class ChunkedBodies(RawSocketCase):
    def test_a_chunked_850_is_answered_like_any_other(self):
        payload = x12_order("PO-CHUNKED").encode()
        pieces = [payload[i:i + 100] for i in range(0, len(payload), 100)]
        body = b"".join(b"%x\r\n%s\r\n" % (len(p), p) for p in pieces) + b"0\r\n\r\n"
        sock = self.connect()
        sock.sendall(b"POST /edi HTTP/1.1\r\nHost: mock\r\n"
                     b"Content-Type: application/edi-x12\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n" + body)
        status, _headers, data = self.read_response(sock)
        self.assertEqual(status, 200, data)
        self.assertEqual(json.loads(data)["orders"], ["PO-CHUNKED"])

        # And the connection is still in step: the next request is a request.
        sock.sendall(b"GET /_mock/health HTTP/1.1\r\nHost: mock\r\n\r\n")
        status, _headers, _data = self.read_response(sock)
        self.assertEqual(status, 200)

    def test_chunk_extensions_and_trailers_are_allowed(self):
        payload = x12_order("PO-TRAILER").encode()
        body = (b"%x;name=value\r\n%s\r\n0\r\nX-Checksum: none\r\n\r\n"
                % (len(payload), payload))
        sock = self.connect()
        sock.sendall(b"POST /edi HTTP/1.1\r\nHost: mock\r\n"
                     b"Transfer-Encoding: chunked\r\n\r\n" + body)
        status, _headers, data = self.read_response(sock)
        self.assertEqual(status, 200, data)

    def test_another_transfer_coding_is_refused_by_name(self):
        sock = self.connect()
        sock.sendall(b"POST /edi HTTP/1.1\r\nHost: mock\r\n"
                     b"Transfer-Encoding: gzip\r\n\r\n")
        status, headers, data = self.read_response(sock)
        self.assertEqual(status, 501)
        self.assertIn(b"gzip", data)
        self.assertEqual(headers.get("connection"), "close")


class LengthsThatLie(RawSocketCase):
    def test_a_negative_length_is_400_without_waiting_for_a_body(self):
        sock = self.connect()
        started = time.time()
        sock.sendall(b"POST /edi HTTP/1.1\r\nHost: mock\r\nContent-Length: -1\r\n\r\n")
        status, headers, _data = self.read_response(sock)
        self.assertEqual(status, 400)
        self.assertEqual(headers.get("connection"), "close")
        self.assertLess(time.time() - started, 0.9)   # not the timeout

    def test_a_body_over_the_cap_is_413_before_it_is_sent(self):
        sock = self.connect()
        sock.sendall(b"POST /edi HTTP/1.1\r\nHost: mock\r\n"
                     b"Content-Length: 10000000000\r\n\r\n")
        status, headers, data = self.read_response(sock)
        self.assertEqual(status, 413)
        self.assertIn(b"--max-body", data)
        self.assertEqual(headers.get("connection"), "close")


class ClientsThatStall(RawSocketCase):
    def test_a_body_that_never_arrives_is_408_after_the_timeout(self):
        sock = self.connect()
        started = time.time()
        sock.sendall(b"POST /edi HTTP/1.1\r\nHost: mock\r\nContent-Length: 100\r\n\r\n")
        status, headers, _data = self.read_response(sock)
        self.assertEqual(status, 408)
        self.assertEqual(headers.get("connection"), "close")
        self.assertLess(time.time() - started, 5)

    def test_a_client_that_sends_nothing_is_let_go(self):
        sock = self.connect()
        sock.sendall(b"POST /edi HTTP/1.1\r\n")      # headers never finished
        sock.settimeout(5)
        started = time.time()
        self.assertEqual(sock.recv(1), b"")
        self.assertLess(time.time() - started, 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
