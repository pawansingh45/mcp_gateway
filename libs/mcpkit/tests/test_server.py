import unittest

from mcpkit import McpApp, ToolError


def make_app(token=None):
    app = McpApp("demo", auth_token=token)

    @app.tool("add", "Add two ints", {"properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                                      "required": ["a", "b"]}, read_only=True)
    def add(args):
        return {"sum": args["a"] + args["b"]}

    @app.tool("boom", "Always fails", {"properties": {}})
    def boom(args):
        raise ToolError("nope")

    @app.tool("crash", "Raises a bug", {"properties": {}})
    def crash(args):
        raise RuntimeError("secret internal detail")

    return app


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.app = make_app()

    def rpc(self, method, params=None, id=1):
        return self.app.handle_message({"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}})

    def test_initialize_echoes_supported_version(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_initialize_falls_back_to_newest_for_unknown_version(self):
        r = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")

    def test_notification_gets_no_reply(self):
        self.assertIsNone(self.app.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list(self):
        names = {t["name"] for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertEqual(names, {"add", "boom", "crash"})

    def test_call_ok(self):
        r = self.rpc("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}})
        self.assertNotIn("isError", r["result"])
        self.assertIn('"sum": 5', r["result"]["content"][0]["text"])

    def test_invalid_arguments_are_tool_errors_not_protocol_errors(self):
        r = self.rpc("tools/call", {"name": "add", "arguments": {"a": "x"}})
        self.assertTrue(r["result"]["isError"])
        self.assertIn("arguments.b: is required", r["result"]["content"][0]["text"])

    def test_tool_error_is_reported_to_model(self):
        r = self.rpc("tools/call", {"name": "boom", "arguments": {}})
        self.assertTrue(r["result"]["isError"])
        self.assertEqual(r["result"]["content"][0]["text"], "nope")

    def test_unexpected_exception_does_not_leak_details(self):
        r = self.rpc("tools/call", {"name": "crash", "arguments": {}})
        self.assertTrue(r["result"]["isError"])
        self.assertNotIn("secret internal detail", r["result"]["content"][0]["text"])

    def test_unknown_tool_and_method(self):
        self.assertEqual(self.rpc("tools/call", {"name": "zzz"})["error"]["code"], -32602)
        self.assertEqual(self.rpc("nope")["error"]["code"], -32601)


class HttpTests(unittest.TestCase):
    def test_bearer_auth_enforced(self):
        client = make_app("s3cret").flask_app().test_client()
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(client.post("/mcp", json=body).status_code, 401)
        self.assertEqual(client.post("/mcp", json=body, headers={"Authorization": "Bearer wrong"}).status_code, 401)
        ok = client.post("/mcp", json=body, headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(ok.status_code, 200)

    def test_notification_returns_202_and_get_is_405(self):
        client = make_app().flask_app().test_client()
        r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(client.get("/mcp").status_code, 405)

    def test_healthz_needs_no_auth(self):
        self.assertEqual(make_app("x").flask_app().test_client().get("/healthz").status_code, 200)


if __name__ == "__main__":
    unittest.main()
