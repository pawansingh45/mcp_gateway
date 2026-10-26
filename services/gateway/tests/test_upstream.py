import json
import time
import unittest

from flask import Flask, Response, jsonify, redirect, request

from gateway.upstream import UpstreamClient, UpstreamError
from tests.harness import LiveServer, orders_server


def client_for(srv, token="orders-secret", **kw):
    return UpstreamClient(srv.url, {"Authorization": f"Bearer {token}"}, connect_timeout=1.0, **kw)


class SseServer:
    """A server that answers every request as an SSE stream, preceded by an unrelated notification."""

    def __init__(self):
        self.app = Flask("sse")
        self.sessions_created = 0

        @self.app.post("/mcp")
        def mcp():
            msg = request.get_json()
            if "id" not in msg:
                return "", 202
            if msg["method"] == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "sse"}}
            elif msg["method"] == "tools/list":
                result = {"tools": [{"name": "ping_tool", "inputSchema": {"type": "object"}}]}
            else:
                result = {"content": [{"type": "text", "text": "via-sse"}]}

            def stream():
                yield 'data: {"jsonrpc":"2.0","method":"notifications/message","params":{}}\n\n'
                yield ": keep-alive comment\n\n"
                yield "data: " + json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n\n"
            return Response(stream(), mimetype="text/event-stream")


class UpstreamClientTests(unittest.TestCase):
    def test_json_upstream_list_and_call(self):
        srv = LiveServer(orders_server().flask_app())
        self.addCleanup(srv.stop)
        c = client_for(srv)
        names = {t["name"] for t in c.list_tools(2)}
        self.assertIn("list_orders", names)
        res = c.call_tool("create_product", {"name": "Mug", "regular_price": "9.99"}, 2)
        self.assertIn("Mug", res["content"][0]["text"])

    def test_wrong_credential_is_an_auth_error(self):
        srv = LiveServer(orders_server().flask_app())
        self.addCleanup(srv.stop)
        with self.assertRaises(UpstreamError) as cm:
            client_for(srv, token="wrong").list_tools(2)
        self.assertEqual(cm.exception.kind, "auth")

    def test_sse_responses_are_parsed_and_unrelated_events_ignored(self):
        s = SseServer()
        srv = LiveServer(s.app)
        self.addCleanup(srv.stop)
        c = UpstreamClient(srv.url, {}, connect_timeout=1.0)
        self.assertEqual(c.list_tools(2)[0]["name"], "ping_tool")
        self.assertEqual(c.call_tool("ping_tool", {}, 2)["content"][0]["text"], "via-sse")

    def test_session_id_is_sent_and_expired_session_is_renegotiated(self):
        app = Flask("stateful")
        live = {"ids": set(), "n": 0}

        @app.post("/mcp")
        def mcp():
            msg = request.get_json()
            sid = request.headers.get("Mcp-Session-Id")
            if msg.get("method") == "initialize":
                live["n"] += 1
                new = f"sess-{live['n']}"
                live["ids"].add(new)
                r = jsonify({"jsonrpc": "2.0", "id": msg["id"], "result": {"protocolVersion": "2025-06-18"}})
                r.headers["Mcp-Session-Id"] = new
                return r
            if sid not in live["ids"]:
                return "gone", 404
            if "id" not in msg:
                return "", 202
            return jsonify({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [{"name": "t"}]}})

        srv = LiveServer(app)
        self.addCleanup(srv.stop)
        c = UpstreamClient(srv.url, {}, connect_timeout=1.0)
        self.assertEqual(c.list_tools(2)[0]["name"], "t")
        live["ids"].clear()  # server "restarts" and forgets every session
        self.assertEqual(c.list_tools(2)[0]["name"], "t")
        self.assertEqual(live["n"], 2)

    def test_redirects_are_not_followed(self):
        app = Flask("redir")

        @app.post("/mcp")
        def mcp():
            return redirect("http://169.254.169.254/latest/meta-data", code=307)

        srv = LiveServer(app)
        self.addCleanup(srv.stop)
        with self.assertRaises(UpstreamError) as cm:
            UpstreamClient(srv.url, {}).list_tools(2)
        self.assertIn("redirect", cm.exception.message)

    def test_oversized_response_is_rejected(self):
        app = Flask("big")

        @app.post("/mcp")
        def mcp():
            return Response("x" * 50_000, mimetype="application/json")

        srv = LiveServer(app)
        self.addCleanup(srv.stop)
        with self.assertRaises(UpstreamError) as cm:
            UpstreamClient(srv.url, {}, max_bytes=10_000).list_tools(2)
        self.assertIn("too large", cm.exception.message)

    def test_slow_call_times_out_within_the_deadline(self):
        srv = LiveServer(orders_server().flask_app())
        self.addCleanup(srv.stop)
        c = client_for(srv)
        c.list_tools(2)
        started = time.monotonic()
        with self.assertRaises(UpstreamError) as cm:
            c.call_tool("slow", {}, timeout=0.5)
        self.assertEqual(cm.exception.kind, "timeout")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_dead_port_is_unreachable(self):
        with self.assertRaises(UpstreamError) as cm:
            UpstreamClient("http://127.0.0.1:9/mcp", {}, connect_timeout=0.5).list_tools(1)
        self.assertEqual(cm.exception.kind, "unreachable")

    def test_pre_connect_check_blocks_before_any_request(self):
        def deny():
            raise ValueError("private address")
        with self.assertRaises(UpstreamError) as cm:
            UpstreamClient("http://127.0.0.1:9/mcp", {}, pre_connect_check=deny).list_tools(1)
        self.assertIn("blocked", cm.exception.message)

    def test_client_headers_are_only_the_configured_credential(self):
        seen = {}
        app = Flask("spy")

        @app.post("/mcp")
        def mcp():
            seen.update(request.headers)
            msg = request.get_json()
            if "id" not in msg:
                return "", 202
            return jsonify({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": []}})

        srv = LiveServer(app)
        self.addCleanup(srv.stop)
        UpstreamClient(srv.url, {"Authorization": "Bearer UPSTREAM"}).list_tools(2)
        self.assertEqual(seen["Authorization"], "Bearer UPSTREAM")
        self.assertNotIn("Cookie", seen)


if __name__ == "__main__":
    unittest.main()
