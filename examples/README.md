# Examples

Two things that need nothing but this mock:

- `demo.sh`, a guided tour in curl.
- `client.py`, the same tour in Python with no dependencies.

## The integration moved to mock-acme

`po_bridge.py` lived here, with its tests. It is code that sits *between* this
mock and mock-sap, so it now lives with the rest of the integration, in one
place, with one copy and tests that run against all three mocks:
[mock-acme](https://github.com/rseufert/mock-acme).

| Was here | Is now |
| --- | --- |
| `examples/po_bridge.py` | [`mockacme/po_bridge.py`](https://github.com/rseufert/mock-acme/blob/main/mockacme/po_bridge.py) |
| `examples/test_po_bridge.py` | [`tests/test_po_bridge.py`](https://github.com/rseufert/mock-acme/blob/main/tests/test_po_bridge.py) |

The last versions kept here are at
[`0e86279`](https://github.com/rseufert/mock-edi/tree/0e86279/examples), which
is release 0.7.0.
