"""AS2: the headers, the MIC, and the MDN.

AS2 (RFC 4130) is EDI over HTTP POST with a receipt.  The parts that matter
for testing an integration are the parts implemented here:

* the `AS2-From` / `AS2-To` identifiers, which must match what the two sides
  registered with each other - the single most common misconfiguration;
* the **MDN**, the signed-for-delivery receipt, returned either on the same
  HTTP response (synchronous) or posted back later to the URL in
  `Receipt-Delivery-Option` (asynchronous);
* the **MIC**, a hash of what was received, echoed in the MDN so the sender
  can prove the bytes arrived intact.

**What is deliberately not here: S/MIME.**  Signing and encrypting AS2
payloads needs certificates and a cryptography library, and this project has
no dependencies on purpose.  A message that arrives encrypted or signed is
refused with an MDN that says so, rather than being mangled - which is more
useful than a half-implementation that appears to work.  If your integration
must be tested against signed AS2, this mock is the wrong tool and will tell
you so on the first message.
"""
from __future__ import annotations

import base64
import datetime
import email.utils
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

AS2_VERSION = "1.2"

# Content types that carry EDI, and the dialect each implies when present.
EDI_CONTENT_TYPES = {
    "application/edi-x12": "X12",
    "application/x12": "X12",
    "application/edifact": "EDIFACT",
    "application/edi-consent": "",
    "application/octet-stream": "",
    "text/plain": "",
}
SECURE_CONTENT_TYPES = ("application/pkcs7-mime", "application/x-pkcs7-mime",
                        "multipart/signed", "application/pkcs7-signature")

MIC_ALGORITHMS = {"sha1": hashlib.sha1, "sha-1": hashlib.sha1,
                  "sha256": hashlib.sha256, "sha-256": hashlib.sha256,
                  "sha512": hashlib.sha512, "sha-512": hashlib.sha512,
                  "md5": hashlib.md5}
DEFAULT_MIC_ALGORITHM = "sha1"

PROCESSED = "processed"
ERROR = "processed/Error: unexpected-processing-error"
UNSUPPORTED = "processed/Error: insufficient-message-security"
FAILED = "failed/Failure: unsupported-format"


@dataclass
class Inbound:
    """An AS2 POST, as far as the headers describe it."""
    sender: str = ""
    receiver: str = ""
    message_id: str = ""
    subject: str = ""
    content_type: str = ""
    notify_to: str = ""
    notify_options: str = ""
    async_url: str = ""
    version: str = ""
    secured: bool = False

    @property
    def wants_mdn(self) -> bool:
        return bool(self.notify_to or self.async_url)

    @property
    def asynchronous(self) -> bool:
        return bool(self.async_url)

    @property
    def wants_signed_receipt(self) -> bool:
        """Whether the sender *required* a signed receipt, not merely offered.

        `signed-receipt-protocol=optional` says the sender will take an
        unsigned MDN; `required` says it will not. The mock does not do
        S/MIME, so the second is a request it has to refuse rather than
        answer with an unsigned success the sender has said it cannot use.
        """
        return bool(re.search(r"signed-receipt-protocol\s*=\s*required",
                              self.notify_options or "", re.I))

    @property
    def micalg(self) -> str:
        """The digest the sender asked for, defaulting the way AS2 does."""
        found = re.search(r"signed-receipt-micalg\s*=\s*(?:optional|required)\s*,\s*([\w-]+)",
                          self.notify_options or "", re.I)
        if found and found.group(1).lower() in MIC_ALGORITHMS:
            return found.group(1).lower()
        return DEFAULT_MIC_ALGORITHM

    def dialect_hint(self) -> str:
        base = (self.content_type or "").split(";")[0].strip().lower()
        return EDI_CONTENT_TYPES.get(base, "")


def is_as2(headers) -> bool:
    """Whether a request is an AS2 POST rather than a plain one.

    `AS2-To` is the marker: `AS2-From` alone could be a sloppy client, but a
    message addressed to an AS2 identifier is asking for AS2 handling.
    """
    return bool(_header(headers, "AS2-To") or _header(headers, "AS2-From"))


def read(headers) -> Inbound:
    content_type = _header(headers, "Content-Type")
    base = content_type.split(";")[0].strip().lower()
    return Inbound(
        sender=_header(headers, "AS2-From").strip().strip('"'),
        receiver=_header(headers, "AS2-To").strip().strip('"'),
        message_id=_header(headers, "Message-ID") or _header(headers, "Message-Id"),
        subject=_header(headers, "Subject"),
        content_type=content_type,
        notify_to=_header(headers, "Disposition-Notification-To"),
        notify_options=_header(headers, "Disposition-Notification-Options"),
        async_url=_header(headers, "Receipt-Delivery-Option"),
        version=_header(headers, "AS2-Version") or AS2_VERSION,
        secured=base in SECURE_CONTENT_TYPES,
    )


def _header(headers, name: str) -> str:
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    return (getter(name, "") if getter else "") or ""


def mic(payload: bytes, algorithm: str = DEFAULT_MIC_ALGORITHM) -> str:
    """The Message Integrity Check: base64 of the digest of what arrived.

    For an unsigned message the digest covers the payload alone.  A signed
    message would digest the MIME entity including its headers, which is one
    more reason the two cases cannot be quietly conflated.
    """
    digest = MIC_ALGORITHMS.get(algorithm.lower(), hashlib.sha1)
    return base64.b64encode(digest(payload).digest()).decode("ascii")


def received_content_mic(payload: bytes, algorithm: str) -> str:
    return "%s, %s" % (mic(payload, algorithm), algorithm)


def message_id(host: str) -> str:
    return "<%s@%s>" % (uuid.uuid4().hex, host or "mock-edi")


def build_mdn(inbound: Inbound, payload: bytes, receiver: str,
              disposition: str = PROCESSED, explanation: str = "",
              user_agent: str = "mock-edi",
              moment: Optional[datetime.datetime] = None) -> Tuple[Dict[str, str], bytes]:
    """A multipart/report MDN: one part for a human, one for the machine.

    The human-readable part is not decoration.  When an MDN says a message was
    refused, the machine-readable disposition gives a code from a short list,
    and the text part is the only place the actual reason can go - so it is
    the first thing anyone reads when an interchange is rejected.
    """
    when = moment or datetime.datetime.now()
    boundary = "----=_MDN_%s" % uuid.uuid4().hex[:16]
    mdn_id = message_id(receiver)
    algorithm = inbound.micalg

    text = explanation or _default_text(inbound, disposition)
    machine = [
        "Reporting-UA: %s" % user_agent,
        "Original-Recipient: rfc822; %s" % receiver,
        "Final-Recipient: rfc822; %s" % receiver,
        "Original-Message-ID: %s" % (inbound.message_id or "<unknown>"),
    ]
    if payload:
        machine.append("Received-Content-MIC: %s"
                       % received_content_mic(payload, algorithm))
    machine.append("Disposition: automatic-action/MDN-sent-automatically; %s"
                   % disposition)

    # The human-readable part is written as UTF-8, so it may only call itself
    # us-ascii and 7bit when it really is. A partner id with a diaeresis in it
    # is enough to make that a lie, and a strict client acts on the lie.
    ascii_text = all(ord(char) < 128 for char in text)
    charset = "us-ascii" if ascii_text else "utf-8"
    encoding = "7bit" if ascii_text else "8bit"

    body = (
        "This is a multi-part message in MIME format.\r\n"
        "\r\n--%(b)s\r\n"
        "Content-Type: text/plain; charset=%(charset)s\r\n"
        "Content-Transfer-Encoding: %(encoding)s\r\n"
        "\r\n%(text)s\r\n"
        "\r\n--%(b)s\r\n"
        "Content-Type: message/disposition-notification\r\n"
        "Content-Transfer-Encoding: 7bit\r\n"
        "\r\n%(machine)s\r\n"
        "\r\n--%(b)s--\r\n"
    ) % {"b": boundary, "text": text, "machine": "\r\n".join(machine),
         "charset": charset, "encoding": encoding}

    headers = {
        "Content-Type": 'multipart/report; report-type=disposition-notification; '
                        'boundary="%s"' % boundary,
        "AS2-Version": AS2_VERSION,
        "AS2-From": receiver,
        "AS2-To": inbound.sender or "unknown",
        "Message-ID": mdn_id,
        "Date": email.utils.format_datetime(when),
        "MIME-Version": "1.0",
    }
    return headers, body.encode("utf-8")


def _default_text(inbound: Inbound, disposition: str) -> str:
    if disposition.startswith(PROCESSED) and "Error" not in disposition:
        return ("The message %s sent by %s has been received. It has been "
                "processed. This is no guarantee that its contents have been "
                "acted upon." % (inbound.message_id or "(no Message-ID)",
                                 inbound.sender or "an unidentified partner"))
    return ("The message %s sent by %s was received but could not be processed."
            % (inbound.message_id or "(no Message-ID)",
               inbound.sender or "an unidentified partner"))


def outbound_headers(sender: str, receiver: str, subject: str, dialect: str,
                     message_id_value: str, request_mdn: bool = True,
                     notify_to: str = "",
                     moment: Optional[datetime.datetime] = None) -> Dict[str, str]:
    """Headers for a document the mock posts to a partner's AS2 URL."""
    when = moment or datetime.datetime.now()
    headers = {
        "Content-Type": "application/edi-x12" if dialect == "X12"
                        else "application/edifact",
        "AS2-Version": AS2_VERSION,
        "AS2-From": sender,
        "AS2-To": receiver,
        "Message-ID": message_id_value,
        "Subject": subject,
        "Date": email.utils.format_datetime(when),
        "MIME-Version": "1.0",
    }
    if request_mdn:
        headers["Disposition-Notification-To"] = notify_to or sender
        headers["Disposition-Notification-Options"] = (
            "signed-receipt-protocol=optional, pkcs7-signature; "
            "signed-receipt-micalg=optional, sha256")
    return headers


def parse_mdn(body: bytes) -> Dict[str, str]:
    """Pull the machine-readable fields out of an MDN that came back to us.

    Used when a partner acknowledges something the mock sent, so that
    `/_mock/mdns` can show whether the receipt actually arrived.
    """
    text = body.decode("utf-8", "replace")
    out: Dict[str, str] = {}
    for field_name in ("Disposition", "Original-Message-ID", "Received-Content-MIC",
                       "Final-Recipient", "Reporting-UA"):
        found = re.search(r"^%s:\s*(.+)$" % re.escape(field_name), text,
                          re.I | re.M)
        if found:
            out[field_name] = found.group(1).strip()
    return out
