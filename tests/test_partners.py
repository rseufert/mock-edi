"""The partner control plane refuses what it cannot act on.

"Refuse rather than half-implement" applies to the control plane as much as
to the wire. A PATCH that accepts a misspelled field and answers 200 has not
been polite; it has sent whoever wrote it looking for the bug somewhere else
entirely, and the mock is the one place in their stack that was supposed to
be predictable.

Every test here is a negative one, paired where it matters with the positive
case that proves the rule is not simply refusing everything.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, EURODIS, MockServerCase, x12_order


class PartnerCase(MockServerCase):
    def patch_partner(self, identifier, body):
        return self.patch("/_mock/partners/" + identifier, body)

    def create_partner(self, body):
        return self.post("/_mock/partners", body)

    def assertRefused(self, status, data, *expected):
        self.assertEqual(status, 400, data)
        for fragment in expected:
            self.assertIn(fragment, data["error"])


class UnknownFields(PartnerCase):
    def test_a_misspelled_field_is_refused_not_dropped(self):
        status, _h, data = self.patch_partner(ACME, {"behavior": "short-ship"})
        self.assertRefused(status, data, "'behavior'", "behaviour")

    def test_the_error_lists_the_fields_a_partner_actually_has(self):
        _s, _h, data = self.patch_partner(ACME, {"nonsense": 1})
        for field in ("dialect", "version", "as2_url", "mdn_mode", "test"):
            self.assertIn(field, data["error"])

    def test_several_unknown_fields_are_all_named(self):
        _s, _h, data = self.patch_partner(ACME, {"foo": 1, "bar": 2})
        self.assertIn("'bar'", data["error"])
        self.assertIn("'foo'", data["error"])

    def test_it_applies_on_create_too(self):
        status, _h, data = self.create_partner({"id": "NEWCO", "behavior": "accept"})
        self.assertRefused(status, data, "'behavior'")

    def test_and_the_partner_is_unchanged(self):
        self.patch_partner(ACME, {"behavior": "short-ship"})
        _s, _h, row = self.get("/_mock/partners/" + ACME)
        self.assertEqual(row["behaviour"], "accept")


class TheVersionField(PartnerCase):
    def test_an_x12_partner_needs_six_digits(self):
        status, _h, data = self.patch_partner(ACME, {"version": "5010"})
        self.assertRefused(status, data, "004010")

    def test_004010_and_005010_are_both_fine(self):
        for version in ("004010", "005010"):
            status, _h, row = self.patch_partner(ACME, {"version": version})
            self.assertEqual(status, 200, row)
            self.assertEqual(row["version"], version)

    def test_an_edifact_directory_on_an_x12_partner_is_refused(self):
        status, _h, data = self.patch_partner(ACME, {"version": "D:96A:UN"})
        self.assertRefused(status, data, "six digits")

    def test_an_edifact_partner_needs_a_directory(self):
        status, _h, data = self.patch_partner(EURODIS, {"version": "004010"})
        self.assertRefused(status, data, "D:96A:UN")

    def test_the_directory_the_dictionary_has_is_accepted(self):
        status, _h, row = self.patch_partner(EURODIS, {"version": "D:96A:UN"})
        self.assertEqual(status, 200, row)
        self.assertEqual(row["version"], "D:96A:UN")

    def test_a_well_formed_version_the_dictionary_lacks_is_refused(self):
        # The mock would put it in every UNH and GS08 it wrote, and then fill
        # them with documents shaped for another version.
        status, _h, data = self.patch_partner(EURODIS, {"version": "D:01B:UN"})
        self.assertRefused(status, data, "no dictionary", "D:96A:UN")
        status, _h, data = self.patch_partner(ACME, {"version": "004020"})
        self.assertRefused(status, data, "no dictionary", "004010 and 005010")


class TheTestFlag(PartnerCase):
    def test_prose_is_refused(self):
        status, _h, data = self.patch_partner(ACME, {"test": "maybe"})
        self.assertRefused(status, data, "flag", "0 or 1")

    def test_a_number_outside_the_flag_is_refused(self):
        status, _h, data = self.patch_partner(ACME, {"test": 2})
        self.assertRefused(status, data, "flag")

    def test_zero_and_one_are_kept(self):
        for value, expected in ((0, 0), (1, 1), ("0", 0), ("1", 1)):
            _s, _h, row = self.patch_partner(ACME, {"test": value})
            self.assertEqual(row["test"], expected)

    def test_a_json_boolean_is_normalised(self):
        _s, _h, row = self.patch_partner(ACME, {"test": False})
        self.assertEqual(row["test"], 0)
        _s, _h, row = self.patch_partner(ACME, {"test": True})
        self.assertEqual(row["test"], 1)

    def test_the_flag_reaches_the_wire(self):
        self.patch_partner(ACME, {"test": 1})
        self.send(x12_order("PO-FLAG"))
        payload = self.mailbox(ACME)[0]["payload"]
        self.assertIn("*T*", payload.split("~")[0])


class TheMdnMode(PartnerCase):
    def test_an_unknown_mode_is_refused_with_the_known_ones(self):
        status, _h, data = self.patch_partner(ACME, {"mdn_mode": "carrier pigeon"})
        self.assertRefused(status, data, "sync", "async")

    def test_both_modes_are_accepted(self):
        for mode in ("sync", "async"):
            _s, _h, row = self.patch_partner(ACME, {"mdn_mode": mode})
            self.assertEqual(row["mdn_mode"], mode)


class TheAs2Url(PartnerCase):
    def test_something_that_is_not_a_url_is_refused(self):
        status, _h, data = self.patch_partner(ACME, {"as2_url": "localhost:9000"})
        self.assertRefused(status, data, "http")

    def test_a_scheme_the_courier_cannot_use_is_refused(self):
        status, _h, data = self.patch_partner(ACME, {"as2_url": "ftp://host/as2"})
        self.assertRefused(status, data, "http or https")

    def test_a_url_with_no_host_is_refused(self):
        status, _h, data = self.patch_partner(ACME, {"as2_url": "http:///as2"})
        self.assertRefused(status, data, "host")

    def test_http_and_https_are_accepted(self):
        for url in ("http://localhost:9000/as2", "https://partner.example/as2"):
            _s, _h, row = self.patch_partner(ACME, {"as2_url": url})
            self.assertEqual(row["as2_url"], url)

    def test_it_can_be_cleared_back_to_a_mailbox_partner(self):
        self.patch_partner(ACME, {"as2_url": "http://localhost:9000/as2"})
        _s, _h, row = self.patch_partner(ACME, {"as2_url": ""})
        self.assertEqual(row["as2_url"], "")


class TheQualifier(PartnerCase):
    def test_x12_refuses_more_than_two_characters(self):
        status, _h, data = self.patch_partner(ACME, {"qualifier": "ZZZ"})
        self.assertRefused(status, data, "at most 2")

    def test_two_characters_are_fine(self):
        _s, _h, row = self.patch_partner(ACME, {"qualifier": "01"})
        self.assertEqual(row["qualifier"], "01")

    def test_edifact_allows_four(self):
        _s, _h, row = self.patch_partner(EURODIS, {"qualifier": "ZZZZ"})
        self.assertEqual(row["qualifier"], "ZZZZ")

    def test_but_not_five(self):
        status, _h, data = self.patch_partner(EURODIS, {"qualifier": "ZZZZZ"})
        self.assertRefused(status, data, "at most 4")


class TheIdentifier(PartnerCase):
    """ISA06 is fixed at fifteen characters; a longer id cannot come back."""

    def test_an_id_too_long_for_x12_is_refused(self):
        status, _h, data = self.create_partner({"id": "AVERYLONGPARTNERID"})
        self.assertRefused(status, data, "15", "truncated")

    def test_fifteen_characters_are_accepted_and_reachable(self):
        identifier = "PARTNER12345678"          # exactly 15
        status, _h, _row = self.create_partner({"id": identifier})
        self.assertEqual(status, 201)
        summary = self.send(x12_order("PO-LONGID", sender=identifier))
        self.assertTrue(summary["accepted"], summary)
        self.assertEqual(summary["partner"], identifier)

    def test_edifact_allows_thirty_five(self):
        identifier = "E" * 35
        status, _h, _row = self.create_partner({"id": identifier,
                                                "dialect": "EDIFACT",
                                                "version": "D:96A:UN"})
        self.assertEqual(status, 201)

    def test_but_not_thirty_six(self):
        status, _h, data = self.create_partner({"id": "E" * 36,
                                                "dialect": "EDIFACT",
                                                "version": "D:96A:UN"})
        self.assertRefused(status, data, "35")

    def test_an_empty_id_is_refused(self):
        status, _h, data = self.create_partner({"id": "   "})
        self.assertEqual(status, 400, data)

    def test_an_id_that_would_leave_the_pickup_directory_is_refused(self):
        for identifier in ("../../trav", "A/B", "A\\B", ".HIDDEN"):
            status, _h, data = self.create_partner({"id": identifier})
            self.assertRefused(status, data, "letters, digits")

    def test_an_id_with_a_space_or_a_delimiter_is_refused(self):
        for identifier in ("TWO WORDS", "STAR*CO", "TILDE~CO", "PLUS+CO"):
            status, _h, data = self.create_partner({"id": identifier})
            self.assertEqual(status, 400, (identifier, data))

    def test_letters_digits_and_dot_dash_underscore_are_accepted(self):
        status, _h, data = self.create_partner({"id": "Acme-2.EU_1"})
        self.assertEqual(status, 201, data)

    def test_moving_a_long_id_to_x12_is_refused_rather_than_broken(self):
        identifier = "E" * 30
        self.create_partner({"id": identifier, "dialect": "EDIFACT",
                             "version": "D:96A:UN"})
        status, _h, data = self.patch_partner(identifier,
                                              {"dialect": "X12",
                                               "version": "004010"})
        self.assertRefused(status, data, "15")


class SendingSomebodyElsesOrder(PartnerCase):
    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-OWNED"))
        self.mailbox(ACME, leave=False)

    def test_an_order_cannot_be_sent_to_a_partner_it_does_not_belong_to(self):
        status, _h, data = self.post("/_mock/send",
                                     {"partner": EURODIS, "kind": "invoice",
                                      "order": "PO-OWNED"})
        self.assertEqual(status, 400, data)
        self.assertIn("EURODIS has no purchase order 'PO-OWNED'; ACME does",
                      data["error"])

    def test_and_nothing_is_queued_for_them(self):
        self.post("/_mock/send", {"partner": EURODIS, "kind": "invoice",
                                  "order": "PO-OWNED"})
        self.assertEqual(self.mailbox(EURODIS), [])

    def test_its_own_partner_can_still_be_sent_it(self):
        status, _h, data = self.post("/_mock/send",
                                     {"partner": ACME, "kind": "invoice",
                                      "order": "PO-OWNED"})
        self.assertEqual(status, 201, data)
        self.assertEqual([r["code"] for r in self.mailbox(ACME)], ["810"])


class DeletingAPartnerWithWorkOutstanding(PartnerCase):
    config_kwargs = {"invoice_delay_ms": 3600000}

    def setUp(self):
        super().setUp()
        self.send(x12_order("PO-LEAVING"))

    def test_documents_waiting_for_them_are_cancelled(self):
        _s, _h, outcome = self.request("DELETE", "/_mock/partners/" + ACME)
        self.assertTrue(outcome["deleted"])
        self.assertGreater(outcome["cancelled"], 0)
        self.assertEqual(self.mailbox(ACME), [])

    def test_the_cancellation_says_why(self):
        self.request("DELETE", "/_mock/partners/" + ACME)
        _s, _h, rows = self.get("/_mock/outbox")
        cancelled = [r for r in rows if r["status"] == "cancelled"]
        self.assertTrue(cancelled)
        self.assertTrue(all(r["note"] == "partner deleted" for r in cancelled))

    def test_promised_work_is_marked_off_with_a_reason(self):
        _s, _h, outcome = self.request("DELETE", "/_mock/partners/" + ACME)
        self.assertGreater(outcome["unscheduled"], 0)
        _s, _h, rows = self.get("/_mock/scheduled")
        self.assertEqual(rows, [], "nothing should still be promised")
        # Only what was still outstanding carries the reason. The despatch had
        # already been packed on its own account, and rewriting its note would
        # be rewriting history.
        _s, _h, rows = self.get("/_mock/scheduled?all")
        by_kind = {r["kind"]: r for r in rows}
        self.assertEqual(by_kind["invoice"]["note"], "partner deleted")
        self.assertEqual(by_kind["despatch"]["note"], "")

    def test_nothing_is_packed_for_them_afterwards(self):
        self.request("DELETE", "/_mock/partners/" + ACME)
        self.post("/_mock/advance?all")
        self.assertEqual(self.mailbox(ACME), [])

    def test_the_evidence_of_what_happened_is_kept(self):
        self.request("DELETE", "/_mock/partners/" + ACME)
        _s, _h, rows = self.get("/_mock/documents?partner=" + ACME)
        self.assertTrue(rows, "the archive should outlive the partner")

    def test_deleting_a_partner_that_is_not_there_is_a_404(self):
        status, _h, outcome = self.request("DELETE", "/_mock/partners/NOBODY")
        self.assertEqual(status, 404)
        self.assertFalse(outcome["deleted"])


class TheRole(PartnerCase):
    """The mock sells to a customer and buys from a supplier (#125)."""

    def test_every_partner_there_was_is_a_customer(self):
        for pid in (ACME, EURODIS):
            self.assertEqual(self.get("/_mock/partners/" + pid)[2]["role"],
                             "customer")

    def test_one_supplier_is_seeded(self):
        _s, _h, rows = self.get("/_mock/partners")
        self.assertEqual([r["id"] for r in rows if r["role"] == "supplier"],
                         ["NORTHWIND"])

    def test_a_new_partner_is_a_customer_unless_told_otherwise(self):
        status, _h, data = self.create_partner({"id": "NEWCO"})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["role"], "customer")

    def test_a_supplier_can_be_created(self):
        status, _h, data = self.create_partner({"id": "PARTS", "role": "supplier"})
        self.assertEqual(status, 201, data)
        self.assertEqual(data["role"], "supplier")

    def test_an_unknown_role_is_refused_with_the_known_ones(self):
        status, _h, data = self.patch_partner(ACME, {"role": "reseller"})
        self.assertRefused(status, data, "'reseller'", "customer", "supplier")

    def test_the_mocks_word_for_itself_is_not_a_partner_role(self):
        # "buyer" and "seller" are the mock's side; a partner is a customer or
        # a supplier, so the two vocabularies can never be confused.
        for word in ("buyer", "seller"):
            status, _h, data = self.patch_partner(ACME, {"role": word})
            self.assertRefused(status, data, repr(word))

    def test_the_role_can_be_changed(self):
        status, _h, data = self.patch_partner(ACME, {"role": "supplier"})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["role"], "supplier")


class TheOneConversion(unittest.TestCase):
    """The partner's role and the mock's are turned into each other in one place."""

    def test_the_mock_sells_to_a_customer_and_buys_from_a_supplier(self):
        from mockedi import partners, schema
        self.assertEqual(partners.mock_role({"role": partners.CUSTOMER}),
                         schema.SELLER)
        self.assertEqual(partners.mock_role({"role": partners.SUPPLIER}),
                         schema.BUYER)

    def test_the_two_vocabularies_share_no_word(self):
        from mockedi import partners, schema
        self.assertFalse(set(partners.ROLES) & set(schema.RECEIVED_BY))
        with self.assertRaises(KeyError):
            partners.mock_role({"role": schema.BUYER})


class ABehaviourFitsTheRole(PartnerCase):
    """A seller-only behaviour on a supplier would be stored and never happen."""

    SELLER_ONLY = ("short-ship", "reject-line", "reject-all",
                   "duplicate-invoice", "out-of-order", "no-invoice")
    EITHER = ("accept", "no-ack", "late", "reject-ack", "strict", "corrupt")

    def test_each_seller_only_behaviour_is_refused_on_a_supplier(self):
        for behaviour in self.SELLER_ONLY:
            with self.subTest(behaviour=behaviour):
                status, _h, data = self.patch_partner(
                    "NORTHWIND", {"behaviour": behaviour})
                self.assertRefused(status, data, repr(behaviour), "supplier")
        self.assertEqual(self.get("/_mock/partners/NORTHWIND")[2]["behaviour"],
                         "accept")

    def test_the_refusal_says_what_a_supplier_may_be(self):
        status, _h, data = self.patch_partner("NORTHWIND", {"behaviour": "short-ship"})
        self.assertRefused(status, data, "accept", "no-ack")

    def test_the_rest_are_allowed(self):
        for behaviour in self.EITHER:
            with self.subTest(behaviour=behaviour):
                status, _h, data = self.patch_partner(
                    "NORTHWIND", {"behaviour": behaviour})
                self.assertEqual(status, 200, data)

    def test_it_applies_on_create(self):
        status, _h, data = self.create_partner(
            {"id": "PARTS", "role": "supplier", "behaviour": "reject-all"})
        self.assertRefused(status, data, "'reject-all'")
        self.assertEqual(self.get("/_mock/partners/PARTS")[0], 404)

    def test_changing_the_role_is_checked_against_the_behaviour_held(self):
        # GLOBEX short-ships; making it a supplier would strand the behaviour.
        status, _h, data = self.patch_partner("GLOBEX", {"role": "supplier"})
        self.assertRefused(status, data, "'short-ship'")
        self.assertEqual(self.get("/_mock/partners/GLOBEX")[2]["role"],
                         "customer")

    def test_both_changed_together_is_fine(self):
        status, _h, data = self.patch_partner(
            "GLOBEX", {"role": "supplier", "behaviour": "accept"})
        self.assertEqual(status, 200, data)

    def test_a_customer_may_have_any_of_them(self):
        for behaviour in self.SELLER_ONLY + self.EITHER:
            with self.subTest(behaviour=behaviour):
                status, _h, data = self.patch_partner(ACME, {"behaviour": behaviour})
                self.assertEqual(status, 200, data)


class EachBehaviourSaysWhichRolesItFits(PartnerCase):
    """db.BEHAVIOUR_ROLES is the one record; the refusal and the docs read it (#127)."""

    def test_every_behaviour_has_roles_and_nothing_else_does(self):
        from mockedi import db, partners
        self.assertEqual(set(db.BEHAVIOUR_ROLES), set(db.BEHAVIOURS))
        for name, roles in db.BEHAVIOUR_ROLES.items():
            self.assertTrue(roles, name)
            self.assertLessEqual(set(roles), set(partners.ROLES), name)

    def test_the_ones_that_describe_how_the_mock_answers_fit_both(self):
        from mockedi import db
        for name in ("accept", "no-ack", "late", "reject-ack", "strict", "corrupt"):
            self.assertEqual(set(db.BEHAVIOUR_ROLES[name]),
                             {"customer", "supplier"}, name)

    def test_each_of_them_can_be_set_on_the_seeded_supplier(self):
        for name in ("accept", "no-ack", "late", "reject-ack", "strict", "corrupt"):
            with self.subTest(behaviour=name):
                status, _h, data = self.patch_partner("NORTHWIND", {"behaviour": name})
                self.assertEqual(status, 200, data)
                self.assertEqual(data["behaviour"], name)

    def test_a_supplier_only_behaviour_would_be_refused_on_a_customer(self):
        # None exists yet - #127's buyer behaviours are next - so one is added
        # for the length of this test, to show the rule runs both ways.
        from mockedi import db
        db.BEHAVIOURS["duplicate-order"] = "Send the 850 twice."
        db.BEHAVIOUR_ROLES["duplicate-order"] = ("supplier",)
        try:
            status, _h, data = self.patch_partner(ACME, {"behaviour": "duplicate-order"})
            self.assertRefused(status, data, "'duplicate-order'",
                               "sells to a customer", "A customer may be")
            status, _h, data = self.patch_partner(
                "NORTHWIND", {"behaviour": "duplicate-order"})
            self.assertEqual(status, 200, data)
        finally:
            self.patch_partner("NORTHWIND", {"behaviour": "accept"})
            del db.BEHAVIOURS["duplicate-order"]
            del db.BEHAVIOUR_ROLES["duplicate-order"]

    def test_help_names_the_roles(self):
        from mockedi.__main__ import build_parser
        epilog = build_parser().epilog
        self.assertIn("short-ship         [customer]", epilog)
        self.assertIn("no-ack             [customer, supplier]", epilog)

    def test_the_index_page_lists_them_with_their_roles(self):
        _s, _h, body = self.get("/", raw=True)
        page = body.decode()
        self.assertIn("<h2>Behaviours</h2>", page)
        self.assertIn("<td><code>short-ship</code></td><td>customer</td>", page)
        self.assertIn("<td><code>late</code></td><td>customer or supplier</td>", page)


class TheIndexPage(PartnerCase):
    def test_it_shows_each_partners_role(self):
        _s, _h, body = self.get("/", raw=True)
        page = body.decode()
        self.assertIn("<th>Role</th>", page)
        self.assertIn("<td><code>NORTHWIND</code></td><td>Northwind Components Ltd"
                      "</td><td>supplier</td>", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
