"""The index page: who the mock trades with, how each misbehaves, and the endpoints."""
from __future__ import annotations

from typing import Tuple

from .. import partners
from . import route


@route("GET", "/", aliases=("/index.html",), refuse="GET the index page")
def index(h) -> Tuple[int, int]:
    return h.html(200, page(h.mock, h.base()))


_STYLE = """
:root { color-scheme: light dark; --fg:#1a1a1a; --bg:#fbfbfa; --muted:#6b6b6b;
        --line:#e3e3e0; --accent:#8b5a2b; --code:#f1f0ee; }
@media (prefers-color-scheme: dark) {
  :root { --fg:#e8e6e3; --bg:#1c1c1a; --muted:#9a9894; --line:#33322f;
          --accent:#d4a373; --code:#262523; } }
* { box-sizing: border-box; }
body { font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
       color: var(--fg); background: var(--bg); margin: 0; padding: 32px 16px; }
main { max-width: 860px; margin: 0 auto; }
h1 { font-size: 1.6rem; margin: 0 0 4px; }
h2 { font-size: 1.05rem; margin: 32px 0 8px; border-bottom: 1px solid var(--line);
     padding-bottom: 6px; }
p.lead { color: var(--muted); margin: 0 0 8px; }
table { border-collapse: collapse; width: 100%; font-size: 14px; }
th, td { text-align: left; padding: 6px 10px 6px 0; border-bottom: 1px solid var(--line);
         vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
code, a code { background: var(--code); padding: 1px 5px; border-radius: 4px;
       font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
.tag { font-size: 12px; color: var(--muted); }
"""


def page(mock, base: str) -> str:
    conn = mock.conn
    rows = partners.listing(conn)
    partner_rows = "".join(
        "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td>"
        "<td><code>%s</code></td><td>%s</td></tr>"
        % (row["id"], _esc(row["name"]), row["role"], row["dialect"],
           row["behaviour"],
           _esc(partners.BEHAVIOURS.get(row["behaviour"], "")))
        for row in rows)
    behaviour_rows = "".join(
        "<tr><td><code>%s</code></td><td>%s</td><td>%s</td></tr>"
        % (name, " or ".join(partners.BEHAVIOUR_ROLES[name]), _esc(text))
        for name, text in sorted(partners.BEHAVIOURS.items()))

    endpoints = [
        ("POST", "/as2", "An AS2 interchange. Answers with an MDN."),
        ("POST", "/edi", "The same, without AS2. Answers with a JSON summary."),
        ("POST", "/as2/mdn", "An asynchronous MDN coming back to us."),
        ("POST", "/_mock/validate", "Check a document, change nothing."),
        ("GET", "/_mock/health", "Liveness."),
        ("GET", "/_mock/state", "Counts, queue depth, configured delays."),
        ("GET", "/_mock/partners", "Who we trade with, and how each misbehaves."),
        ("GET", "/_mock/catalog", "What we sell."),
        ("GET", "/_mock/orders", "Purchase orders received and placed, and what became of them."),
        ("GET", "/_mock/disagreements", "Where a supplier disagrees with an order the mock placed, or a remittance with itself."),
        ("GET", "/_mock/remittances", "Every remittance advice received, and whether a later one reversed it."),
        ("POST", "/_mock/purchase", "Place an order with a supplier: an 850 or ORDERS goes out."),
        ("POST", "/_mock/purchase/{po}/change", "Change or cancel an order the mock placed."),
        ("GET", "/_mock/documents", "Every transaction set, in and out."),
        ("GET", "/_mock/interchanges", "Raw payloads. Add <code>?raw</code> for one."),
        ("GET", "/_mock/mailbox", "Collect what is waiting. <code>?leave</code> to peek."),
        ("GET", "/_mock/orders/{po}/timeline", "Everything that happened to one order, in order."),
        ("GET", "/_mock/outbox", "Documents produced, and what became of them."),
        ("GET", "/_mock/scheduled", "Work promised but not done: the unpacked despatch, the unwritten invoice."),
        ("GET", "/_mock/drop", "The drop and pickup directories, and what they have seen."),
        ("POST", "/_mock/drop/scan", "Read the drop directory now, without waiting for a poll."),
        ("POST", "/_mock/advance", "Release what is due. <code>?all</code> for everything, "
                                   "<code>?failed</code> to redeliver what failed."),
        ("POST", "/_mock/outbox/{id}/retry", "Deliver a failed document again, unchanged."),
        ("POST", "/_mock/outbox/{id}/resend", "Send a document again, unchanged, whatever became of it."),
        ("POST", "/_mock/send", "Send a document out of band."),
        ("GET", "/_mock/mdns", "Receipts, sent and received."),
        ("GET", "/_mock/unacknowledged", "What we sent that nobody has acknowledged."),
        ("GET", "/_mock/dictionary", "The segment dictionary the mock validates against."),
        ("POST", "/_mock/reset", "Back to a freshly seeded system."),
    ]
    endpoint_rows = "".join(
        '<tr><td class="tag">%s</td><td><a href="%s">%s</a></td><td>%s</td></tr>'
        % (method, path if method == "GET" else "#", path, note)
        for method, path, note in endpoints)

    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mock-edi</title><style>%s</style></head><body><main>
<h1>mock-edi</h1>
<p class="lead">A mock EDI trading partner answering as <code>%s</code>.
Send it an 850 or an ORDERS and it answers with an acknowledgment, a purchase
order response, a despatch advice and an invoice.</p>
<h2>Trading partners</h2>
<table><tr><th>Id</th><th>Name</th><th>Role</th><th>Dialect</th><th>Behaviour</th><th></th></tr>%s</table>
<h2>Behaviours</h2>
<table><tr><th>Behaviour</th><th>Partner</th><th></th></tr>%s</table>
<h2>Endpoints</h2>
<table><tr><th></th><th>Path</th><th></th></tr>%s</table>
<h2>Try it</h2>
<p class="lead">There is a worked example in <code>examples/demo.sh</code>.
The short version:</p>
<p><code>curl -X POST --data-binary @order.edi %s/edi</code></p>
</main></body></html>""" % (_STYLE, mock.config.as2_id, partner_rows,
                            behaviour_rows, endpoint_rows, base)


def _esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
