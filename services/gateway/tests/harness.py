"""Test helpers: real HTTP servers on localhost so we exercise real sockets,
timeouts and streaming instead of mocks."""
import json
import logging
import tempfile
import threading
import time

from cryptography.fernet import Fernet
from flask import Flask, Response, jsonify, request
from werkzeug.serving import make_server

from gateway.config import Settings
from mcpkit import McpApp, ToolError


logging.getLogger("werkzeug").setLevel(logging.ERROR)


class LiveServer:
    def __init__(self, flask_app: Flask, port: int = 0):
        self.srv = make_server("127.0.0.1", port, flask_app, threaded=True)
        self.port = self.srv.server_port
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def make_settings(tmpdir: str, **overrides) -> Settings:
    base = dict(encryption_keys=(Fernet.generate_key().decode(),), db_path=f"{tmpdir}/gw.db",
                admin_email="admin@example.com", admin_password="correct-horse-battery",
                ssrf_allow_loopback=True, connect_timeout=1.0, list_timeout=2.0, call_timeout=2.0,
                health_interval=0.2, public_url="http://gw.test", llm_api_key="test-llm-key",
                llm_base_url="http://llm.test/v1", rate_limit_per_minute=0)
    base.update(overrides)
    return Settings(**base)


def orders_server(token="orders-secret") -> McpApp:
    app = McpApp("orders", auth_token=token)
    state = {"products": []}

    @app.tool("list_orders", "List orders (test double).", {"properties": {"created_today": {"type": "boolean"}}},
              read_only=True)
    def list_orders(a):
        return {"orders": [{"id": 101, "total": "20.00", "status": "processing"}]}

    @app.tool("create_product", "Create a product (test double).",
              {"properties": {"name": {"type": "string"}, "regular_price": {"type": "string"}},
               "required": ["name", "regular_price"]})
    def create_product(a):
        state["products"].append(a)
        return {"id": len(state["products"]), **a}

    @app.tool("fail", "Always reports a tool error.", {"properties": {}})
    def fail(a):
        raise ToolError("store exploded")

    @app.tool("slow", "Sleeps longer than the gateway timeout.", {"properties": {}})
    def slow(a):
        time.sleep(5)
        return "late"

    app.state = state
    return app


def clock_server(token="clock-secret") -> McpApp:
    app = McpApp("clock", auth_token=token)

    @app.tool("current_time", "Current time (test double).", {"properties": {}}, read_only=True)
    def current_time(a):
        return {"date": "2026-10-24"}

    return app


def collide_server(token="x") -> McpApp:
    """Exposes a tool whose name collides with the clock server's."""
    app = McpApp("collide", auth_token=token)

    @app.tool("current_time", "A different current_time.", {"properties": {}}, read_only=True)
    def current_time(a):
        return {"date": "1999-12-31"}

    return app


def tool_call(name, args, call_id="call_1"):
    return lambda messages: {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


def say(text):
    return lambda messages: {"role": "assistant", "content": text}


def fake_llm():
    """OpenAI-compatible fake. Assign `app.script = [step, ...]` (each step: messages -> assistant message)."""
    app = Flask("fake-llm")
    app.script, app.requests, app.status = [], [], 200

    @app.post("/v1/chat/completions")
    def completions():
        body = request.get_json()
        app.requests.append(body)
        if app.status != 200:
            return jsonify({"error": "boom"}), app.status
        step = app.script[min(len(app.requests) - 1, len(app.script) - 1)]
        return jsonify({"choices": [{"message": step(body["messages"])}]})

    def set_script(*steps):
        app.script, app.requests, app.status = list(steps), [], 200

    app.set_script = set_script
    return app
