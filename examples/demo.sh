#!/usr/bin/env bash
#
# A guided tour of mock-edi in curl.
#
#   bash examples/demo.sh                    # against http://127.0.0.1:8080
#   BASE=http://host:9000 bash examples/demo.sh
#
# Start the mock first:  python3 -m mockedi --port 8080
#
# `set -eu` rather than `set -euo pipefail`: several commands here pipe into
# `sed -n 1p`, and under pipefail a SIGPIPE from the reader aborts the script.
set -eu

BASE="${BASE:-http://127.0.0.1:8080}"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }
run() { printf '\n  $ %s\n' "$*"; }

say "0. Is it there?"
run "curl -s $BASE/_mock/health"
curl -s "$BASE/_mock/health"

say "1. Who does it trade with?"
note "Four partners, each configured to misbehave in a different way."
run "curl -s $BASE/_mock/partners"
curl -s "$BASE/_mock/partners" \
  | tr ',' '\n' | grep -E '"(id|dialect|behaviour)"' | paste - - - | sed 's/[" ]//g'

say "2. Send it an 850."
cat > /tmp/mock-edi-order.x12 <<'EDI'
ISA*00*          *00*          *ZZ*ACME           *ZZ*MOCKEDI        *260924*1030*U*00401*000000101*0*T*>~
GS*PO*ACME*MOCKEDI*20260924*1030*101*X*004010~
ST*850*0001~
BEG*00*SA*4500000501**20260924~
CUR*BY*USD~
DTM*002*20261010~
N1*ST*Acme DC 4*92*ACME-DC4~
N3*9 Dock Road~
N4*Columbus*OH*43217*US~
PO1*1*100*EA*12.50**VP*WIDGET-001~
PO1*2*40*EA*4.15**VP*BRKT-050~
CTT*2~
SE*11*0001~
GE*1*101~
IEA*1*000000101~
EDI
run "curl -s -X POST --data-binary @order.x12 $BASE/edi"
curl -s -X POST --data-binary @/tmp/mock-edi-order.x12 \
  -H 'Content-Type: application/edi-x12' "$BASE/edi"

say "3. Four documents came back. Collect them."
note "Collecting empties the mailbox, the way reading one does."
run "curl -s '$BASE/_mock/mailbox?raw'"
curl -s "$BASE/_mock/mailbox?raw"

say "4. The order, as the seller now sees it."
run "curl -s $BASE/_mock/orders/4500000501"
curl -s "$BASE/_mock/orders/4500000501"

say "5. Make the partner short-ship, and send the same order again."
run "curl -s -X PATCH -d '{\"behaviour\":\"short-ship\"}' $BASE/_mock/partners/ACME"
curl -s -X PATCH -H 'Content-Type: application/json' \
  -d '{"behaviour":"short-ship"}' "$BASE/_mock/partners/ACME" \
  | tr ',' '\n' | grep '"behaviour"'
sed 's/4500000501/4500000502/' /tmp/mock-edi-order.x12 > /tmp/mock-edi-order2.x12
curl -s -X POST --data-binary @/tmp/mock-edi-order2.x12 "$BASE/edi" > /dev/null
note "Now the 855 confirms less than was ordered:"
curl -s "$BASE/_mock/mailbox?partner=ACME&kind=response&raw" \
  | grep -E '^(BAK|PO1|ACK)' || true

say "6. AS2, with a receipt."
run "curl -s -X POST --data-binary @order.x12 -H 'AS2-From: ACME' ... $BASE/as2"
sed 's/4500000501/4500000503/' /tmp/mock-edi-order.x12 > /tmp/mock-edi-order3.x12
curl -s -X POST --data-binary @/tmp/mock-edi-order3.x12 \
  -H 'Content-Type: application/edi-x12' \
  -H 'AS2-From: ACME' -H 'AS2-To: MOCKEDI' \
  -H 'Message-ID: <demo-1@acme.example>' \
  -H 'Disposition-Notification-To: edi@acme.example' \
  -H 'Disposition-Notification-Options: signed-receipt-protocol=optional, pkcs7-signature; signed-receipt-micalg=optional, sha256' \
  "$BASE/as2" | grep -E '^(Disposition|Received-Content-MIC|Original-Message-ID):' || true

say "7. An EDIFACT order, to the EDIFACT partner."
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

say "8. Send it something broken, and read the findings."
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

say "9. The dictionary the mock validates against, served as data."
run "curl -s $BASE/_mock/dictionary/X12/850"
curl -s "$BASE/_mock/dictionary/X12/850" | sed -n 1,12p

say "10. Put everything back."
run "curl -s -X POST $BASE/_mock/reset"
curl -s -X POST "$BASE/_mock/reset"

printf '\n\033[1mDone.\033[0m See the README for the rest, and %s/ in a browser.\n' "$BASE"
rm -f /tmp/mock-edi-order.x12 /tmp/mock-edi-order2.x12 /tmp/mock-edi-order3.x12 \
      /tmp/mock-edi-order.edifact /tmp/mock-edi-broken.x12
