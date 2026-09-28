- **When a remittance arrives, and whether it was taken back** ([#149]). An
  820 whose `BPR16` says the payment takes effect after the advice arrived
  carries a `remitted-before-settlement` disagreement: the payee is being told
  to reconcile cash that has not arrived. It is judged by the mock's clock,
  so `/_mock/advance` moves it. An 820 with `BPR03 = D` reverses the earlier
  advice with the same `TRN02` trace - the correction a payer owes once the
  bank returns the payment - and the new `GET /_mock/remittances` lists every
  advice received with its trace, total, invoices and settlement date, the
  reversed one marked `reversed` and naming what reversed it. A debit for a
  trace never advised is a `reversal-of-nothing`. Both read the 820 only.
