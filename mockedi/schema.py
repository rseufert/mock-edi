"""The EDI dictionary: the declarative heart of the mock.

Everything the mock does with a document is derived from what is defined
here - parsing names the elements, validation checks them, generation builds
segments in the order this file gives, and the documentation index is read
straight off these objects.  Nothing else in the package hard-codes a segment
tag or an element position.

The model is deliberately one model for both dialects.  X12 and EDIFACT
disagree about almost everything at the surface - delimiters, envelope tags,
whether an element may be composite - but they agree on the shape underneath:
a document is an ordered tree of segments and repeating loops, a segment is an
ordered list of elements, and an element is a typed, length-bounded field that
may carry a code list.  `Element`, `Segment`, `Use`, `Loop` and `TransactionSet`
describe that shape; `x12.py` and `edifact.py` differ only in how they write it
down.

What is *not* here: anything business.  The dictionary says a PO1 segment holds
a quantity in position 2; it does not say what a purchase order means.  That
lives in `documents.py`, and the mapping between the two in `transactions.py`.

Coverage is the commonly traded core of each set, not the full standard.  A
real 850 admits some fifty segment types and almost nobody sends more than a
dozen; the mock implements the dozen, validates them properly, and reports an
unrecognised segment rather than pretending to understand it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence, Tuple, Union

# Requirement designators, shared by both dialects.
MANDATORY = "M"
OPTIONAL = "O"
CONDITIONAL = "C"

# Element data types.  X12 spells them AN/ID/DT/TM/N0/R; EDIFACT spells them
# an/a/n; they mean close enough to the same thing that one set serves both.
#
#   AN  string            ID  string from a code list
#   DT  date              TM  time
#   N0  integer           Nn  integer with n implied decimals
#   R   decimal, explicit decimal point
TYPES = ("AN", "ID", "DT", "TM", "N0", "N1", "N2", "R")


@dataclass(frozen=True)
class Element:
    """One field of a segment, at one position.

    `ref` is the standard's own number for the element - X12 data element 353,
    EDIFACT 1225 - and exists so that a validation error can name the element
    the way the trading partner's spec names it.  `req` belongs to the
    *position*, not to the element: data element 373 is mandatory in BEG05 and
    optional in DTM02, and it is the same element both times.
    """
    ref: str
    name: str
    type: str = "AN"
    min_len: int = 1
    max_len: int = 80
    req: str = OPTIONAL
    codes: Optional[Dict[str, str]] = None
    components: Tuple["Element", ...] = ()

    @property
    def composite(self) -> bool:
        return bool(self.components)

    def code_meaning(self, value: str) -> str:
        """What a code means, for the human-readable side of an error."""
        return (self.codes or {}).get(value, "")


@dataclass(frozen=True)
class Segment:
    """A segment definition: a tag and an ordered list of element positions.

    Positions are 1-based to match how the standards refer to them.  BEG03 is
    `elements[2]`, and everything user-facing says BEG03.
    """
    tag: str
    name: str
    elements: Tuple[Element, ...]
    purpose: str = ""

    def element(self, position: int) -> Optional[Element]:
        if 1 <= position <= len(self.elements):
            return self.elements[position - 1]
        return None

    def label(self, position: int) -> str:
        """`BEG03`, the way a trading partner's implementation guide writes it."""
        return "%s%02d" % (self.tag, position)


@dataclass(frozen=True)
class Use:
    """A segment used at one place in a transaction set."""
    segment: Segment
    req: str = OPTIONAL
    max_use: int = 1

    @property
    def tag(self) -> str:
        return self.segment.tag


@dataclass(frozen=True)
class Loop:
    """A repeating group.  Its id is its first segment's tag, as in the standard."""
    id: str
    children: Tuple[Union[Use, "Loop"], ...]
    req: str = OPTIONAL
    repeat: int = 1

    @property
    def trigger(self) -> str:
        """The tag that starts a repetition - how a parser recognises the loop."""
        head = self.children[0]
        return head.tag if isinstance(head, Use) else head.trigger


@dataclass(frozen=True)
class TransactionSet:
    """A transaction set (X12) or message (EDIFACT).

    `code` is what goes in ST01 or in UNH02.1 - "850", "ORDERS".  `group` is
    the X12 functional identifier code that GS01 carries for this set, and is
    empty for EDIFACT, which has no equivalent in common use.
    """
    code: str
    name: str
    dialect: str
    children: Tuple[Union[Use, Loop], ...]
    group: str = ""
    version: str = ""
    purpose: str = ""

    def walk(self) -> Iterator[Tuple[Union[Use, Loop], Optional[Loop]]]:
        """Every use in the set, depth first, each with the loop containing it."""
        def visit(children, parent):
            for child in children:
                if isinstance(child, Loop):
                    yield child, parent
                    for item in visit(child.children, child):
                        yield item
                else:
                    yield child, parent
        return visit(self.children, None)

    def uses(self) -> Iterator[Tuple[Use, Optional[Loop]]]:
        for child, parent in self.walk():
            if isinstance(child, Use):
                yield child, parent

    def segment_for(self, tag: str) -> Optional[Segment]:
        for use, _ in self.uses():
            if use.tag == tag:
                return use.segment
        return None

    def loop_for(self, tag: str) -> Optional[Loop]:
        """The loop a tag starts, if it starts one."""
        for child, _ in self.walk():
            if isinstance(child, Loop) and child.trigger == tag:
                return child
        return None

    def known_tags(self) -> Tuple[str, ...]:
        return tuple(sorted({use.tag for use, _ in self.uses()}))


# ---------------------------------------------------------------------------
# X12 code lists
#
# Only the codes the mock recognises.  A code outside these lists is a 7
# ("invalid code value") in the 997, which is the point: a code list that
# accepted everything would acknowledge everything.
# ---------------------------------------------------------------------------

PURPOSE_CODES = {          # 353
    "00": "Original", "01": "Cancellation", "04": "Change", "05": "Replace",
    "06": "Confirmation", "07": "Duplicate", "22": "Information Copy",
}
PO_TYPE_CODES = {          # 92
    "SA": "Stand-alone Order", "NE": "New Order", "RL": "Release or Delivery Order",
    "DS": "Dropship", "BK": "Blanket Order", "KN": "Purchase Order",
}
ACK_TYPE_CODES = {         # 587
    "AC": "Acknowledge - With Detail and Change",
    "AD": "Acknowledge - With Detail, No Change",
    "AE": "Acknowledge - With Exception Detail Only",
    "AH": "Acknowledge - Hold Status",
    "AK": "Acknowledge - No Detail or Change",
    "RD": "Reject - With Detail",
    "RF": "Reject - With Exception Detail Only",
    "RJ": "Rejected - No Detail",
}
LINE_STATUS_CODES = {      # 668
    "IA": "Item Accepted", "IB": "Item Backordered",
    "IC": "Item Accepted - Changes Made", "ID": "Item Deleted",
    "IP": "Item Accepted - Price Changed", "IQ": "Item Accepted - Quantity Changed",
    "IR": "Item Rejected", "IS": "Item Accepted - Substitution Made",
    "DR": "Item Accepted - Date Rescheduled",
}
UOM_CODES = {              # 355
    "EA": "Each", "CA": "Case", "CS": "Case", "BX": "Box", "CT": "Carton",
    "PC": "Piece", "DZ": "Dozen", "LB": "Pound", "KG": "Kilogram",
    "GA": "Gallon", "FT": "Foot", "M": "Metre", "PL": "Pallet",
}
DATE_QUALIFIER_CODES = {   # 374
    "002": "Delivery Requested", "010": "Requested Ship", "011": "Shipped",
    "017": "Estimated Delivery", "035": "Delivered", "037": "Ship Not Before",
    "038": "Ship No Later Than", "068": "Current Schedule Delivery",
    "118": "Requested Pick Up", "137": "Document/Message Date",
}
ENTITY_CODES = {           # 98
    "BY": "Buying Party", "SE": "Selling Party", "ST": "Ship To",
    "SF": "Ship From", "BT": "Bill To", "RE": "Party to Receive Remittance",
    "VN": "Vendor", "SU": "Supplier", "MA": "Party for whom Item is Ultimately Intended",
}
ID_QUALIFIER_CODES = {     # 66
    "1": "D-U-N-S Number", "2": "Standard Carrier Alpha Code (SCAC)",
    "9": "D-U-N-S+4", "91": "Assigned by Seller",
    "92": "Assigned by Buyer", "ZZ": "Mutually Defined", "UL": "GLN",
}
PRODUCT_QUALIFIER_CODES = {  # 235
    "VP": "Vendor's Part Number", "BP": "Buyer's Part Number",
    "UP": "UPC Consumer Package Code", "EN": "EAN", "IN": "Buyer's Item Number",
    "SK": "Stock Keeping Unit", "MG": "Manufacturer's Part Number", "UI": "UPC/EAN",
}
REFERENCE_QUALIFIER_CODES = {  # 128
    "BM": "Bill of Lading Number", "CO": "Customer Order Number",
    "DP": "Department Number", "IA": "Internal Vendor Number",
    "PO": "Purchase Order Number", "VN": "Vendor Order Number",
    "CN": "Carrier's Reference Number", "SI": "Shipper's Identifying Number",
    "IV": "Seller's Invoice Number", "ZZ": "Mutually Defined",
}
HIERARCHY_CODES = {        # 1005 - which levels appear, in which order
    "0001": "Shipment, Order, Packaging, Item",
    "0002": "Shipment, Order, Item, Packaging",
    "0003": "Shipment, Order, Packaging",
    "0004": "Shipment, Order, Item",
    "0005": "Shipment, Order",
}
LEVEL_CODES = {"S": "Shipment", "O": "Order", "I": "Item", "P": "Pack", "T": "Tare"}
CHILD_CODES = {"0": "No subordinate segments", "1": "Additional subordinate segments"}

TS_ACK_CODES = {           # 717
    "A": "Accepted", "E": "Accepted, but errors were noted",
    "R": "Rejected", "M": "Rejected - message authentication failed",
    "W": "Rejected - assurance failed validity tests",
}
GROUP_ACK_CODES = {        # 715
    "A": "Accepted", "E": "Accepted, but errors were noted",
    "P": "Partially accepted - at least one transaction set was rejected",
    "R": "Rejected", "M": "Rejected - message authentication failed",
}
SEGMENT_ERROR_CODES = {    # 720
    "1": "Unrecognized segment ID", "2": "Unexpected segment",
    "3": "Mandatory segment missing", "4": "Loop occurs over maximum times",
    "5": "Segment exceeds maximum use",
    "6": "Segment not in defined transaction set",
    "7": "Segment not in proper sequence", "8": "Segment has data element errors",
}
ELEMENT_ERROR_CODES = {    # 723
    "1": "Mandatory data element missing",
    "2": "Conditional required data element missing",
    "3": "Too many data elements", "4": "Data element too short",
    "5": "Data element too long", "6": "Invalid character in data element",
    "7": "Invalid code value", "8": "Invalid date", "9": "Invalid time",
    "10": "Exclusion condition violated",
}
TS_ERROR_CODES = {         # 718
    "1": "Transaction set not supported",
    "2": "Transaction set trailer missing",
    "3": "Transaction set control number in header and trailer do not match",
    "4": "Number of included segments does not match actual count",
    "5": "One or more segments in error",
    "6": "Missing or invalid transaction set identifier",
    "7": "Missing or invalid transaction set control number",
}
GROUP_ERROR_CODES = {      # 716
    "1": "Functional group not supported",
    "2": "Functional group version not supported",
    "3": "Functional group trailer missing",
    "4": "Group control number in header and trailer do not agree",
    "5": "Number of included transaction sets does not match actual count",
    "6": "Group control number violates syntax",
}
FUNCTIONAL_GROUP_CODES = {  # 479
    "PO": "Purchase Order (850)", "PR": "Purchase Order Acknowledgment (855)",
    "SH": "Ship Notice/Manifest (856)", "IN": "Invoice (810)",
    "FA": "Functional Acknowledgment (997)",
}
CURRENCY_CODES = {"USD": "US Dollar", "EUR": "Euro", "GBP": "Pound Sterling",
                  "CAD": "Canadian Dollar", "JPY": "Yen", "CHF": "Swiss Franc"}


def _e(ref, name, type="AN", min_len=1, max_len=80, req=OPTIONAL, codes=None,
       components=()):
    return Element(ref=ref, name=name, type=type, min_len=min_len,
                   max_len=max_len, req=req, codes=codes, components=components)


def _product_ids(count: int, req_first: str = OPTIONAL) -> Tuple[Element, ...]:
    """The repeating qualifier/identifier pairs that end PO1, IT1 and LIN."""
    out: List[Element] = []
    for index in range(count):
        req = req_first if index == 0 else OPTIONAL
        out.append(_e("235", "Product/Service ID Qualifier", "ID", 2, 2, req,
                      PRODUCT_QUALIFIER_CODES))
        out.append(_e("234", "Product/Service ID", "AN", 1, 48, req))
    return tuple(out)


# ---------------------------------------------------------------------------
# X12 segments
#
# Defined once and shared: N1, DTM and REF appear in every set here, and a
# validation message should say the same thing about DTM02 wherever it is met.
# ---------------------------------------------------------------------------

ST = Segment("ST", "Transaction Set Header", (
    _e("143", "Transaction Set Identifier Code", "ID", 3, 3, MANDATORY),
    _e("329", "Transaction Set Control Number", "AN", 4, 9, MANDATORY),
    _e("1705", "Implementation Convention Reference", "AN", 1, 35),
), "Starts a transaction set and assigns it a control number.")

SE = Segment("SE", "Transaction Set Trailer", (
    _e("96", "Number of Included Segments", "N0", 1, 10, MANDATORY),
    _e("329", "Transaction Set Control Number", "AN", 4, 9, MANDATORY),
), "Ends a transaction set; the count includes ST and SE themselves.")

BEG = Segment("BEG", "Beginning Segment for Purchase Order", (
    _e("353", "Transaction Set Purpose Code", "ID", 2, 2, MANDATORY, PURPOSE_CODES),
    _e("92", "Purchase Order Type Code", "ID", 2, 2, MANDATORY, PO_TYPE_CODES),
    _e("324", "Purchase Order Number", "AN", 1, 22, MANDATORY),
    _e("328", "Release Number", "AN", 1, 30),
    _e("373", "Date", "DT", 8, 8, MANDATORY),
    _e("367", "Contract Number", "AN", 1, 30),
    _e("587", "Acknowledgment Type", "ID", 2, 2, OPTIONAL, ACK_TYPE_CODES),
), "Identifies the purchase order and why it was sent.")

BAK = Segment("BAK", "Beginning Segment for Purchase Order Acknowledgment", (
    _e("353", "Transaction Set Purpose Code", "ID", 2, 2, MANDATORY, PURPOSE_CODES),
    _e("587", "Acknowledgment Type", "ID", 2, 2, MANDATORY, ACK_TYPE_CODES),
    _e("324", "Purchase Order Number", "AN", 1, 22, MANDATORY),
    _e("373", "Date", "DT", 8, 8, MANDATORY),
    _e("328", "Release Number", "AN", 1, 30),
    _e("367", "Contract Number", "AN", 1, 30),
    _e("373", "Acknowledgment Date", "DT", 8, 8),
    _e("326", "Request Reference Number", "AN", 1, 45),
), "Identifies the order being acknowledged and the overall verdict on it.")

BSN = Segment("BSN", "Beginning Segment for Ship Notice", (
    _e("353", "Transaction Set Purpose Code", "ID", 2, 2, MANDATORY, PURPOSE_CODES),
    _e("396", "Shipment Identification", "AN", 2, 30, MANDATORY),
    _e("373", "Date", "DT", 8, 8, MANDATORY),
    _e("337", "Time", "TM", 4, 8, MANDATORY),
    _e("1005", "Hierarchical Structure Code", "ID", 4, 4, OPTIONAL, HIERARCHY_CODES),
), "Identifies the shipment and declares which HL levels the notice uses.")

BIG = Segment("BIG", "Beginning Segment for Invoice", (
    _e("373", "Invoice Date", "DT", 8, 8, MANDATORY),
    _e("76", "Invoice Number", "AN", 1, 22, MANDATORY),
    _e("373", "Purchase Order Date", "DT", 8, 8),
    _e("324", "Purchase Order Number", "AN", 1, 22),
    _e("328", "Release Number", "AN", 1, 30),
    _e("327", "Change Order Sequence Number", "AN", 1, 8),
    _e("640", "Transaction Type Code", "ID", 2, 2, OPTIONAL,
       {"DI": "Debit Invoice", "CR": "Credit Memo", "CI": "Consolidated Invoice",
        "FD": "Freight Invoice"}),
    _e("353", "Transaction Set Purpose Code", "ID", 2, 2, OPTIONAL, PURPOSE_CODES),
), "Identifies the invoice and the order it bills.")

CUR = Segment("CUR", "Currency", (
    _e("98", "Entity Identifier Code", "ID", 2, 3, MANDATORY, ENTITY_CODES),
    _e("100", "Currency Code", "ID", 3, 3, MANDATORY, CURRENCY_CODES),
    _e("280", "Exchange Rate", "R", 4, 10),
), "The currency every monetary amount in the document is expressed in.")

REF = Segment("REF", "Reference Identification", (
    _e("128", "Reference Identification Qualifier", "ID", 2, 3, MANDATORY,
       REFERENCE_QUALIFIER_CODES),
    _e("127", "Reference Identification", "AN", 1, 50),
    _e("352", "Description", "AN", 1, 80),
), "A secondary identifier, named by its qualifier.")

PER = Segment("PER", "Administrative Communications Contact", (
    _e("366", "Contact Function Code", "ID", 2, 2, MANDATORY,
       {"BD": "Buyer Name or Department", "IC": "Information Contact",
        "OC": "Order Contact", "AP": "Accounts Payable", "CN": "General Contact"}),
    _e("93", "Name", "AN", 1, 60),
    _e("365", "Communication Number Qualifier", "ID", 2, 2, OPTIONAL,
       {"TE": "Telephone", "EM": "Electronic Mail", "FX": "Facsimile",
        "UR": "Uniform Resource Locator"}),
    _e("364", "Communication Number", "AN", 1, 80),
), "Who to call about this document.")

FOB = Segment("FOB", "F.O.B. Related Instructions", (
    _e("146", "Shipment Method of Payment", "ID", 2, 2, MANDATORY,
       {"PP": "Prepaid", "CC": "Collect", "TP": "Third Party",
        "PC": "Prepaid and Charged", "DF": "Defined by Buyer and Seller"}),
    _e("309", "Location Qualifier", "ID", 1, 2),
    _e("352", "Description", "AN", 1, 80),
), "Who pays the freight, and where title passes.")

DTM = Segment("DTM", "Date/Time Reference", (
    _e("374", "Date/Time Qualifier", "ID", 3, 3, MANDATORY, DATE_QUALIFIER_CODES),
    _e("373", "Date", "DT", 8, 8),
    _e("337", "Time", "TM", 4, 8),
    _e("623", "Time Code", "ID", 2, 2),
), "A date, named by what kind of date it is.")

N1 = Segment("N1", "Party Identification", (
    _e("98", "Entity Identifier Code", "ID", 2, 3, MANDATORY, ENTITY_CODES),
    _e("93", "Name", "AN", 1, 60),
    _e("66", "Identification Code Qualifier", "ID", 1, 2, OPTIONAL, ID_QUALIFIER_CODES),
    _e("67", "Identification Code", "AN", 2, 80),
), "A party to the transaction, by role.")

N2 = Segment("N2", "Additional Name Information", (
    _e("93", "Name", "AN", 1, 60, MANDATORY),
    _e("93", "Name", "AN", 1, 60),
))

N3 = Segment("N3", "Party Location", (
    _e("166", "Address Information", "AN", 1, 55, MANDATORY),
    _e("166", "Address Information", "AN", 1, 55),
))

N4 = Segment("N4", "Geographic Location", (
    _e("19", "City Name", "AN", 2, 30),
    _e("156", "State or Province Code", "ID", 2, 2),
    _e("116", "Postal Code", "ID", 3, 15),
    _e("26", "Country Code", "ID", 2, 3),
))

PO1 = Segment("PO1", "Baseline Item Data", (
    _e("350", "Assigned Identification", "AN", 1, 20),
    _e("330", "Quantity Ordered", "R", 1, 15, MANDATORY),
    _e("355", "Unit or Basis for Measurement Code", "ID", 2, 2, MANDATORY, UOM_CODES),
    _e("212", "Unit Price", "R", 1, 17, MANDATORY),
    _e("639", "Basis of Unit Price Code", "ID", 2, 2),
) + _product_ids(5, MANDATORY), "One ordered line: how many, at what price, of what.")

PID = Segment("PID", "Product/Item Description", (
    _e("349", "Item Description Type", "ID", 1, 1, MANDATORY,
       {"F": "Free-form", "S": "Structured", "X": "Semi-structured"}),
    _e("750", "Product/Process Characteristic Code", "ID", 2, 3),
    _e("559", "Agency Qualifier Code", "ID", 2, 2),
    _e("751", "Product Description Code", "AN", 1, 12),
    _e("352", "Description", "AN", 1, 80),
))

PO4 = Segment("PO4", "Item Physical Details", (
    _e("356", "Pack", "N0", 1, 6),
    _e("357", "Size", "R", 1, 8),
    _e("355", "Unit or Basis for Measurement Code", "ID", 2, 2, OPTIONAL, UOM_CODES),
))

ACK = Segment("ACK", "Line Item Acknowledgment", (
    _e("668", "Line Item Status Code", "ID", 2, 2, MANDATORY, LINE_STATUS_CODES),
    _e("380", "Quantity", "R", 1, 15),
    _e("355", "Unit or Basis for Measurement Code", "ID", 2, 2, OPTIONAL, UOM_CODES),
    _e("374", "Date/Time Qualifier", "ID", 3, 3, OPTIONAL, DATE_QUALIFIER_CODES),
    _e("373", "Date", "DT", 8, 8),
    _e("326", "Request Reference Number", "AN", 1, 45),
) + _product_ids(5), "What the seller will actually do with the line above it.")

CTT = Segment("CTT", "Transaction Totals", (
    _e("354", "Number of Line Items", "N0", 1, 6, MANDATORY),
    _e("347", "Hash Total", "R", 1, 10),
), "A checksum: how many lines, and their quantities summed.")

HL = Segment("HL", "Hierarchical Level", (
    _e("628", "Hierarchical ID Number", "AN", 1, 12, MANDATORY),
    _e("734", "Hierarchical Parent ID Number", "AN", 1, 12),
    _e("735", "Hierarchical Level Code", "ID", 1, 2, MANDATORY, LEVEL_CODES),
    _e("736", "Hierarchical Child Code", "ID", 1, 1, OPTIONAL, CHILD_CODES),
), "One node of the shipment tree: its id, its parent, and what it is.")

TD1 = Segment("TD1", "Carrier Details - Quantity and Weight", (
    _e("103", "Packaging Code", "AN", 3, 5),
    _e("80", "Lading Quantity", "N0", 1, 7),
    _e("23", "Commodity Code Qualifier", "ID", 1, 1),
    _e("22", "Commodity Code", "AN", 1, 30),
    _e("79", "Lading Description", "AN", 1, 50),
    _e("187", "Weight Qualifier", "ID", 1, 2),
    _e("81", "Weight", "R", 1, 10),
    _e("355", "Unit or Basis for Measurement Code", "ID", 2, 2, OPTIONAL, UOM_CODES),
))

TD5 = Segment("TD5", "Carrier Details - Routing", (
    _e("133", "Routing Sequence Code", "ID", 1, 2),
    _e("66", "Identification Code Qualifier", "ID", 1, 2, OPTIONAL, ID_QUALIFIER_CODES),
    _e("67", "Identification Code", "AN", 2, 80),
    _e("91", "Transportation Method/Type Code", "ID", 1, 2, OPTIONAL,
       {"A": "Air", "M": "Motor (Common Carrier)", "U": "Private Parcel Service",
        "R": "Rail", "S": "Ocean", "LT": "Less Than Trailer Load"}),
    _e("387", "Routing", "AN", 1, 35),
))

TD3 = Segment("TD3", "Carrier Details - Equipment", (
    _e("40", "Equipment Description Code", "ID", 2, 2),
    _e("206", "Equipment Initial", "AN", 1, 4),
    _e("207", "Equipment Number", "AN", 1, 10),
))

PRF = Segment("PRF", "Purchase Order Reference", (
    _e("324", "Purchase Order Number", "AN", 1, 22, MANDATORY),
    _e("328", "Release Number", "AN", 1, 30),
    _e("327", "Change Order Sequence Number", "AN", 1, 8),
    _e("373", "Date", "DT", 8, 8),
), "Which purchase order this branch of the shipment tree belongs to.")

LIN = Segment("LIN", "Item Identification", (
    _e("350", "Assigned Identification", "AN", 1, 20),
) + _product_ids(5, MANDATORY), "What the item is, by one or more identifiers.")

SN1 = Segment("SN1", "Item Detail - Shipment", (
    _e("350", "Assigned Identification", "AN", 1, 20),
    _e("382", "Number of Units Shipped", "R", 1, 10, MANDATORY),
    _e("355", "Unit or Basis for Measurement Code", "ID", 2, 2, MANDATORY, UOM_CODES),
    _e("646", "Quantity Shipped to Date", "R", 1, 9),
    _e("330", "Quantity Ordered", "R", 1, 15),
    _e("355", "Unit or Basis for Measurement Code", "ID", 2, 2, OPTIONAL, UOM_CODES),
    _e("668", "Line Item Status Code", "ID", 2, 2, OPTIONAL, LINE_STATUS_CODES),
), "How many of the item above it are in this shipment.")

IT1 = Segment("IT1", "Baseline Item Data - Invoice", (
    _e("350", "Assigned Identification", "AN", 1, 20),
    _e("358", "Quantity Invoiced", "R", 1, 10, MANDATORY),
    _e("355", "Unit or Basis for Measurement Code", "ID", 2, 2, MANDATORY, UOM_CODES),
    _e("212", "Unit Price", "R", 1, 17, MANDATORY),
    _e("639", "Basis of Unit Price Code", "ID", 2, 2),
) + _product_ids(5, MANDATORY), "One invoiced line.")

ITD = Segment("ITD", "Terms of Sale", (
    _e("336", "Terms Type Code", "ID", 2, 2, OPTIONAL,
       {"01": "Basic", "05": "Discount Not Applicable", "08": "Basic Discount Offered",
        "14": "Previously agreed upon", "22": "Cash Discount Terms Apply"}),
    _e("333", "Terms Basis Date Code", "ID", 1, 2, OPTIONAL,
       {"3": "Invoice Date", "2": "Delivery Date", "5": "Receipt of Goods"}),
    _e("338", "Terms Discount Percent", "R", 1, 6),
    _e("370", "Terms Discount Due Date", "DT", 8, 8),
    _e("351", "Terms Discount Days Due", "N0", 1, 3),
    _e("446", "Terms Net Due Date", "DT", 8, 8),
    _e("386", "Terms Net Days", "N0", 1, 3),
    _e("362", "Terms Discount Amount", "R", 1, 10),
), "Payment terms: 2% 10 net 30 and its relatives.")

TXI = Segment("TXI", "Tax Information", (
    _e("963", "Tax Type Code", "ID", 2, 2, MANDATORY,
       {"ST": "State Sales Tax", "TX": "All Taxes", "VA": "Value Added Tax",
        "CT": "County Tax", "LS": "State and Local Sales Tax"}),
    _e("782", "Monetary Amount", "R", 1, 18),
    _e("954", "Percent", "R", 1, 10),
))

SAC = Segment("SAC", "Service, Promotion, Allowance, or Charge Information", (
    _e("248", "Allowance or Charge Indicator", "ID", 1, 1, MANDATORY,
       {"A": "Allowance", "C": "Charge", "N": "No Allowance or Charge"}),
    _e("1300", "Service, Promotion, Allowance, or Charge Code", "ID", 4, 4),
    _e("559", "Agency Qualifier Code", "ID", 2, 2),
    _e("1301", "Agency Service, Promotion, Allowance, or Charge Code", "AN", 1, 10),
    _e("610", "Amount", "N2", 1, 15),
))

TDS = Segment("TDS", "Total Monetary Value Summary", (
    _e("361", "Total Invoice Amount", "N2", 1, 15, MANDATORY),
    _e("390", "Amount Subject to Terms Discount", "N2", 1, 15),
    _e("391", "Discounted Amount Due", "N2", 1, 15),
    _e("362", "Terms Discount Amount", "N2", 1, 15),
), "The invoice total, as an integer with two implied decimals: 12500 is 125.00.")

CAD = Segment("CAD", "Carrier Detail", (
    _e("91", "Transportation Method/Type Code", "ID", 1, 2),
    _e("206", "Equipment Initial", "AN", 1, 4),
    _e("207", "Equipment Number", "AN", 1, 10),
    _e("140", "Standard Carrier Alpha Code", "ID", 2, 4),
    _e("387", "Routing", "AN", 1, 35),
))

AK1 = Segment("AK1", "Functional Group Response Header", (
    _e("479", "Functional Identifier Code", "ID", 2, 2, MANDATORY, FUNCTIONAL_GROUP_CODES),
    _e("28", "Group Control Number", "N0", 1, 9, MANDATORY),
    _e("480", "Version / Release / Industry Identifier Code", "AN", 1, 12),
), "Which functional group is being acknowledged.")

AK2 = Segment("AK2", "Transaction Set Response Header", (
    _e("143", "Transaction Set Identifier Code", "ID", 3, 3, MANDATORY),
    _e("329", "Transaction Set Control Number", "AN", 4, 9, MANDATORY),
), "Which transaction set within the group.")

AK3 = Segment("AK3", "Data Segment Note", (
    _e("721", "Segment ID Code", "ID", 2, 3, MANDATORY),
    _e("719", "Segment Position in Transaction Set", "N0", 1, 6, MANDATORY),
    _e("447", "Loop Identifier Code", "AN", 1, 6),
    _e("720", "Segment Syntax Error Code", "ID", 1, 3, OPTIONAL, SEGMENT_ERROR_CODES),
), "Which segment was wrong, counted from ST as segment 1.")

AK4 = Segment("AK4", "Data Element Note", (
    _e("722", "Element Position in Segment", "N0", 1, 2, MANDATORY),
    _e("725", "Data Element Reference Number", "N0", 1, 4),
    _e("723", "Data Element Syntax Error Code", "ID", 1, 3, MANDATORY, ELEMENT_ERROR_CODES),
    _e("724", "Copy of Bad Data Element", "AN", 1, 99),
), "Which element of that segment, why, and what it said.")

AK5 = Segment("AK5", "Transaction Set Response Trailer", (
    _e("717", "Transaction Set Acknowledgment Code", "ID", 1, 1, MANDATORY, TS_ACK_CODES),
    _e("718", "Transaction Set Syntax Error Code", "ID", 1, 3, OPTIONAL, TS_ERROR_CODES),
    _e("718", "Transaction Set Syntax Error Code", "ID", 1, 3, OPTIONAL, TS_ERROR_CODES),
    _e("718", "Transaction Set Syntax Error Code", "ID", 1, 3, OPTIONAL, TS_ERROR_CODES),
), "The verdict on one transaction set.")

AK9 = Segment("AK9", "Functional Group Response Trailer", (
    _e("715", "Functional Group Acknowledge Code", "ID", 1, 1, MANDATORY, GROUP_ACK_CODES),
    _e("97", "Number of Transaction Sets Included", "N0", 1, 6, MANDATORY),
    _e("123", "Number of Received Transaction Sets", "N0", 1, 6, MANDATORY),
    _e("2", "Number of Accepted Transaction Sets", "N0", 1, 6, MANDATORY),
    _e("716", "Functional Group Syntax Error Code", "ID", 1, 3, OPTIONAL, GROUP_ERROR_CODES),
), "The verdict on the group, and the counts that prove it adds up.")


# ---------------------------------------------------------------------------
# The X12 interchange envelope
#
# ISA is the odd one out: every element is fixed width, so the segment is
# always exactly 106 characters and a receiver can read the delimiters out of
# it before it knows anything else.  That is why ISA16 declares the component
# separator and, from 00501, ISA11 declares the repetition separator.
# ---------------------------------------------------------------------------

ISA = Segment("ISA", "Interchange Control Header", (
    _e("I01", "Authorization Information Qualifier", "ID", 2, 2, MANDATORY,
       {"00": "No Authorization Information Present", "03": "Additional Data Identification"}),
    _e("I02", "Authorization Information", "AN", 10, 10, MANDATORY),
    _e("I03", "Security Information Qualifier", "ID", 2, 2, MANDATORY,
       {"00": "No Security Information Present", "01": "Password"}),
    _e("I04", "Security Information", "AN", 10, 10, MANDATORY),
    _e("I05", "Interchange Sender ID Qualifier", "ID", 2, 2, MANDATORY),
    _e("I06", "Interchange Sender ID", "AN", 15, 15, MANDATORY),
    _e("I05", "Interchange Receiver ID Qualifier", "ID", 2, 2, MANDATORY),
    _e("I07", "Interchange Receiver ID", "AN", 15, 15, MANDATORY),
    _e("I08", "Interchange Date", "DT", 6, 6, MANDATORY),
    _e("I09", "Interchange Time", "TM", 4, 4, MANDATORY),
    # ISA11 means two different things: in 00401 it is the standards
    # identifier and is always "U"; from 00501 it is the repetition separator.
    _e("I65", "Repetition Separator / Control Standards Identifier", "AN", 1, 1,
       MANDATORY),
    _e("I11", "Interchange Control Version Number", "ID", 5, 5, MANDATORY,
       {"00401": "Version 4 Release 1", "00501": "Version 5 Release 1"}),
    _e("I12", "Interchange Control Number", "N0", 9, 9, MANDATORY),
    _e("I13", "Acknowledgment Requested", "ID", 1, 1, MANDATORY,
       {"0": "No Interchange Acknowledgment Requested",
        "1": "Interchange Acknowledgment Requested (TA1)"}),
    _e("I14", "Usage Indicator", "ID", 1, 1, MANDATORY,
       {"P": "Production Data", "T": "Test Data", "I": "Information"}),
    _e("I15", "Component Element Separator", "AN", 1, 1, MANDATORY),
), "The outermost envelope, and the segment that declares the delimiters.")

GS = Segment("GS", "Functional Group Header", (
    _e("479", "Functional Identifier Code", "ID", 2, 2, MANDATORY, FUNCTIONAL_GROUP_CODES),
    _e("142", "Application Sender's Code", "AN", 2, 15, MANDATORY),
    _e("124", "Application Receiver's Code", "AN", 2, 15, MANDATORY),
    _e("373", "Date", "DT", 8, 8, MANDATORY),
    _e("337", "Time", "TM", 4, 8, MANDATORY),
    _e("28", "Group Control Number", "N0", 1, 9, MANDATORY),
    _e("455", "Responsible Agency Code", "ID", 1, 2, MANDATORY,
       {"X": "Accredited Standards Committee X12", "T": "Transportation Data Coordinating Committee"}),
    _e("480", "Version / Release / Industry Identifier Code", "AN", 1, 12, MANDATORY),
), "Groups transaction sets of one kind; what a 997 acknowledges.")

GE = Segment("GE", "Functional Group Trailer", (
    _e("97", "Number of Transaction Sets Included", "N0", 1, 6, MANDATORY),
    _e("28", "Group Control Number", "N0", 1, 9, MANDATORY),
))

IEA = Segment("IEA", "Interchange Control Trailer", (
    _e("I16", "Number of Included Functional Groups", "N0", 1, 5, MANDATORY),
    _e("I12", "Interchange Control Number", "N0", 9, 9, MANDATORY),
))

TA1 = Segment("TA1", "Interchange Acknowledgment", (
    _e("I12", "Interchange Control Number", "N0", 9, 9, MANDATORY),
    _e("I08", "Interchange Date", "DT", 6, 6, MANDATORY),
    _e("I09", "Interchange Time", "TM", 4, 4, MANDATORY),
    _e("I17", "Interchange Acknowledgment Code", "ID", 1, 1, MANDATORY,
       {"A": "The Transmitted Interchange Control Structure Header and Trailer Have Been Received and Have No Errors",
        "E": "The Transmitted Interchange Control Structure Header and Trailer Have Been Received and Are Accepted But Errors Are Noted",
        "R": "The Transmitted Interchange Control Structure Header and Trailer are Rejected Because of Errors"}),
    _e("I18", "Interchange Note Code", "ID", 3, 3, MANDATORY,
       {"000": "No error", "001": "The Interchange Control Number in the Header and Trailer Do Not Match",
        "024": "Invalid Interchange Content", "025": "Duplicate Interchange Control Number",
        "021": "Invalid Number of Included Groups Value"}),
), "Acknowledges the envelope itself, before anything inside it is read.")


def _address_loop(repeat: int = 200) -> Loop:
    return Loop("N1", (
        Use(N1, MANDATORY), Use(N2, max_use=2), Use(N3, max_use=2), Use(N4),
        Use(REF, max_use=12), Use(PER, max_use=3),
    ), OPTIONAL, repeat)


# ---------------------------------------------------------------------------
# X12 transaction sets
# ---------------------------------------------------------------------------

X12_850 = TransactionSet("850", "Purchase Order", "X12", (
    Use(ST, MANDATORY),
    Use(BEG, MANDATORY),
    Use(CUR),
    Use(REF, max_use=12),
    Use(PER, max_use=3),
    Use(FOB, max_use=5),
    Use(ITD, max_use=5),
    Use(DTM, max_use=10),
    _address_loop(),
    Loop("PO1", (
        Use(PO1, MANDATORY),
        Use(PID, max_use=200),
        Use(PO4),
        Use(REF, max_use=12),
        Use(DTM, max_use=10),
        Use(SAC, max_use=25),
        _address_loop(),
    ), OPTIONAL, 100000),
    Use(CTT),
    Use(SE, MANDATORY),
), group="PO", version="004010",
   purpose="The buyer orders goods: what, how many, at what price, delivered when.")

X12_855 = TransactionSet("855", "Purchase Order Acknowledgment", "X12", (
    Use(ST, MANDATORY),
    Use(BAK, MANDATORY),
    Use(CUR),
    Use(REF, max_use=12),
    Use(PER, max_use=3),
    Use(DTM, max_use=10),
    _address_loop(),
    Loop("PO1", (
        Use(PO1, MANDATORY),
        Use(ACK, max_use=104),
        Use(PID, max_use=200),
        Use(REF, max_use=12),
        Use(DTM, max_use=10),
        _address_loop(),
    ), OPTIONAL, 100000),
    Use(CTT),
    Use(SE, MANDATORY),
), group="PR", version="004010",
   purpose="The seller answers the order, line by line: accepted, changed or rejected.")

X12_856 = TransactionSet("856", "Ship Notice/Manifest", "X12", (
    Use(ST, MANDATORY),
    Use(BSN, MANDATORY),
    Use(DTM, max_use=10),
    # One flat HL loop, as the standard has it: the tree is carried by the
    # parent pointers in HL02, not by nesting on the wire.
    Loop("HL", (
        Use(HL, MANDATORY),
        Use(LIN),
        Use(SN1),
        Use(PRF),
        Use(PO4),
        Use(PID, max_use=200),
        Use(TD1, max_use=20),
        Use(TD5, max_use=12),
        Use(TD3, max_use=12),
        Use(REF, max_use=200),
        Use(DTM, max_use=10),
        _address_loop(),
    ), MANDATORY, 200000),
    Use(CTT),
    Use(SE, MANDATORY),
), group="SH", version="004010",
   purpose="The seller says what is on the truck, as a tree of shipment, order and item.")

X12_810 = TransactionSet("810", "Invoice", "X12", (
    Use(ST, MANDATORY),
    Use(BIG, MANDATORY),
    Use(CUR),
    Use(REF, max_use=12),
    Use(PER, max_use=3),
    _address_loop(),
    Use(ITD, max_use=5),
    Use(DTM, max_use=10),
    Use(FOB, max_use=5),
    Loop("IT1", (
        Use(IT1, MANDATORY),
        Use(PID, max_use=200),
        Use(REF, max_use=12),
        Use(DTM, max_use=10),
        Use(SAC, max_use=25),
        Use(TXI, max_use=10),
    ), OPTIONAL, 200000),
    Use(TDS, MANDATORY),
    Use(TXI, max_use=10),
    Use(CAD),
    Use(SAC, max_use=25),
    Use(CTT),
    Use(SE, MANDATORY),
), group="IN", version="004010",
   purpose="The seller asks to be paid, referencing the order and the shipment.")

X12_997 = TransactionSet("997", "Functional Acknowledgment", "X12", (
    Use(ST, MANDATORY),
    Use(AK1, MANDATORY),
    Loop("AK2", (
        Use(AK2, MANDATORY),
        Loop("AK3", (
            Use(AK3, MANDATORY),
            Use(AK4, max_use=99),
        ), OPTIONAL, 999999),
        Use(AK5, MANDATORY),
    ), OPTIONAL, 999999),
    Use(AK9, MANDATORY),
    Use(SE, MANDATORY),
), group="FA", version="004010",
   purpose="Syntax only: the envelope arrived and parsed, or it did not. "
           "It says nothing about whether the business accepted the order.")


# ---------------------------------------------------------------------------
# EDIFACT
#
# The structural difference from X12 that matters: an EDIFACT element may be
# *composite*, several components packed into one position and separated by
# `:`.  BGM's first element is C002, and C002's first component is 1001, the
# document name code - which is why EDIFACT documentation says "BGM C002/1001"
# where X12 would say "BEG01".
#
# EDIFACT numbers its elements too (1225, 4343, 6063), and those numbers are
# what a CONTRL error names, so they are carried here for the same reason the
# X12 numbers are.
# ---------------------------------------------------------------------------

DOCUMENT_NAME_CODES = {    # 1001
    "220": "Order", "231": "Purchase order response", "351": "Despatch advice",
    "380": "Commercial invoice", "381": "Credit note", "83": "Credit note",
}
MESSAGE_FUNCTION_CODES = {  # 1225
    "9": "Original", "1": "Cancellation", "4": "Change", "5": "Replace",
    "6": "Confirmation", "7": "Duplicate", "3": "Deletion",
}
RESPONSE_TYPE_CODES = {    # 4343 - the verdict an ORDRSP carries
    "AB": "Message acknowledgement",
    "AC": "Acknowledge - with detail and change",
    "AI": "Acknowledge - with detail, no change",
    "AP": "Accepted",
    "RE": "Rejected",
}
EDIFACT_DATE_QUALIFIERS = {  # 2005
    "137": "Document/message date/time", "2": "Delivery date/time, requested",
    "4": "Order date/time",
    "11": "Despatch date and/or time", "17": "Delivery date/time, estimated",
    "35": "Delivery date/time, actual", "132": "Arrival date/time, estimated",
    "200": "Pick-up/collection date/time of cargo",
}
EDIFACT_DATE_FORMATS = {   # 2379
    "101": "YYMMDD", "102": "CCYYMMDD", "203": "CCYYMMDDHHMM", "204": "CCYYMMDDHHMMSS",
}
EDIFACT_REFERENCE_QUALIFIERS = {  # 1153
    "ON": "Order number (purchase)", "VN": "Order number (supplier)",
    "DQ": "Delivery note number", "AAK": "Despatch advice number",
    "BM": "Bill of lading number", "IV": "Invoice number",
    "CR": "Customer reference number", "CT": "Contract number",
    "CN": "Carrier's reference number",
}
EDIFACT_PARTY_QUALIFIERS = {  # 3035
    "BY": "Buyer", "SU": "Supplier", "DP": "Delivery party", "IV": "Invoicee",
    "CN": "Consignee", "CZ": "Consignor", "SE": "Seller", "SF": "Ship from",
}
EDIFACT_ITEM_TYPES = {     # 7143
    "IN": "Buyer's item number", "SA": "Supplier's article number",
    "EN": "International Article Number (EAN)", "UP": "UPC",
    "MF": "Manufacturer's article number", "VP": "Vendor part number",
}
EDIFACT_QUANTITY_QUALIFIERS = {  # 6063
    "12": "Despatch quantity", "21": "Ordered quantity", "47": "Invoiced quantity",
    "83": "Backorder quantity", "113": "Quantity to be delivered",
}
EDIFACT_AMOUNT_QUALIFIERS = {  # 5025
    "203": "Line item amount", "79": "Total line items amount",
    "124": "Tax amount", "139": "Total payable amount", "77": "Invoice amount",
    "9": "Amount due/amount payable",
}
EDIFACT_PRICE_QUALIFIERS = {  # 5125
    "AAA": "Calculation net", "AAB": "Calculation gross",
    "AAE": "Information price, excluding tax",
}
EDIFACT_TEXT_QUALIFIERS = {  # 4451
    "AAI": "General information", "AAO": "Error description",
    "ORI": "Order information", "ZZZ": "Mutually defined",
}
EDIFACT_ACTION_CODES = {   # 0083, in CONTRL
    "4": "This level and all lower levels rejected",
    "7": "This level acknowledged, and all lower levels acknowledged",
    "8": "Interchange received",
}
EDIFACT_SYNTAX_ERRORS = {  # 0085, in CONTRL
    "12": "Invalid value", "13": "Missing",
    "14": "Value not supported in this position",
    "15": "Not supported in this position", "16": "Too many constituents",
    "35": "Too many data element or segment repetitions",
    "36": "Too many segment group repetitions",
}

# UN/ECE Recommendation 20 codes for the X12 units the mock knows about.
# Translating a document between dialects has to translate these too, and
# getting it wrong is the classic way a converted order arrives in the wrong
# multiple.
UOM_TO_EDIFACT = {
    "EA": "PCE", "PC": "PCE", "CA": "CT", "CS": "CT", "CT": "CT", "BX": "BX",
    "DZ": "DZN", "LB": "LBR", "KG": "KGM", "GA": "GLL", "FT": "FOT",
    "M": "MTR", "PL": "PF",
}
UOM_FROM_EDIFACT = {"PCE": "EA", "CT": "CA", "BX": "BX", "DZN": "DZ",
                    "LBR": "LB", "KGM": "KG", "GLL": "GA", "FOT": "FT",
                    "MTR": "M", "PF": "PL"}


def _c(ref, name, components, req=OPTIONAL):
    return Element(ref=ref, name=name, type="AN", req=req, components=tuple(components))


# -- EDIFACT service segments

UNB = Segment("UNB", "Interchange Header", (
    _c("S001", "Syntax Identifier", (
        _e("0001", "Syntax identifier", "ID", 4, 4, MANDATORY,
           {"UNOA": "Level A character set", "UNOB": "Level B", "UNOC": "Level C (Latin-1)"}),
        _e("0002", "Syntax version number", "N0", 1, 1, MANDATORY),
    ), MANDATORY),
    _c("S002", "Interchange Sender", (
        _e("0004", "Sender identification", "AN", 1, 35, MANDATORY),
        _e("0007", "Partner identification code qualifier", "AN", 1, 4),
    ), MANDATORY),
    _c("S003", "Interchange Recipient", (
        _e("0010", "Recipient identification", "AN", 1, 35, MANDATORY),
        _e("0007", "Partner identification code qualifier", "AN", 1, 4),
    ), MANDATORY),
    _c("S004", "Date/Time of Preparation", (
        _e("0017", "Date", "DT", 6, 8, MANDATORY),
        _e("0019", "Time", "TM", 4, 4, MANDATORY),
    ), MANDATORY),
    _e("0020", "Interchange control reference", "AN", 1, 14, MANDATORY),
    _c("S005", "Recipient's Reference/Password", (
        _e("0022", "Recipient's reference/password", "AN", 1, 14),
        _e("0025", "Recipient's reference/password qualifier", "AN", 2, 2),
    )),
    _e("0026", "Application reference", "AN", 1, 14),
    _e("0029", "Processing priority code", "AN", 1, 1),
    _e("0031", "Acknowledgement request", "N0", 1, 1),
    _e("0032", "Interchange agreement identifier", "AN", 1, 35),
    _e("0035", "Test indicator", "N0", 1, 1),
), "The EDIFACT envelope, and where the test flag lives.")

UNH = Segment("UNH", "Message Header", (
    _e("0062", "Message reference number", "AN", 1, 14, MANDATORY),
    _c("S009", "Message Identifier", (
        _e("0065", "Message type", "AN", 1, 6, MANDATORY),
        _e("0052", "Message version number", "AN", 1, 3, MANDATORY),
        _e("0054", "Message release number", "AN", 1, 3, MANDATORY),
        _e("0051", "Controlling agency", "AN", 1, 2, MANDATORY),
        _e("0057", "Association assigned code", "AN", 1, 6),
    ), MANDATORY),
), "Starts a message and says which one it is: ORDERS:D:96A:UN.")

UNT = Segment("UNT", "Message Trailer", (
    _e("0074", "Number of segments in the message", "N0", 1, 10, MANDATORY),
    _e("0062", "Message reference number", "AN", 1, 14, MANDATORY),
))

UNZ = Segment("UNZ", "Interchange Trailer", (
    _e("0036", "Interchange control count", "N0", 1, 6, MANDATORY),
    _e("0020", "Interchange control reference", "AN", 1, 14, MANDATORY),
))

UNS = Segment("UNS", "Section Control", (
    _e("0081", "Section identification", "ID", 1, 1, MANDATORY,
       {"D": "Header/detail section separation", "S": "Detail/summary section separation"}),
), "Separates the detail section from the summary; easy to forget, and mandatory.")

# -- EDIFACT business segments

BGM = Segment("BGM", "Beginning of Message", (
    _c("C002", "Document/Message Name", (
        _e("1001", "Document name code", "ID", 1, 3, OPTIONAL, DOCUMENT_NAME_CODES),
        _e("1131", "Code list identification code", "AN", 1, 17),
        _e("3055", "Code list responsible agency code", "AN", 1, 3),
        _e("1000", "Document name", "AN", 1, 35),
    )),
    _c("C106", "Document/Message Identification", (
        _e("1004", "Document identifier", "AN", 1, 35),
        _e("1056", "Version identifier", "AN", 1, 9),
        _e("1060", "Revision identifier", "AN", 1, 6),
    )),
    _e("1225", "Message function code", "ID", 1, 3, OPTIONAL, MESSAGE_FUNCTION_CODES),
    _e("4343", "Response type code", "ID", 1, 3, OPTIONAL, RESPONSE_TYPE_CODES),
), "What kind of document this is, its number, and - in a response - the verdict.")

E_DTM = Segment("DTM", "Date/Time/Period", (
    _c("C507", "Date/Time/Period", (
        _e("2005", "Date or time or period function code qualifier", "ID", 1, 3,
           MANDATORY, EDIFACT_DATE_QUALIFIERS),
        _e("2380", "Date or time or period value", "AN", 1, 35),
        _e("2379", "Date or time or period format code", "ID", 1, 3, OPTIONAL,
           EDIFACT_DATE_FORMATS),
    ), MANDATORY),
), "A date, its meaning and its format - EDIFACT states the format explicitly.")

RFF = Segment("RFF", "Reference", (
    _c("C506", "Reference", (
        _e("1153", "Reference code qualifier", "ID", 1, 3, MANDATORY,
           EDIFACT_REFERENCE_QUALIFIERS),
        _e("1154", "Reference identifier", "AN", 1, 70),
        _e("1156", "Document line identifier", "AN", 1, 6),
        _e("4000", "Reference version identifier", "AN", 1, 35),
        _e("1060", "Revision identifier", "AN", 1, 6),
    ), MANDATORY),
))

NAD = Segment("NAD", "Name and Address", (
    _e("3035", "Party function code qualifier", "ID", 1, 3, MANDATORY,
       EDIFACT_PARTY_QUALIFIERS),
    _c("C082", "Party Identification Details", (
        _e("3039", "Party identifier", "AN", 1, 35, MANDATORY),
        _e("1131", "Code list identification code", "AN", 1, 17),
        _e("3055", "Code list responsible agency code", "AN", 1, 3),
    )),
    _c("C058", "Name and Address", (
        _e("3124", "Name and address description", "AN", 1, 35, MANDATORY),
    )),
    _c("C080", "Party Name", (
        _e("3036", "Party name", "AN", 1, 35, MANDATORY),
        _e("3036", "Party name", "AN", 1, 35),
        _e("3045", "Party name format code", "AN", 1, 3),
    )),
    _c("C059", "Street", (
        _e("3042", "Street and number or post office box identifier", "AN", 1, 35, MANDATORY),
        _e("3042", "Street and number or post office box identifier", "AN", 1, 35),
    )),
    _e("3164", "City name", "AN", 1, 35),
    _e("3229", "Country sub-entity identification", "AN", 1, 9),
    _e("3251", "Postal identification code", "AN", 1, 17),
    _e("3207", "Country identifier", "AN", 1, 3),
), "A party and its address, in one segment - where X12 uses N1/N3/N4.")

CUX = Segment("CUX", "Currencies", (
    _c("C504", "Currency Details", (
        _e("6347", "Currency usage code qualifier", "ID", 1, 3, MANDATORY,
           {"2": "Reference currency", "3": "Target currency", "4": "Invoicing currency"}),
        _e("6345", "Currency identification code", "ID", 3, 3, OPTIONAL, CURRENCY_CODES),
        _e("6343", "Currency type code qualifier", "ID", 1, 3),
        _e("6348", "Currency rate base", "N0", 1, 4),
    )),
    _c("C504", "Currency Details", (
        _e("6347", "Currency usage code qualifier", "ID", 1, 3, MANDATORY),
        _e("6345", "Currency identification code", "ID", 3, 3),
    )),
    _e("5402", "Currency exchange rate", "R", 1, 12),
))

PAT = Segment("PAT", "Payment Terms Basis", (
    _e("4279", "Payment terms type code qualifier", "ID", 1, 3, MANDATORY,
       {"1": "Basic", "3": "Fixed date", "20": "Penalty terms", "22": "Discount"}),
    _c("C110", "Payment Terms", (
        _e("4277", "Payment terms description identifier", "AN", 1, 17, MANDATORY),
        _e("1131", "Code list identification code", "AN", 1, 17),
        _e("3055", "Code list responsible agency code", "AN", 1, 3),
        _e("4276", "Payment terms description", "AN", 1, 35),
    )),
    _c("C112", "Terms/Time Information", (
        _e("2475", "Payment time reference code", "ID", 1, 3, MANDATORY),
        _e("2009", "Time relation code", "ID", 1, 3),
        _e("2151", "Type of period code", "ID", 1, 3),
        _e("2152", "Number of periods", "N0", 1, 3),
    )),
))

LIN_E = Segment("LIN", "Line Item", (
    _e("1082", "Line item identifier", "AN", 1, 6),
    _e("1229", "Action request/notification description code", "ID", 1, 3),
    _c("C212", "Item Number Identification", (
        _e("7140", "Item identifier", "AN", 1, 35),
        _e("7143", "Item type identification code", "ID", 1, 3, OPTIONAL, EDIFACT_ITEM_TYPES),
        _e("1131", "Code list identification code", "AN", 1, 17),
        _e("3055", "Code list responsible agency code", "AN", 1, 3),
    )),
), "One line, and the item number the parties agreed to identify it by.")

PIA = Segment("PIA", "Additional Product ID", (
    _e("4347", "Product identifier code qualifier", "ID", 1, 3, MANDATORY,
       {"1": "Additional identification", "5": "Product identification"}),
    _c("C212", "Item Number Identification", (
        _e("7140", "Item identifier", "AN", 1, 35, MANDATORY),
        _e("7143", "Item type identification code", "ID", 1, 3, OPTIONAL, EDIFACT_ITEM_TYPES),
    ), MANDATORY),
), "Any further item numbers - a UPC beside the supplier's article number.")

IMD = Segment("IMD", "Item Description", (
    _e("7077", "Description format code", "ID", 1, 3, OPTIONAL,
       {"A": "Free-form short description", "B": "Code and text", "C": "Code (from industry list)",
        "E": "Free-form", "F": "Free-form"}),
    _e("7081", "Item characteristic code", "ID", 1, 3),
    _c("C273", "Item Description", (
        _e("7009", "Item description code", "AN", 1, 17),
        _e("1131", "Code list identification code", "AN", 1, 17),
        _e("3055", "Code list responsible agency code", "AN", 1, 3),
        _e("7008", "Item description", "AN", 1, 256),
        _e("7008", "Item description", "AN", 1, 256),
        _e("3453", "Language name code", "AN", 1, 3),
    )),
))

QTY = Segment("QTY", "Quantity", (
    _c("C186", "Quantity Details", (
        _e("6063", "Quantity type code qualifier", "ID", 1, 3, MANDATORY,
           EDIFACT_QUANTITY_QUALIFIERS),
        _e("6060", "Quantity", "R", 1, 35, MANDATORY),
        _e("6411", "Measurement unit code", "AN", 1, 8),
    ), MANDATORY),
), "A quantity, named by what kind it is - ordered, confirmed, despatched, invoiced.")

PRI = Segment("PRI", "Price Details", (
    _c("C509", "Price Information", (
        _e("5125", "Price code qualifier", "ID", 1, 3, MANDATORY, EDIFACT_PRICE_QUALIFIERS),
        _e("5118", "Price amount", "R", 1, 15),
        _e("5375", "Price type code", "ID", 1, 3),
        _e("5387", "Price specification code", "ID", 1, 3),
        _e("5284", "Unit price basis quantity", "R", 1, 9),
        _e("6411", "Measurement unit code", "AN", 1, 8),
    )),
))

MOA = Segment("MOA", "Monetary Amount", (
    _c("C516", "Monetary Amount", (
        _e("5025", "Monetary amount type code qualifier", "ID", 1, 3, MANDATORY,
           EDIFACT_AMOUNT_QUALIFIERS),
        _e("5004", "Monetary amount", "R", 1, 35),
        _e("6345", "Currency identification code", "ID", 3, 3),
        _e("6343", "Currency type code qualifier", "ID", 1, 3),
        _e("4405", "Status description code", "ID", 1, 3),
    ), MANDATORY),
), "An amount, named by what it is an amount of - unlike X12's positional TDS.")

FTX = Segment("FTX", "Free Text", (
    _e("4451", "Text subject code qualifier", "ID", 1, 3, MANDATORY, EDIFACT_TEXT_QUALIFIERS),
    _e("4453", "Free text function code", "ID", 1, 3),
    _c("C107", "Text Reference", (
        _e("4441", "Free text description code", "AN", 1, 17, MANDATORY),
    )),
    _c("C108", "Text Literal", (
        _e("4440", "Free text", "AN", 1, 512, MANDATORY),
        _e("4440", "Free text", "AN", 1, 512),
    )),
), "Prose. In a response it carries the reason a line was changed or refused.")

TDT = Segment("TDT", "Transport Information", (
    _e("8051", "Transport stage code qualifier", "ID", 1, 3, MANDATORY,
       {"20": "Main carriage transport", "10": "Pre-carriage", "30": "On-carriage"}),
    _e("8028", "Means of transport journey identifier", "AN", 1, 17),
    _c("C220", "Mode of Transport", (
        _e("8067", "Transport mode name code", "ID", 1, 3),
        _e("8066", "Transport mode name", "AN", 1, 17),
    )),
    _c("C228", "Transport Means", (
        _e("8179", "Transport means description code", "AN", 1, 8),
        _e("8178", "Transport means description", "AN", 1, 17),
    )),
    _c("C040", "Carrier", (
        _e("3127", "Carrier identifier", "AN", 1, 17),
        _e("1131", "Code list identification code", "AN", 1, 17),
        _e("3055", "Code list responsible agency code", "AN", 1, 3),
        _e("3128", "Carrier name", "AN", 1, 35),
    )),
))

CPS = Segment("CPS", "Consignment Packing Sequence", (
    _e("7164", "Hierarchical structure level identifier", "AN", 1, 12, MANDATORY),
    _e("7166", "Hierarchical structure parent identifier", "AN", 1, 12),
    _e("7075", "Packaging level code", "ID", 1, 3),
), "The despatch advice's answer to HL: a packing hierarchy by parent pointer.")

PAC = Segment("PAC", "Package", (
    _e("7224", "Package quantity", "N0", 1, 8),
    _c("C531", "Packaging Details", (
        _e("7075", "Packaging level code", "ID", 1, 3),
    )),
    _c("C202", "Package Type", (
        _e("7065", "Package type description code", "AN", 1, 17),
        _e("1131", "Code list identification code", "AN", 1, 17),
        _e("3055", "Code list responsible agency code", "AN", 1, 3),
        _e("7064", "Type of packages", "AN", 1, 35),
    )),
))

CNT = Segment("CNT", "Control Total", (
    _c("C270", "Control", (
        _e("6069", "Control total type code qualifier", "ID", 1, 3, MANDATORY,
           {"1": "Algebraic total of quantity values", "2": "Number of line items in message",
            "4": "Number of lines in message", "11": "Total quantity"}),
        _e("6066", "Control total quantity", "R", 1, 18, MANDATORY),
        _e("6411", "Measurement unit code", "AN", 1, 8),
    ), MANDATORY),
))

# -- CONTRL

UCI = Segment("UCI", "Interchange Response", (
    _e("0020", "Interchange control reference", "AN", 1, 14, MANDATORY),
    _c("S002", "Interchange Sender", (
        _e("0004", "Sender identification", "AN", 1, 35, MANDATORY),
        _e("0007", "Partner identification code qualifier", "AN", 1, 4),
    ), MANDATORY),
    _c("S003", "Interchange Recipient", (
        _e("0010", "Recipient identification", "AN", 1, 35, MANDATORY),
        _e("0007", "Partner identification code qualifier", "AN", 1, 4),
    ), MANDATORY),
    _e("0083", "Action code", "ID", 1, 3, MANDATORY, EDIFACT_ACTION_CODES),
    _e("0085", "Syntax error code", "ID", 1, 3, OPTIONAL, EDIFACT_SYNTAX_ERRORS),
), "The verdict on the interchange as a whole.")

UCM = Segment("UCM", "Message Response", (
    _e("0062", "Message reference number", "AN", 1, 14, MANDATORY),
    _c("S009", "Message Identifier", (
        _e("0065", "Message type", "AN", 1, 6, MANDATORY),
        _e("0052", "Message version number", "AN", 1, 3, MANDATORY),
        _e("0054", "Message release number", "AN", 1, 3, MANDATORY),
        _e("0051", "Controlling agency", "AN", 1, 2, MANDATORY),
    ), MANDATORY),
    _e("0083", "Action code", "ID", 1, 3, MANDATORY, EDIFACT_ACTION_CODES),
    _e("0085", "Syntax error code", "ID", 1, 3, OPTIONAL, EDIFACT_SYNTAX_ERRORS),
), "The verdict on one message inside the interchange.")

UCS = Segment("UCS", "Segment Error Indication", (
    _e("0096", "Segment position in message body", "N0", 1, 6, MANDATORY),
    _e("0085", "Syntax error code", "ID", 1, 3, OPTIONAL, EDIFACT_SYNTAX_ERRORS),
), "Which segment was wrong, counted from UNH as segment 1.")

UCD = Segment("UCD", "Data Element Error Indication", (
    _e("0085", "Syntax error code", "ID", 1, 3, MANDATORY, EDIFACT_SYNTAX_ERRORS),
    _c("S011", "Data Element Identification", (
        _e("0098", "Erroneous data element position in segment", "N0", 1, 3, MANDATORY),
        _e("0104", "Erroneous component data element position", "N0", 1, 3),
        _e("0136", "Erroneous data element occurrence", "N0", 1, 6),
    ), MANDATORY),
), "Which element of that segment, and which component of it.")


# ---------------------------------------------------------------------------
# EDIFACT messages (D.96A, the release still most widely traded)
# ---------------------------------------------------------------------------

def _edifact_party_group(repeat: int = 99) -> Loop:
    return Loop("NAD", (
        Use(NAD, MANDATORY),
        Loop("RFF", (Use(RFF, MANDATORY),), OPTIONAL, 9),
    ), OPTIONAL, repeat)


EDIFACT_ORDERS = TransactionSet("ORDERS", "Purchase Order Message", "EDIFACT", (
    Use(UNH, MANDATORY),
    Use(BGM, MANDATORY),
    Use(E_DTM, max_use=35),
    Use(FTX, max_use=99),
    Loop("RFF", (Use(RFF, MANDATORY), Use(E_DTM)), OPTIONAL, 99),
    _edifact_party_group(),
    Loop("CUX", (Use(CUX, MANDATORY), Use(E_DTM, max_use=5)), OPTIONAL, 99),
    Loop("PAT", (Use(PAT, MANDATORY), Use(E_DTM, max_use=5)), OPTIONAL, 10),
    Loop("LIN", (
        Use(LIN_E, MANDATORY),
        Use(PIA, max_use=25),
        Use(IMD, max_use=99),
        Use(QTY, max_use=99),
        Use(E_DTM, max_use=35),
        Use(MOA, max_use=30),
        Use(FTX, max_use=99),
        Loop("PRI", (Use(PRI, MANDATORY),), OPTIONAL, 25),
        Loop("RFF", (Use(RFF, MANDATORY), Use(E_DTM)), OPTIONAL, 99),
        _edifact_party_group(),
    ), OPTIONAL, 200000),
    Use(UNS, MANDATORY),
    Use(CNT, max_use=10),
    Use(UNT, MANDATORY),
), version="D:96A:UN",
   purpose="The EDIFACT order. Same intent as an 850; almost nothing in common at the surface.")

EDIFACT_ORDRSP = TransactionSet("ORDRSP", "Purchase Order Response Message", "EDIFACT", (
    Use(UNH, MANDATORY),
    Use(BGM, MANDATORY),
    Use(E_DTM, max_use=35),
    Use(FTX, max_use=99),
    Loop("RFF", (Use(RFF, MANDATORY), Use(E_DTM)), OPTIONAL, 99),
    _edifact_party_group(),
    Loop("CUX", (Use(CUX, MANDATORY),), OPTIONAL, 99),
    Loop("LIN", (
        Use(LIN_E, MANDATORY),
        Use(PIA, max_use=25),
        Use(IMD, max_use=99),
        Use(QTY, max_use=99),
        Use(E_DTM, max_use=35),
        Use(MOA, max_use=30),
        Use(FTX, max_use=99),
        Loop("PRI", (Use(PRI, MANDATORY),), OPTIONAL, 25),
        Loop("RFF", (Use(RFF, MANDATORY),), OPTIONAL, 99),
    ), OPTIONAL, 200000),
    Use(UNS, MANDATORY),
    Use(CNT, max_use=10),
    Use(UNT, MANDATORY),
), version="D:96A:UN",
   purpose="The seller's answer. The verdict is in BGM's response type code and, "
           "line by line, in the confirmed quantity.")

EDIFACT_DESADV = TransactionSet("DESADV", "Despatch Advice Message", "EDIFACT", (
    Use(UNH, MANDATORY),
    Use(BGM, MANDATORY),
    Use(E_DTM, max_use=10),
    Loop("RFF", (Use(RFF, MANDATORY), Use(E_DTM)), OPTIONAL, 10),
    _edifact_party_group(),
    Loop("TDT", (Use(TDT, MANDATORY), Use(E_DTM, max_use=5)), OPTIONAL, 10),
    Loop("CPS", (
        Use(CPS, MANDATORY),
        Loop("PAC", (Use(PAC, MANDATORY),), OPTIONAL, 1000),
        Loop("LIN", (
            Use(LIN_E, MANDATORY),
            Use(PIA, max_use=10),
            Use(IMD, max_use=25),
            Use(QTY, max_use=10),
            Use(E_DTM, max_use=5),
            Use(FTX, max_use=5),
            Loop("RFF", (Use(RFF, MANDATORY),), OPTIONAL, 10),
        ), OPTIONAL, 9999),
    ), MANDATORY, 9999),
    Use(UNT, MANDATORY),
), version="D:96A:UN",
   purpose="What is on the truck. The CPS packing hierarchy plays the part X12 gives to HL.")

EDIFACT_INVOIC = TransactionSet("INVOIC", "Invoice Message", "EDIFACT", (
    Use(UNH, MANDATORY),
    Use(BGM, MANDATORY),
    Use(E_DTM, max_use=35),
    Use(FTX, max_use=99),
    Loop("RFF", (Use(RFF, MANDATORY), Use(E_DTM)), OPTIONAL, 99),
    _edifact_party_group(),
    Loop("CUX", (Use(CUX, MANDATORY), Use(E_DTM, max_use=5)), OPTIONAL, 99),
    Loop("PAT", (Use(PAT, MANDATORY), Use(E_DTM, max_use=5)), OPTIONAL, 10),
    Loop("LIN", (
        Use(LIN_E, MANDATORY),
        Use(PIA, max_use=25),
        Use(IMD, max_use=99),
        Use(QTY, max_use=99),
        Use(E_DTM, max_use=35),
        Use(MOA, max_use=30),
        Use(FTX, max_use=99),
        Loop("PRI", (Use(PRI, MANDATORY),), OPTIONAL, 25),
        Loop("RFF", (Use(RFF, MANDATORY),), OPTIONAL, 99),
    ), OPTIONAL, 200000),
    Use(UNS, MANDATORY),
    Use(MOA, max_use=100),
    Use(CNT, max_use=10),
    Use(UNT, MANDATORY),
), version="D:96A:UN",
   purpose="The EDIFACT invoice. Every total is a named MOA, where X12 puts them "
           "in fixed positions of TDS.")

EDIFACT_CONTRL = TransactionSet("CONTRL", "Syntax and Service Report Message", "EDIFACT", (
    Use(UNH, MANDATORY),
    Use(UCI, MANDATORY),
    Loop("UCM", (
        Use(UCM, MANDATORY),
        Loop("UCS", (Use(UCS, MANDATORY), Use(UCD, max_use=99)), OPTIONAL, 999),
    ), OPTIONAL, 999999),
    Use(UNT, MANDATORY),
), version="D:3:UN",
   purpose="EDIFACT's 997: syntax only, and no opinion about the business.")


# ---------------------------------------------------------------------------
# Registries
#
# `KIND` is how the rest of the package stays dialect-agnostic.  The pipeline
# reasons about an order, a response, a despatch and an invoice; only the
# reader and writer care that an order is called 850 here and ORDERS there.
# ---------------------------------------------------------------------------

ORDER = "order"
RESPONSE = "response"
DESPATCH = "despatch"
INVOICE = "invoice"
ACKNOWLEDGMENT = "acknowledgment"

KINDS = (ORDER, RESPONSE, DESPATCH, INVOICE, ACKNOWLEDGMENT)

X12_SETS = {s.code: s for s in (X12_850, X12_855, X12_856, X12_810, X12_997)}
EDIFACT_SETS = {s.code: s for s in (EDIFACT_ORDERS, EDIFACT_ORDRSP,
                                    EDIFACT_DESADV, EDIFACT_INVOIC, EDIFACT_CONTRL)}
SETS = {("X12", code): s for code, s in X12_SETS.items()}
SETS.update({("EDIFACT", code): s for code, s in EDIFACT_SETS.items()})

DIALECTS = ("X12", "EDIFACT")

SET_FOR_KIND = {
    "X12": {ORDER: "850", RESPONSE: "855", DESPATCH: "856",
            INVOICE: "810", ACKNOWLEDGMENT: "997"},
    "EDIFACT": {ORDER: "ORDERS", RESPONSE: "ORDRSP", DESPATCH: "DESADV",
                INVOICE: "INVOIC", ACKNOWLEDGMENT: "CONTRL"},
}
KIND_OF = {}
for _dialect, _map in SET_FOR_KIND.items():
    for _kind, _code in _map.items():
        KIND_OF[(_dialect, _code)] = _kind


def lookup(dialect: str, code: str) -> Optional[TransactionSet]:
    """The definition of a transaction set, or None if the mock does not know it."""
    return SETS.get((dialect, code))


def kind_of(dialect: str, code: str) -> str:
    """Which business document a set code stands for, in either dialect."""
    return KIND_OF.get((dialect, code), "")


def set_code(dialect: str, kind: str) -> str:
    """The set code a dialect uses for a business document - 850, or ORDERS."""
    return SET_FOR_KIND[dialect][kind]
