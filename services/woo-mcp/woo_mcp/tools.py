"""WooCommerce tools exposed over MCP."""
from __future__ import annotations

import os
import re
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from mcpkit import McpApp, ToolError

from .client import WooClient, WooError

ORDER_STATUSES = ["pending", "processing", "on-hold", "completed", "cancelled", "refunded", "failed", "trash"]
_PRICE_RE = re.compile(r"^\d+(\.\d{1,2})?$")


def _product_view(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "status": p.get("status"),
        "price": p.get("price"),
        "regular_price": p.get("regular_price"),
        "sku": p.get("sku") or None,
        "stock_status": p.get("stock_status"),
        "url": p.get("permalink"),
    }


def _order_view(o: dict) -> dict:
    billing = o.get("billing") or {}
    return {
        "id": o.get("id"),
        "number": o.get("number"),
        "status": o.get("status"),
        "created": o.get("date_created"),  # store-local time
        "total": o.get("total"),
        "currency": o.get("currency"),
        "payment_method": o.get("payment_method_title") or o.get("payment_method"),
        "customer": (f"{billing.get('first_name', '')} {billing.get('last_name', '')}".strip() or None),
        "customer_email": billing.get("email") or None,
        "items": [
            {"product_id": li.get("product_id"), "name": li.get("name"),
             "quantity": li.get("quantity"), "total": li.get("total")}
            for li in o.get("line_items", [])
        ],
    }


def start_of_today(tz_name: str, now: datetime | None = None) -> datetime:
    tz = ZoneInfo(tz_name)
    now = now.astimezone(tz) if now else datetime.now(tz)
    return datetime.combine(now.date(), time.min, tzinfo=tz)


def build_app(client: WooClient, store_timezone: str = "UTC", auth_token: str | None = None) -> McpApp:
    app = McpApp(
        name="woocommerce-mcp",
        version="1.0.0",
        auth_token=auth_token,
        instructions="Tools for reading and managing one WooCommerce store: products and orders.",
    )
    paging = {
        "per_page": {"type": "integer", "minimum": 1, "maximum": 50, "description": "Results per page (default 20)."},
        "page": {"type": "integer", "minimum": 1, "description": "Page number (default 1)."},
    }

    def guarded(fn):
        def wrapper(args):
            try:
                return fn(args)
            except WooError as exc:
                raise ToolError(str(exc)) from exc
        return wrapper

    @app.tool("list_products", "List products in the store, optionally filtered by search text or status.",
              {"properties": {"search": {"type": "string", "description": "Match against product name/description."},
                              "status": {"type": "string", "enum": ["publish", "draft", "pending", "private", "any"]},
                              **paging}}, read_only=True)
    @guarded
    def list_products(a):
        rows = client.get("products", search=a.get("search"), status=a.get("status"),
                          per_page=a.get("per_page", 20), page=a.get("page", 1))
        return {"count": len(rows), "products": [_product_view(p) for p in rows]}

    @app.tool("create_product",
              "Create a simple product. It is published immediately unless status is 'draft'.",
              {"properties": {"name": {"type": "string", "description": "Product title."},
                              "regular_price": {"type": "string", "description": "Price as a decimal string, e.g. '19.99'."},
                              "description": {"type": "string"},
                              "status": {"type": "string", "enum": ["publish", "draft"]}},
               "required": ["name", "regular_price"]})
    @guarded
    def create_product(a):
        name = a["name"].strip()
        if not name:
            raise ToolError("name must not be empty")
        price = str(a["regular_price"]).strip()
        if not _PRICE_RE.match(price):
            raise ToolError("regular_price must be a positive decimal such as '19.99'")
        created = client.post("products", {
            "name": name, "type": "simple", "regular_price": price,
            "description": a.get("description", ""), "status": a.get("status", "publish"),
        })
        return _product_view(created)

    @app.tool("list_orders",
              "List orders, newest first. Use created_today=true for 'orders that came in today' "
              "(evaluated in the store's timezone), or after/before (ISO 8601) for other ranges.",
              {"properties": {"status": {"type": "string", "enum": ORDER_STATUSES + ["any"]},
                              "created_today": {"type": "boolean"},
                              "after": {"type": "string", "description": "ISO 8601 timestamp, e.g. 2026-10-24T00:00:00+05:30"},
                              "before": {"type": "string", "description": "ISO 8601 timestamp"},
                              **paging}}, read_only=True)
    @guarded
    def list_orders(a):
        after, before = a.get("after"), a.get("before")
        if a.get("created_today"):
            midnight = start_of_today(store_timezone)
            after, before = midnight.isoformat(), (midnight + timedelta(days=1)).isoformat()
        rows = client.get("orders", status=a.get("status"), after=after, before=before,
                          orderby="date", order="desc",
                          per_page=a.get("per_page", 20), page=a.get("page", 1))
        return {"count": len(rows), "timezone": store_timezone, "orders": [_order_view(o) for o in rows]}

    @app.tool("get_order", "Get one order by its numeric id.",
              {"properties": {"id": {"type": "integer", "minimum": 1}}, "required": ["id"]}, read_only=True)
    @guarded
    def get_order(a):
        return _order_view(client.get(f"orders/{a['id']}"))

    @app.tool("update_order_status", "Change an order's status (e.g. processing -> completed).",
              {"properties": {"id": {"type": "integer", "minimum": 1},
                              "status": {"type": "string", "enum": ORDER_STATUSES}},
               "required": ["id", "status"]})
    @guarded
    def update_order_status(a):
        return _order_view(client.put(f"orders/{a['id']}", {"status": a["status"]}))

    return app


def from_env() -> McpApp:
    def need(name: str) -> str:
        value = os.environ.get(name)
        if not value:
            raise RuntimeError(f"missing required environment variable {name}")
        return value

    client = WooClient(need("WOO_BASE_URL"), need("WOO_CONSUMER_KEY"), need("WOO_CONSUMER_SECRET"),
                       timeout=float(os.environ.get("WOO_TIMEOUT_SECONDS", "15")))
    return build_app(client, os.environ.get("STORE_TIMEZONE", "UTC"), need("MCP_AUTH_TOKEN"))
