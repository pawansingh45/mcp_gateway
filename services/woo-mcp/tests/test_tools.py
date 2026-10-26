"""Tests run the real MCP app against a fake WooCommerce REST API (Flask test client
standing in for the `requests` transport), so we verify the real request shapes."""
import json
import unittest
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

from woo_mcp.client import WooClient
from woo_mcp.tools import build_app, start_of_today


class FakeWoo:
    """Records calls and mimics the bits of wc/v3 we rely on."""

    def __init__(self):
        self.app = Flask("fake-woo")
        self.calls = []
        self.products = []
        self.orders = [{"id": 7, "number": "7", "status": "processing", "total": "20.00", "currency": "USD",
                        "date_created": "2026-10-24T10:00:00", "payment_method_title": "Cash on delivery",
                        "billing": {"first_name": "Asha", "last_name": "K", "email": "a@example.com"},
                        "line_items": [{"product_id": 1, "name": "Mug", "quantity": 2, "total": "20.00"}]}]
        app = self.app

        @app.before_request
        def record():
            self.calls.append((request.method, request.path, dict(request.args), request.get_json(silent=True),
                               request.authorization.username if request.authorization else None,
                               request.headers.get("X-Forwarded-Proto")))

        @app.get("/wp-json/wc/v3/products")
        def lp():
            return jsonify(self.products)

        @app.post("/wp-json/wc/v3/products")
        def cp():
            body = request.get_json()
            p = {"id": len(self.products) + 1, "permalink": "http://shop/p", "price": body["regular_price"], **body}
            self.products.append(p)
            return jsonify(p), 201

        @app.get("/wp-json/wc/v3/orders")
        def lo():
            return jsonify(self.orders)

        @app.get("/wp-json/wc/v3/orders/<int:oid>")
        def go(oid):
            for o in self.orders:
                if o["id"] == oid:
                    return jsonify(o)
            return jsonify({"code": "woocommerce_rest_shop_order_invalid_id", "message": "Invalid ID."}), 404

        @app.put("/wp-json/wc/v3/orders/<int:oid>")
        def uo(oid):
            self.orders[0]["status"] = request.get_json()["status"]
            return jsonify(self.orders[0])


class FlaskAdapter(requests.adapters.BaseAdapter):
    """Route `requests` calls into a Flask app in-process."""

    def __init__(self, flask_app):
        super().__init__()
        self.client = flask_app.test_client()

    def send(self, req, **kwargs):
        from urllib.parse import urlparse
        u = urlparse(req.url)
        r = self.client.open(u.path + ("?" + u.query if u.query else ""), method=req.method,
                             data=req.body, headers=dict(req.headers))
        resp = requests.Response()
        resp.status_code = r.status_code
        resp._content = r.data
        resp.headers.update(r.headers)
        resp.url = req.url
        resp.reason = "ERR" if r.status_code >= 400 else "OK"
        return resp

    def close(self):
        pass


def make(tz="UTC"):
    fake = FakeWoo()
    session = requests.Session()
    session.mount("http://store", FlaskAdapter(fake.app))
    client = WooClient("http://store", "ck_test", "cs_test", session=session)
    return fake, build_app(client, tz)


def call(app, name, args):
    r = app.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": name, "arguments": args}})
    return r["result"], json.loads(r["result"]["content"][0]["text"]) if not r["result"].get("isError") else None


class WooToolTests(unittest.TestCase):
    def test_exposes_the_required_tools(self):
        _, app = make()
        self.assertTrue({"list_products", "create_product", "list_orders", "get_order",
                         "update_order_status"} <= set(app.tools))

    def test_create_product_sends_correct_payload_and_auth(self):
        fake, app = make()
        _, out = call(app, "create_product", {"name": "Blue Mug", "regular_price": "12.50"})
        self.assertEqual(out["name"], "Blue Mug")
        method, path, _, body, user, proto = fake.calls[-1]
        self.assertEqual((method, path), ("POST", "/wp-json/wc/v3/products"))
        self.assertEqual(body["regular_price"], "12.50")
        self.assertEqual(body["status"], "publish")
        self.assertEqual(user, "ck_test")
        self.assertEqual(proto, "https")

    def test_create_product_rejects_bad_price_before_calling_store(self):
        fake, app = make()
        result, _ = call(app, "create_product", {"name": "x", "regular_price": "free"})
        self.assertTrue(result["isError"])
        self.assertEqual(fake.calls, [])

    def test_list_orders_created_today_uses_store_timezone(self):
        fake, app = make("Asia/Kolkata")
        call(app, "list_orders", {"created_today": True})
        params = fake.calls[-1][2]
        self.assertTrue(params["after"].endswith("+05:30"))
        self.assertIn("T00:00:00", params["after"])
        self.assertEqual(params["orderby"], "date")

    def test_list_orders_projects_only_useful_fields(self):
        _, app = make()
        _, out = call(app, "list_orders", {})
        order = out["orders"][0]
        self.assertEqual(order["customer"], "Asha K")
        self.assertEqual(order["items"][0]["quantity"], 2)
        self.assertNotIn("billing", order)

    def test_get_order_not_found_is_a_clean_tool_error(self):
        _, app = make()
        result, _ = call(app, "get_order", {"id": 999})
        self.assertTrue(result["isError"])
        self.assertIn("Not found", result["content"][0]["text"])

    def test_update_order_status_validates_enum(self):
        _, app = make()
        result, _ = call(app, "update_order_status", {"id": 7, "status": "banana"})
        self.assertTrue(result["isError"])
        _, out = call(app, "update_order_status", {"id": 7, "status": "completed"})
        self.assertEqual(out["status"], "completed")

    def test_unreachable_store_gives_clear_error(self):
        client = WooClient("http://127.0.0.1:9", "k", "s", timeout=0.5)
        app = build_app(client)
        result, _ = call(app, "list_products", {})
        self.assertTrue(result["isError"])
        self.assertIn("unreachable", result["content"][0]["text"])

    def test_start_of_today_is_midnight_in_zone(self):
        now = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)  # 01:30 on the 25th in Kolkata
        self.assertEqual(start_of_today("Asia/Kolkata", now).isoformat(), "2026-10-25T00:00:00+05:30")


if __name__ == "__main__":
    unittest.main()
