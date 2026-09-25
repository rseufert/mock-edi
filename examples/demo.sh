#!/usr/bin/env bash
#
# A guided tour of mock-edi in curl.
#
#   bash examples/demo.sh                    # against http://127.0.0.1:8080
#   BASE=http://host:9000 bash examples/demo.sh
#
# Start the mock first:
#
#   python3 -m mockedi --port 8080
#
# Some of the tour needs a mock started with more than the defaults - a drop
# directory to trade through, a despatch window to change an order inside.
# Rather than demand a particular command line, the tour asks the mock what it
# has and shows what it can, naming the flag for anything it skips. To see all
# of it:
#
#   python3 -m mockedi --port 8080 \
#     --drop-dir /tmp/edi/in --pickup-dir /tmp/edi/out --drop-settle-ms 0 \
#     --despatch-delay 3600000 --invoice-delay 3600000
#
# `set -eu` rather than `set -euo pipefail`: several commands here pipe into
# `sed -n 1p`, and under pipefail a SIGPIPE from the reader aborts the script.
set -eu

BASE="${BASE:-http://127.0.0.1:8080}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
skip() { printf '  \033[2m(skipped: %s)\033[0m\n' "$*"; }
run() { printf '\n  $ %s\n' "$*"; }

# What is this mock able to show us?
DROP_DIR=$(curl -s "$BASE/_mock/drop" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['dropDir'])")
PICKUP_DIR=$(curl -s "$BASE/_mock/drop" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['pickupDir'])")
DESPATCH_DELAY=$(curl -s "$BASE/_mock/state" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['delays']['despatch'])")

order_x12() {  # $1 = purchase order number, $2 = interchange/group control
  printf 'ISA*00*          *00*          *ZZ*ACME           *ZZ*MOCKEDI        *260924*1030*U*00401*%09d*0*T*>~GS*PO*ACME*MOCKEDI*20260924*1030*%s*X*004010~ST*850*0001~BEG*00*SA*%s**20260924~CUR*BY*USD~DTM*002*20261010~N1*ST*Acme DC 4*92*ACME-DC4~N3*9 Dock Road~N4*Columbus*OH*43217*US~PO1*1*100*EA*12.50**VP*WIDGET-001~PO1*2*40*EA*4.15**VP*BRKT-050~CTT*2~SE*11*0001~GE*1*%s~IEA*1*%09d~' \
    "$2" "$2" "$1" "$2" "$2"
}

say "0. Is it there?"
run "curl -s $BASE/_mock/health"
curl -s "$BASE/_mock/health"

say "1. Who does it trade with?"
note "Four partners, each configured to misbehave in a different way."
run "curl -s $BASE/_mock/partners"
curl -s "$BASE/_mock/partners" \
  | tr ',' '\n' | grep -E '"(id|dialect|behaviour)"' | paste - - - | sed 's/[" ]//g'

say "2. Send it an 850."
order_x12 PO-DEMO-1 101 > /tmp/mock-edi-order.x12
run "curl -s -X POST --data-binary @order.x12 $BASE/edi"
curl -s -X POST --data-binary @/tmp/mock-edi-order.x12 \
  -H 'Content-Type: application/edi-x12' "$BASE/edi"

say "3. Collect what came back."
note "Collecting empties the mailbox, the way reading one does."
run "curl -s '$BASE/_mock/mailbox?raw'"
curl -s "$BASE/_mock/mailbox?raw"

say "4. The order, as the seller now sees it."
run "curl -s $BASE/_mock/orders/PO-DEMO-1"
curl -s "$BASE/_mock/orders/PO-DEMO-1"

say "5. What has not been acknowledged?"
note "The mock reads 997s as well as sending them, so it knows what is owed."
run "curl -s $BASE/_mock/unacknowledged"
curl -s "$BASE/_mock/unacknowledged" \
  | python3 -c "import json,sys; [print('  %-4s %-14s group %s set %s' % (r['code'], r['reference'], r['group_control'], r['control'])) for r in json.load(sys.stdin)]"

say "6. Send a 997 back, refusing the acknowledgment."
note "The buyer's translator could not parse the seller's 855. The control"
note "numbers are read out of the document the mock actually sent, which is"
note "what a real partner's translator does - and is why both are needed:"
note "ST02 is only unique within its functional group."
SENT=$(curl -s "$BASE/_mock/documents?direction=out&code=855&limit=1" \
  | python3 -c "import json,sys; r=json.load(sys.stdin)[0]; print(r['group_control'], r['control'])")
GS=$(echo "$SENT" | cut -d' ' -f1); ST=$(echo "$SENT" | cut -d' ' -f2)
note "the 855 went out as group $GS, transaction set $ST"
printf 'ISA*00*          *00*          *ZZ*ACME           *ZZ*MOCKEDI        *260924*1040*U*00401*000000102*0*T*>~GS*FA*ACME*MOCKEDI*20260924*1040*102*X*004010~ST*997*0001~AK1*PR*%s*004010~AK2*855*%s~AK3*BAK*2**8~AK4*2**7*ZZ~AK5*R*5~AK9*R*1*1*0~SE*8*0001~GE*1*102~IEA*1*000000102~' \
  "$GS" "$ST" > /tmp/mock-edi-997.x12
run "curl -s -X POST --data-binary @997.x12 $BASE/edi"
curl -s -X POST --data-binary @/tmp/mock-edi-997.x12 "$BASE/edi" \
  | python3 -c "import json,sys; [print('  %s %s -> %s (matched %s)\n    %s' % (a['code'], a['control'], a['status'], a['matched'], a['note'])) for a in json.load(sys.stdin)['acknowledged']]"
note "and it is no longer outstanding:"
curl -s "$BASE/_mock/unacknowledged" \
  | python3 -c "import json,sys; print('  ', [r['code'] for r in json.load(sys.stdin)] or 'nothing')"

say "7. Make the partner short-ship, and send the same order again."
run "curl -s -X PATCH -d '{\"behaviour\":\"short-ship\"}' $BASE/_mock/partners/ACME"
curl -s -X PATCH -H 'Content-Type: application/json' \
  -d '{"behaviour":"short-ship"}' "$BASE/_mock/partners/ACME" \
  | tr ',' '\n' | grep '"behaviour"'
order_x12 PO-DEMO-2 103 > /tmp/mock-edi-order2.x12
curl -s -X POST --data-binary @/tmp/mock-edi-order2.x12 "$BASE/edi" > /dev/null
note "Now the 855 confirms less than was ordered:"
curl -s "$BASE/_mock/mailbox?partner=ACME&kind=response&raw" \
  | grep -E '^(BAK|PO1|ACK)' || true
curl -s -X PATCH -H 'Content-Type: application/json' \
  -d '{"behaviour":"accept"}' "$BASE/_mock/partners/ACME" > /dev/null
curl -s "$BASE/_mock/mailbox?raw" > /dev/null

say "8. Change an order that has not shipped yet."
if [ "$DESPATCH_DELAY" -eq 0 ]; then
  skip "start the mock with --despatch-delay 3600000 to leave a window"
  note "A change is only meaningful before the goods leave. With no delay the"
  note "order is invoiced before the POST returns, so every change is refused -"
  note "which is correct, and is why this section needs a window."
else
  # Finish and clear what the earlier sections left outstanding, so that what
  # follows is about this order and not about all of them.
  curl -s -X POST "$BASE/_mock/advance?all" > /dev/null
  curl -s "$BASE/_mock/mailbox?raw" > /dev/null
  order_x12 PO-DEMO-3 104 > /tmp/mock-edi-order3.x12
  curl -s -X POST --data-binary @/tmp/mock-edi-order3.x12 "$BASE/edi" > /dev/null
  note "The despatch is promised but not packed:"
  run "curl -s $BASE/_mock/scheduled"
  curl -s "$BASE/_mock/scheduled" \
    | python3 -c "import json,sys; [print('  %-10s %-12s due %s' % (r['kind'], r['po_number'], r['due_at'])) for r in json.load(sys.stdin)]"
  note "so an 860 can still change it: line 1 down to 60, line 2 deleted."
  printf 'ISA*00*          *00*          *ZZ*ACME           *ZZ*MOCKEDI        *260924*1130*U*00401*000000105*0*T*>~GS*PC*ACME*MOCKEDI*20260924*1130*105*X*004010~ST*860*0001~BCH*04*SA*PO-DEMO-3**1*20260925****20260924~POC*1*QD*60**EA*12.50**VP*WIDGET-001~POC*2*DI*0**EA*0**VP*BRKT-050~CTT*2~SE*6*0001~GE*1*105~IEA*1*000000105~' > /tmp/mock-edi-860.x12
  run "curl -s -X POST --data-binary @860.x12 $BASE/edi"
  curl -s -X POST --data-binary @/tmp/mock-edi-860.x12 "$BASE/edi" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print('  changed:', d['changed'], '| refused:', d['refusals'], '| queued:', [q['code'] for q in d['queued']])"
  note "the 865 answers line by line:"
  curl -s "$BASE/_mock/mailbox?kind=change-response&raw" \
    | grep -E '^(BCA|POC|ACK|REF\*ZZ)' || true
  note "and the despatch, when it comes due, ships what the change left:"
  curl -s -X POST "$BASE/_mock/advance?all" > /dev/null
  curl -s "$BASE/_mock/mailbox?kind=despatch&raw" | grep -E '^(LIN|SN1)' || true
  curl -s "$BASE/_mock/mailbox?raw" > /dev/null
fi

say "9. An EDIFACT order, to the EDIFACT partner."
cat > /tmp/mock-edi-order.edifact <<'EDI'
UNA:+.? '
UNB+UNOC:3+EURODIS:ZZ+MOCKEDI:ZZ+260924:1030+9501'
UNH+1+ORDERS:D:96A:UN'
BGM+220+PO-2026-00501+9'
DTM+137:20260924:102'
DTM+2:20261010:102'
NAD+BY+EURODIS::92++Eurodis Handels GmbH+Hafenstrasse 12+Hamburg+HH+20457+DE'
CUX+2:EUR:9'
LIN+1++PANEL-A4:VP'
QTY+21:12:PCE'
PRI+AAA:89.00'
UNS+S'
CNT+2:1'
UNT+12+1'
UNZ+1+9501'
EDI
run "curl -s -X POST --data-binary @order.edifact $BASE/edi"
curl -s -X POST --data-binary @/tmp/mock-edi-order.edifact \
  -H 'Content-Type: application/edifact' "$BASE/edi"
note "and the ORDRSP it produced:"
curl -s "$BASE/_mock/mailbox?partner=EURODIS&kind=response&raw"

say "10. Trading over a directory instead of over HTTP."
if [ -z "$DROP_DIR" ]; then
  skip "start the mock with --drop-dir and --pickup-dir"
  note "A great deal of real EDI is a folder, not an AS2 connection: drop a"
  note "file in and the answers appear in the other directory, with no HTTP in"
  note "the middle at all."
else
  note "drop:   $DROP_DIR"
  note "pickup: ${PICKUP_DIR:-(none configured)}"
  order_x12 PO-DEMO-4 106 > "$DROP_DIR/order.edi"
  run "curl -s -X POST $BASE/_mock/drop/scan"
  curl -s -X POST "$BASE/_mock/drop/scan" \
    | python3 -c "import json,sys; [print('  %s: %s, produced %s' % (f['name'], 'read' if f['ok'] else f['error'], ', '.join(f['produced']) or '-')) for f in json.load(sys.stdin)['files']]"
  note "the file is moved aside so it is never read twice:"
  ls "$DROP_DIR/processed" | sed 's/^/    processed\//'
  if [ -n "$PICKUP_DIR" ]; then
    note "and the answers were written out (newest first; the tour has filled"
    note "this directory up by now, so only the last few are shown):"
    ls -t "$PICKUP_DIR" | sed -n '1,4p' | sed 's/^/    /'
  fi
  curl -s "$BASE/_mock/mailbox?raw" > /dev/null
fi

say "11. Send it something broken, and read the findings."
cat > /tmp/mock-edi-broken.x12 <<'EDI'
ISA*00*          *00*          *ZZ*ACME           *ZZ*MOCKEDI        *260924*1030*U*00401*000000199*0*T*>~
GS*PO*ACME*MOCKEDI*20260924*1030*199*X*004010~
ST*850*0001~
BEG*ZZ*SA*PO-BROKEN**2026-09-24~
PO1*1*ten*XX*12.50**VP*WIDGET-001~
ZZZ*not a segment~
CTT*1~
SE*99*0001~
GE*1*199~
IEA*1*000000199~
EDI
run "curl -s -X POST --data-binary @broken.x12 $BASE/_mock/validate"
curl -s -X POST --data-binary @/tmp/mock-edi-broken.x12 "$BASE/_mock/validate"

say "12. The dictionary the mock validates against, served as data."
run "curl -s $BASE/_mock/dictionary/X12/850"
curl -s "$BASE/_mock/dictionary/X12/850" | sed -n 1,12p

say "13. Put everything back."
run "curl -s -X POST $BASE/_mock/reset"
curl -s -X POST "$BASE/_mock/reset"

printf '\n\033[1mDone.\033[0m See the README for the rest, and %s/ in a browser.\n' "$BASE"
rm -f /tmp/mock-edi-order.x12 /tmp/mock-edi-order2.x12 /tmp/mock-edi-order3.x12 \
      /tmp/mock-edi-997.x12 /tmp/mock-edi-860.x12 /tmp/mock-edi-order.edifact \
      /tmp/mock-edi-broken.x12
