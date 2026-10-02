"""A body of the wrong shape is the client's mistake: 400, and the field named (#207).

A JSON `null`, list or number where the mock wanted a string used to reach
whatever first tried to use it as one, and came back as a 500 with the name
of a Python exception - `'int' object has no attribute 'strip'` - or, worse,
was stored as its `repr`: an order numbered `{'a': 1}`.

Every case below says which field, what it was given and what it wanted, and
leaves nothing behind.
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from support import ACME, MockServerCase

NORTHWIND = "NORTHWIND"
LINES = [{"sku": "WIDGET-001", "quantity": "1", "price": "1.00"}]


class BadBodyCase(MockServerCase):
    def assertRefused(self, method, path, body, *fragments):
        status, _h, data = self.request(method, path, body)
        self.assertEqual(status, 400, data)
        text = data["error"] + " " + " ".join(data.get("problems", []))
        for fragment in fragments:
            self.assertIn(fragment, text)
        return data


class APartner(BadBodyCase):
    def test_a_field_that_is_null(self):
        before = self.get("/_mock/partners/" + ACME)[2]
        for field in ("as2_url", "name", "behaviour", "qualifier"):
            with self.subTest(field=field):
                self.assertRefused("PATCH", "/_mock/partners/" + ACME,
                                   {field: None}, field, "a string", "null")
        self.assertEqual(self.get("/_mock/partners/" + ACME)[2], before)

    def test_a_field_that_is_a_list_an_object_or_a_number(self):
        for value, kind in ((["short-ship"], "a list"), ({"a": 1}, "an object"),
                            (7, "a number"), (True, "true or false")):
            with self.subTest(value=value):
                self.assertRefused("PATCH", "/_mock/partners/" + ACME,
                                   {"behaviour": value}, "behaviour",
                                   "a string", kind)

    def test_an_id_that_is_not_a_string(self):
        for value, kind in ((12345, "a number"), (["a"], "a list"),
                            (None, "null")):
            with self.subTest(value=value):
                self.assertRefused("POST", "/_mock/partners",
                                   {"id": value, "name": "Numeric"},
                                   "id", "a string", kind)
        ids = [row["id"] for row in self.get("/_mock/partners")[2]]
        self.assertNotIn("12345", ids)

    def test_a_name_that_is_not_a_string(self):
        self.assertRefused("POST", "/_mock/partners", {"id": "NEWCO", "name": 7},
                           "name", "a string", "a number")
        self.assertEqual(self.get("/_mock/partners/NEWCO")[0], 404)

    def test_the_flag_still_takes_what_a_flag_is(self):
        for value in (True, False, 0, 1):
            status, _h, data = self.request("PATCH", "/_mock/partners/" + ACME,
                                            {"test": value})
            self.assertEqual(status, 200, data)


class APurchase(BadBodyCase):
    def placed(self):
        status, _h, data = self.post("/_mock/purchase",
                                     {"partner": NORTHWIND, "lines": LINES})
        self.assertEqual(status, 201, data)
        return data["po_number"]

    def test_a_po_number_that_is_not_a_string(self):
        before = self.get("/_mock/orders")[2]
        for value, kind in ((["a"], "a list"), ({"a": 1}, "an object"),
                            (None, "null")):
            with self.subTest(value=value):
                self.assertRefused("POST", "/_mock/purchase",
                                   {"partner": NORTHWIND, "po_number": value,
                                    "lines": LINES},
                                   "po_number", "a string", kind)
        self.assertEqual(self.get("/_mock/orders")[2], before)

    def test_a_currency_or_a_date_that_is_not_a_string(self):
        for field in ("currency", "requested_on"):
            with self.subTest(field=field):
                self.assertRefused("POST", "/_mock/purchase",
                                   {"partner": NORTHWIND, field: ["x"],
                                    "lines": LINES}, field, "a string", "a list")

    def test_a_partner_that_is_not_a_string(self):
        self.assertRefused("POST", "/_mock/purchase",
                           {"partner": [NORTHWIND], "lines": LINES},
                           "partner", "a string", "a list")

    def test_a_changed_line_that_is_not_an_object(self):
        po_number = self.placed()
        before = self.get("/_mock/orders/" + po_number)[2]
        for value, kind in ((1, "a number"), (None, "null"), ("1", "a string")):
            with self.subTest(value=value):
                self.assertRefused("POST", "/_mock/purchase/%s/change" % po_number,
                                   {"lines": [value]}, "line 1", "an object", kind)
        self.assertEqual(self.get("/_mock/orders/" + po_number)[2], before)

    def test_cancel_is_true_or_false(self):
        # "no" is a string, and a string is truthy: it cancelled the order.
        po_number = self.placed()
        self.assertRefused("POST", "/_mock/purchase/%s/change" % po_number,
                           {"cancel": "no"}, "cancel", "true or false", "a string")
        self.assertEqual(self.get("/_mock/orders/" + po_number)[2]["status"],
                         "placed")

    def test_quantities_and_prices_may_still_be_numbers(self):
        status, _h, data = self.post("/_mock/purchase", {
            "partner": NORTHWIND,
            "lines": [{"sku": "WIDGET-001", "quantity": 3, "price": 1.5}]})
        self.assertEqual(status, 201, data)
        self.assertEqual((data["lines"][0]["quantity"], data["lines"][0]["price"]),
                         ("3", "1.50"))


class ASend(BadBodyCase):
    def test_a_partner_that_is_not_a_string(self):
        self.assertRefused("POST", "/_mock/send", {"partner": [ACME]},
                           "partner", "a string", "a list")

    def test_a_delay_that_is_not_a_whole_number(self):
        for value, kind in (("soon", "a string"), (1.5, "a number"),
                            ([1], "a list")):
            with self.subTest(value=value):
                self.assertRefused("POST", "/_mock/send",
                                   {"partner": ACME, "kind": "response",
                                    "order": "X", "delayMs": value},
                                   "delayMs", "a whole number", kind)


class ABodyThatIsNotAnObject(BadBodyCase):
    PATHS = (("POST", "/_mock/partners"), ("PATCH", "/_mock/partners/" + ACME),
             ("POST", "/_mock/purchase"), ("POST", "/_mock/send"))

    def test_a_list(self):
        for method, path in self.PATHS:
            with self.subTest(path=path):
                self.assertRefused(method, path, [1, 2], "a JSON object", "a list")

    def test_something_that_is_not_json(self):
        for method, path in self.PATHS:
            with self.subTest(path=path):
                status, _h, data = self.request(method, path, "{not json")
                self.assertEqual(status, 400, data)
                self.assertIn("not JSON", data["error"])

    def test_no_body_at_all_is_an_empty_object_as_before(self):
        status, _h, data = self.request("PATCH", "/_mock/partners/" + ACME, None)
        self.assertEqual(status, 200, data)

    def test_none_of_them_is_a_500(self):
        bodies = ([1], {"id": 1}, {"partner": 1}, {"lines": [1]}, {"po_number": []},
                  {"as2_url": None}, "x", None, {"delayMs": {}})
        for method, path in self.PATHS + (("POST", "/_mock/purchase/NOPE/change"),):
            for body in bodies:
                with self.subTest(path=path, body=body):
                    status, _h, data = self.request(method, path, body)
                    self.assertLess(status, 500, data)


if __name__ == "__main__":
    unittest.main()
