- **The mock buys from a supplier** ([#125]). `POST /_mock/purchase` places
  an order with a partner whose `role` is `supplier`: it is stored with a new
  `direction`, `placed` rather than `received`, and an 850 or ORDERS goes out
  through the same queue, delays and delivery as anything else the mock
  sends. `POST /_mock/purchase/<po>/change` changes or cancels it with an 860
  or ORDCHG. The pipeline now takes the mock's side from the partner's role:
  a supplier's 855, 856, 810 and 865 are accepted, acknowledged and filed
  under the order they name, listed under `filed` in the receipt, and shown in
  the order's timeline after the order going out. One naming an order the
  mock never placed with that supplier is rejected at the order number's
  element in the 997 or CONTRL. Nothing about a customer has changed.
  **The schema version is now 9**; a file database is upgraded in place, and
  every order it held was received.
