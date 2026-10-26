#!/usr/bin/env python3
"""Run the whole platform on localhost WITHOUT Kubernetes (fast inner dev loop, offline demos, UI tests).

    python scripts/dev_stack.py            # gateway+dashboard on http://localhost:8080

Starts: fake WooCommerce REST API -> real woo-mcp -> real clock & notes servers -> real gateway (+ built
dashboard from services/dashboard/dist) and a keyword-driven fake LLM so the chat works offline.
Login: admin@example.com / admin-password-123. This is a dev tool; the real deployment is the Helm chart.
"""
import json
import os
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in ("libs/mcpkit", "services/gateway", "services/woo-mcp", "services/ref-mcp", "services/woo-mcp/tests"):
    sys.path.insert(0, os.path.join(ROOT, p))

from cryptography.fernet import Fernet  # noqa: E402
from flask import Flask, jsonify, request, send_from_directory  # noqa: E402
from werkzeug.serving import make_server  # noqa: E402

from gateway.app import create_app  # noqa: E402
from gateway.config import Settings  # noqa: E402
from ref_mcp import clock, notes  # noqa: E402
from test_tools import FakeWoo  # noqa: E402
from woo_mcp.client import WooClient  # noqa: E402
from woo_mcp.tools import build_app as build_woo  # noqa: E402


def serve(app, port):
    srv = make_server("127.0.0.1", port, app, threaded=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def fake_llm():
    app = Flask("llm")

    @app.post("/v1/chat/completions")
    def chat():
        msgs = request.get_json()["messages"]
        last = msgs[-1]
        if last["role"] == "tool":
            return jsonify({"choices": [{"message": {"role": "assistant", "content": "Done. Result: " + last["content"][:300]}}]})
        text = last["content"].lower()
        call = None
        if "create" in text and "product" in text:
            call = ("woocommerce__create_product", {"name": "Blue Mug", "regular_price": "12.50"})
        elif "order" in text:
            call = ("woocommerce__list_orders", {"created_today": True})
        elif "time" in text:
            call = ("clock__current_time", {})
        if not call:
            return jsonify({"choices": [{"message": {"role": "assistant", "content": "Offline demo LLM: ask about orders, time or create a product."}}]})
        return jsonify({"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": call[0], "arguments": json.dumps(call[1])}}]}}]})
    return app


def main():
    port = int(os.environ.get("PORT", "8080"))
    tmp = tempfile.mkdtemp()
    fake = FakeWoo()
    serve(fake.app, 18090)
    serve(build_woo(WooClient("http://127.0.0.1:18090", "ck", "cs"), "UTC", "woo-token").flask_app(), 18091)
    serve(clock.build_app("clock-token").flask_app(), 18092)
    serve(notes.build_app(notes.NoteStore(os.path.join(tmp, "notes.json")), "notes-token").flask_app(), 18093)
    serve(fake_llm(), 18094)
    os.environ.update(WOO_T="woo-token", CLOCK_T="clock-token", NOTES_T="notes-token")
    seeds = [{"slug": "woocommerce", "name": "WooCommerce", "url": "http://127.0.0.1:18091/mcp", "credential_env": "WOO_T"},
             {"slug": "clock", "name": "Clock", "url": "http://127.0.0.1:18092/mcp", "credential_env": "CLOCK_T"},
             {"slug": "notes", "name": "Notes", "url": "http://127.0.0.1:18093/mcp", "credential_env": "NOTES_T"}]
    settings = Settings(encryption_keys=(Fernet.generate_key().decode(),), db_path=os.path.join(tmp, "gw.db"),
                        admin_email="admin@example.com", admin_password="admin-password-123",
                        public_url=f"http://localhost:{port}", ssrf_allow_loopback=True, health_interval=3,
                        llm_base_url="http://127.0.0.1:18094/v1", llm_api_key="dev", llm_model="offline-demo",
                        seed_servers=tuple(seeds))
    app = create_app(settings)
    dist = os.path.join(ROOT, "services/dashboard/dist")

    @app.get("/", defaults={"path": "index.html"})
    @app.get("/<path:path>")
    def static_files(path):
        return send_from_directory(dist, path if os.path.exists(os.path.join(dist, path)) else "index.html")

    print(f"Dashboard + gateway: http://localhost:{port}   (admin@example.com / admin-password-123)")
    print(f"MCP endpoint:        http://localhost:{port}/mcp")
    app.run("127.0.0.1", port, threaded=True)


if __name__ == "__main__":
    main()
