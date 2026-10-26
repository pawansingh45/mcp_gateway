"""End-to-end tests that mirror the assignment's "definition of done", over real sockets:
real upstream MCP servers, the real gateway app, a scripted OpenAI-compatible LLM."""
import json
import os
import tempfile
import time
import unittest

from gateway.app import create_app
from tests.harness import (LiveServer, clock_server, collide_server, fake_llm, make_settings, orders_server, say,
                           tool_call)

ADMIN = ("admin@example.com", "correct-horse-battery")


class Env:
    """A running platform: 3 upstreams + gateway + scripted LLM."""

    def __init__(self, **settings):
        self.tmp = tempfile.TemporaryDirectory()
        self.orders_app = orders_server()
        self.woo = LiveServer(self.orders_app.flask_app())
        self.clock = LiveServer(clock_server().flask_app())
        self.collide = LiveServer(collide_server().flask_app())
        self.llm_app = fake_llm()
        self.llm = LiveServer(self.llm_app)
        self.settings = make_settings(self.tmp.name, llm_base_url=f"http://127.0.0.1:{self.llm.port}/v1", **settings)
        self.app = create_app(self.settings, start_background=False)
        self.ctx = self.app.extensions["ctx"]
        self.a = self.app.test_client()   # user A (admin), cookie-based session like the dashboard
        self.b = self.app.test_client()   # user B (member), uses an API key like Claude Code
        self.raw = self.app.test_client()  # unauthenticated / API key only

    def close(self):
        for s in (self.woo, self.clock, self.collide, self.llm):
            try:
                s.stop()
            except Exception:
                pass
        self.ctx.registry.stop()
        self.tmp.cleanup()

    # --- helpers
    def login(self, client, creds=ADMIN):
        r = client.post("/api/auth/login", json={"email": creds[0], "password": creds[1]})
        assert r.status_code == 200, r.get_json()

    def add_servers(self):
        self.login(self.a)
        for slug, srv, token in (("woocommerce", self.woo, "orders-secret"), ("clock", self.clock, "clock-secret"),
                                 ("clock2", self.collide, "x")):
            r = self.a.post("/api/servers", json={"slug": slug, "name": slug.title(), "url": srv.url,
                                                  "auth_type": "bearer", "auth_token": token})
            assert r.status_code == 201, r.get_json()

    def invite_member(self, email="bea@example.com"):
        token = self.a.post("/api/invites", json={"email": email, "role": "member"}).get_json()["token"]
        r = self.b.post("/api/invites/accept", json={"token": token, "name": "Bea", "password": "bea-long-password"})
        assert r.status_code == 201, r.get_json()

    def key_for(self, client):
        return client.post("/api/keys", json={"name": "laptop"}).get_json()

    def rpc(self, key, method, params=None, query="", mid=1):
        body = {"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}
        r = self.raw.post("/mcp" + query, json=body, headers={"Authorization": f"Bearer {key}"} if key else {})
        return r

    def call(self, key, name, args=None, query=""):
        return self.rpc(key, "tools/call", {"name": name, "arguments": args or {}}, query)

    def audit(self, **q):
        return self.a.get("/api/audit", query_string=q).get_json()["rows"]


class DefinitionOfDoneTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_full_flow_two_users(self):
        e = self.env
        e.add_servers()

        # --- admin dashboard: three servers, connected, tools prefixed, collision handled
        servers = {s["slug"]: s for s in e.a.get("/api/servers").get_json()["servers"]}
        self.assertEqual({s["status"] for s in servers.values()}, {"connected"})
        self.assertIn("woocommerce__list_orders", [t["name"] for t in servers["woocommerce"]["tools"]])
        self.assertIn("clock__current_time", [t["name"] for t in servers["clock"]["tools"]])
        self.assertIn("clock2__current_time", [t["name"] for t in servers["clock2"]["tools"]])  # same tool name, no clash

        # --- upstream credentials never reach the browser
        blob = e.a.get("/api/servers").get_data(as_text=True)
        for secret in ("orders-secret", "clock-secret"):
            self.assertNotIn(secret, blob)
        self.assertTrue(servers["woocommerce"]["has_credential"])

        # --- credentials are encrypted at rest
        raw = e.ctx.db.all("SELECT credential_enc FROM servers")
        self.assertTrue(all("orders-secret" not in (r["credential_enc"] or "") for r in raw))

        # --- team: invite + keys
        e.invite_member()
        key_a, key_b = e.key_for(e.a), e.key_for(e.b)

        # --- chat agent as user A: create a product through the gateway
        e.llm_app.set_script(tool_call("woocommerce__create_product", {"name": "Blue Mug", "regular_price": "12.50"}),
                             say("Created Blue Mug at 12.50."))
        r = e.a.post("/api/chat", json={"messages": [{"role": "user", "content": "create product Blue Mug 12.50"}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        out = r.get_json()
        self.assertEqual(out["tool_calls"][0]["tool"], "woocommerce__create_product")
        self.assertEqual(out["tool_calls"][0]["outcome"], "success")
        self.assertEqual(e.orders_app.state["products"][0]["name"], "Blue Mug")  # the upstream really received it
        # the model was shown the tool result and a system prompt with today's date
        self.assertIn("Current date", e.llm_app.requests[0]["messages"][0]["content"])
        self.assertEqual(e.llm_app.requests[1]["messages"][-1]["role"], "tool")

        # --- chat agent: "what orders came in today?"
        e.llm_app.set_script(tool_call("woocommerce__list_orders", {"created_today": True}), say("Order 101 came in."))
        out = e.a.post("/api/chat", json={"messages": [{"role": "user", "content": "orders today?"}]}).get_json()
        self.assertIn("101", out["tool_calls"][0]["result"])

        # --- external MCP client as user B: handshake, list, same question
        init = e.rpc(key_b["secret"], "initialize", {"protocolVersion": "2025-06-18"}).get_json()
        self.assertEqual(init["result"]["serverInfo"]["name"], "mcp-gateway")
        self.assertEqual(e.raw.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                                    headers={"Authorization": f"Bearer {key_b['secret']}"}).status_code, 202)
        names = {t["name"] for t in e.rpc(key_b["secret"], "tools/list").get_json()["result"]["tools"]}
        self.assertTrue({"woocommerce__list_orders", "woocommerce__create_product", "clock__current_time",
                         "clock2__current_time"} <= names)
        res = e.call(key_b["secret"], "woocommerce__list_orders", {"created_today": True}).get_json()["result"]
        self.assertIn("101", res["content"][0]["text"])

        # --- one "prompt" needing two different upstream servers
        t = e.call(key_b["secret"], "clock__current_time").get_json()["result"]["content"][0]["text"]
        o = e.call(key_b["secret"], "woocommerce__list_orders").get_json()["result"]["content"][0]["text"]
        self.assertIn("2026-10-24", t)
        self.assertIn("101", o)
        self.assertIn("1999-12-31", e.call(key_b["secret"], "clock2__current_time").get_json()["result"]["content"][0]["text"])

        # --- revoke B's key: next call is rejected
        kid = e.b.get("/api/keys").get_json()["keys"][0]["id"]
        self.assertEqual(e.a.delete(f"/api/keys/{kid}").status_code, 200)  # admin revokes B's key
        rejected = e.call(key_b["secret"], "woocommerce__list_orders")
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(e.rpc(key_b["secret"], "tools/list").status_code, 401)

        # --- disable one upstream: its tools vanish from BOTH the external client and the chat agent
        sid = servers["clock"]["id"]
        self.assertEqual(e.a.patch(f"/api/servers/{sid}", json={"enabled": False}).status_code, 200)
        names = {t["name"] for t in e.rpc(key_a["secret"], "tools/list").get_json()["result"]["tools"]}
        self.assertNotIn("clock__current_time", names)
        self.assertIn("clock2__current_time", names)
        e.llm_app.set_script(say("ok"))
        e.a.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]})
        offered = {t["function"]["name"] for t in e.llm_app.requests[0]["tools"]}
        self.assertNotIn("clock__current_time", offered)
        self.assertEqual(e.call(key_a["secret"], "clock__current_time").get_json()["error"]["code"], -32602)
        self.assertEqual({s["slug"]: s["status"] for s in e.a.get("/api/servers").get_json()["servers"]}["clock"],
                         "disabled")

        # --- kill one upstream pod: others keep working, dashboard shows it unreachable
        e.woo.stop()
        started = time.monotonic()
        dead = e.call(key_a["secret"], "woocommerce__list_orders").get_json()["result"]
        self.assertTrue(dead["isError"])
        self.assertIn("unreachable", dead["content"][0]["text"])
        self.assertLess(time.monotonic() - started, 3)
        ok = e.call(key_a["secret"], "clock2__current_time").get_json()["result"]
        self.assertNotIn("isError", ok)
        e.ctx.registry.refresh_all()
        status = {s["slug"]: s["status"] for s in e.a.get("/api/servers").get_json()["servers"]}
        self.assertEqual(status["woocommerce"], "unreachable")
        self.assertEqual(status["clock2"], "connected")
        # its tools are hidden from the tool list while it is down; other servers' tools are intact
        names = {t["name"] for t in e.rpc(key_a["secret"], "tools/list").get_json()["result"]["tools"]}
        self.assertNotIn("woocommerce__list_orders", names)
        self.assertIn("clock2__current_time", names)

        # --- audit: every call is there, attributed to the right user
        rows = e.audit(limit=200)
        by_user = {}
        for r in rows:
            by_user.setdefault(r["user_email"], []).append(r)
        self.assertIn("admin@example.com", by_user)
        self.assertIn("bea@example.com", by_user)
        chat_calls = [r for r in by_user["admin@example.com"] if r["source"] == "chat"]
        self.assertEqual({r["tool"] for r in chat_calls}, {"create_product", "list_orders"})
        self.assertTrue(all(r["server_slug"] == "woocommerce" for r in chat_calls))
        b_rows = [r for r in by_user["bea@example.com"] if r["source"] == "mcp"]
        self.assertEqual(len(b_rows), 4)
        self.assertTrue(all(r["outcome"] == "success" and r["duration_ms"] >= 0 for r in b_rows))
        self.assertTrue(any(r["outcome"] == "unreachable" for r in by_user["admin@example.com"]))
        # a member sees only their own entries
        e.login(e.b, ("bea@example.com", "bea-long-password"))
        mine = e.b.get("/api/audit").get_json()["rows"]
        self.assertTrue(mine and all(r["user_email"] == "bea@example.com" for r in mine))


class AccessControlTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.add_servers()
        self.env.invite_member()
        self.env.login(self.env.b, ("bea@example.com", "bea-long-password"))

    def test_member_cannot_manage_anything(self):
        b = self.env.b
        self.assertEqual(b.post("/api/servers", json={"slug": "x", "url": "http://x"}).status_code, 403)
        self.assertEqual(b.get("/api/users").status_code, 403)
        self.assertEqual(b.post("/api/invites", json={"email": "z@example.com"}).status_code, 403)
        self.assertEqual(b.patch("/api/servers/1", json={"enabled": False}).status_code, 403)
        self.assertEqual(b.delete("/api/servers/1").status_code, 403)
        self.assertEqual(b.post("/api/keys", json={"user_id": 1}).status_code, 403)

    def test_member_sees_no_upstream_urls_or_credentials(self):
        text = self.env.b.get("/api/servers").get_data(as_text=True)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("has_credential", text)
        self.assertNotIn("auth_type", text)

    def test_member_cannot_revoke_someone_elses_key(self):
        admin_key = self.env.key_for(self.env.a)["key"]
        self.assertEqual(self.env.b.delete(f"/api/keys/{admin_key['id']}").status_code, 404)

    def test_unauthenticated_requests_rejected(self):
        e = self.env
        self.assertEqual(e.raw.get("/api/servers").status_code, 401)
        self.assertEqual(e.rpc(None, "tools/list").status_code, 401)
        self.assertEqual(e.rpc("gw_not-a-real-key", "tools/list").status_code, 401)
        self.assertIn("Bearer", e.rpc(None, "tools/list").headers["WWW-Authenticate"])

    def test_session_token_is_not_an_mcp_credential_and_keys_are_not_dashboard_sessions(self):
        e = self.env
        key = e.key_for(e.b)["secret"]
        self.assertEqual(e.raw.get("/api/servers", headers={"Authorization": f"Bearer {key}"}).status_code, 401)

    def test_disabling_a_user_kills_sessions_and_keys(self):
        e = self.env
        key = e.key_for(e.b)["secret"]
        uid = [u for u in e.a.get("/api/users").get_json()["users"] if u["email"] == "bea@example.com"][0]["id"]
        self.assertEqual(e.call(key, "clock__current_time").status_code, 200)
        e.a.patch(f"/api/users/{uid}", json={"disabled": True})
        self.assertEqual(e.call(key, "clock__current_time").status_code, 401)
        self.assertEqual(e.b.get("/api/me").status_code, 401)

    def test_api_keys_are_stored_hashed(self):
        e = self.env
        secret = e.key_for(e.b)["secret"]
        stored = json.dumps(e.ctx.db.all("SELECT * FROM api_keys"))
        self.assertNotIn(secret, stored)
        self.assertNotIn(secret[3:], stored)

    def test_cannot_demote_or_disable_last_admin(self):
        e = self.env
        admin_id = e.a.get("/api/me").get_json()["user"]["id"]
        self.assertEqual(e.a.patch(f"/api/users/{admin_id}", json={"role": "member"}).status_code, 400)
        self.assertEqual(e.a.patch(f"/api/users/{admin_id}", json={"disabled": True}).status_code, 400)

    def test_invite_is_single_use_and_cross_origin_writes_are_rejected(self):
        e = self.env
        token = e.a.post("/api/invites", json={"email": "cy@example.com"}).get_json()["token"]
        ok = e.raw.post("/api/invites/accept", json={"token": token, "name": "Cy", "password": "cy-long-password"})
        self.assertEqual(ok.status_code, 201)
        again = e.raw.post("/api/invites/accept", json={"token": token, "name": "Cy", "password": "cy-long-password"})
        self.assertEqual(again.status_code, 400)
        evil = e.a.post("/api/servers", json={"slug": "evil", "url": "http://x"}, headers={"Origin": "http://evil.example"})
        self.assertEqual(evil.status_code, 403)

    def test_login_throttle_and_generic_error(self):
        e = self.env
        for _ in range(5):
            r = e.raw.post("/api/auth/login", json={"email": "admin@example.com", "password": "nope-nope-nope"})
            self.assertEqual(r.status_code, 401)
        self.assertEqual(e.raw.post("/api/auth/login", json={"email": "admin@example.com",
                                                             "password": ADMIN[1]}).status_code, 429)


class PolicyAndResilienceTests(unittest.TestCase):
    def setUp(self):
        self.env = Env(call_timeout=0.5)
        self.addCleanup(self.env.close)
        self.env.add_servers()
        self.env.invite_member()
        self.key_a, self.key_b = self.env.key_for(self.env.a)["secret"], self.env.key_for(self.env.b)["secret"]

    def test_admin_only_tools_are_hidden_and_rejected_for_members(self):
        e = self.env
        sid = e.ctx.registry.list_servers()[0]["id"]
        r = e.a.patch(f"/api/servers/{sid}", json={"tool_policy": {"admin_only": ["create_*"], "deny": ["fail"]}})
        self.assertEqual(r.status_code, 200, r.get_json())
        member = {t["name"] for t in e.rpc(self.key_b, "tools/list").get_json()["result"]["tools"]}
        admin = {t["name"] for t in e.rpc(self.key_a, "tools/list").get_json()["result"]["tools"]}
        self.assertNotIn("woocommerce__create_product", member)
        self.assertIn("woocommerce__list_orders", member)
        self.assertIn("woocommerce__create_product", admin)
        self.assertNotIn("woocommerce__fail", admin)  # deny applies to everyone
        denied = e.call(self.key_b, "woocommerce__create_product", {"name": "x", "regular_price": "1"})
        self.assertEqual(denied.get_json()["error"]["code"], -32602)
        self.assertEqual(e.orders_app.state["products"], [])  # never reached the upstream
        self.assertIn("denied", {r["outcome"] for r in e.audit()})
        ok = e.call(self.key_a, "woocommerce__create_product", {"name": "x", "regular_price": "1"})
        self.assertNotIn("isError", ok.get_json()["result"])

    def test_slow_upstream_times_out_and_does_not_block_others(self):
        e = self.env
        started = time.monotonic()
        res = e.call(self.key_a, "woocommerce__slow").get_json()["result"]
        self.assertTrue(res["isError"])
        self.assertIn("did not answer", res["content"][0]["text"])
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertEqual(e.audit()[0]["outcome"], "timeout")
        # a timeout is not proof the server is down, and other servers are fine
        self.assertNotIn("isError", e.call(self.key_a, "clock__current_time").get_json()["result"])
        self.assertEqual(e.ctx.registry.status_of(e.ctx.registry.list_servers()[0]), "connected")

    def test_tool_listing_is_instant_even_if_an_upstream_is_hanging(self):
        e = self.env
        e.woo.stop()  # upstream gone, but catalog is cached and listing never touches upstreams
        started = time.monotonic()
        names = {t["name"] for t in e.rpc(self.key_a, "tools/list").get_json()["result"]["tools"]}
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn("clock__current_time", names)

    def test_tool_level_errors_pass_through_and_are_audited(self):
        e = self.env
        res = e.call(self.key_a, "woocommerce__fail").get_json()["result"]
        self.assertTrue(res["isError"])
        self.assertIn("store exploded", res["content"][0]["text"])
        self.assertEqual(e.audit()[0]["outcome"], "tool_error")

    def test_lazy_mode_search_then_execute(self):
        e = self.env
        tools = e.rpc(self.key_a, "tools/list", query="?mode=lazy").get_json()["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"search_tools", "execute_tool"})
        found = json.loads(e.call(self.key_a, "search_tools", {"query": "orders"}, "?mode=lazy")
                           .get_json()["result"]["content"][0]["text"])
        self.assertEqual(found[0]["name"], "woocommerce__list_orders")
        out = e.call(self.key_a, "execute_tool", {"name": "woocommerce__list_orders", "arguments": {}}, "?mode=lazy")
        self.assertIn("101", out.get_json()["result"]["content"][0]["text"])
        row = e.audit()[0]
        self.assertEqual((row["server_slug"], row["tool"]), ("woocommerce", "list_orders"))  # audited as the real tool

    def test_rate_limit_applies_per_user(self):
        e = self.env
        e.ctx.service.limiter.__init__(3)  # 3 calls/minute
        outs = [e.call(self.key_a, "clock__current_time").get_json()["result"] for _ in range(4)]
        self.assertTrue(outs[3]["isError"])
        self.assertIn("Rate limit", outs[3]["content"][0]["text"])
        self.assertNotIn("isError", e.call(self.key_b, "clock__current_time").get_json()["result"])

    def test_ssrf_blocked_at_registration(self):
        e = self.env
        for url in ("http://169.254.169.254/latest/meta-data", "http://10.0.0.5/mcp", "file:///etc/passwd"):
            r = e.a.post("/api/servers", json={"slug": "bad", "name": "bad", "url": url})
            self.assertEqual(r.status_code, 400, url)

    def test_upstream_changing_its_tools_is_recorded(self):
        e = self.env
        e.orders_app.tools.pop("fail")  # simulates a server silently changing its tool list
        e.ctx.registry.refresh_all()
        self.assertIn("tools_changed", {r["outcome"] for r in e.audit()})

    def test_metrics_endpoint(self):
        e = self.env
        e.call(self.key_a, "clock__current_time")
        text = e.raw.get("/metrics").get_data(as_text=True)
        self.assertIn('mcp_gateway_tool_calls_total{server="clock",outcome="success"} 1', text)
        self.assertIn('mcp_gateway_upstream_up{server="woocommerce",status="connected"} 1', text)


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.env.add_servers()

    def chat(self, text="hi"):
        return self.env.a.post("/api/chat", json={"messages": [{"role": "user", "content": text}]})

    def test_not_configured(self):
        env = Env(llm_api_key=None)
        self.addCleanup(env.close)
        env.add_servers()
        r = env.a.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 503)

    def test_provider_errors_are_clear(self):
        self.env.llm_app.set_script(say("x"))
        self.env.llm_app.status = 401
        self.assertEqual(self.chat().status_code, 502)
        self.env.llm_app.status = 429
        self.assertEqual(self.chat().status_code, 429)

    def test_malformed_tool_arguments_do_not_crash_the_loop(self):
        bad = lambda m: {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "clock__current_time", "arguments": "{not json"}}]}
        self.env.llm_app.set_script(bad, say("recovered"))
        out = self.chat().get_json()
        self.assertEqual(out["reply"], "recovered")
        self.assertEqual(out["tool_calls"][0]["outcome"], "bad_arguments")

    def test_hallucinated_tool_is_reported_back_to_the_model(self):
        self.env.llm_app.set_script(tool_call("nope__nothing", {}), say("sorry"))
        out = self.chat().get_json()
        self.assertEqual(out["tool_calls"][0]["outcome"], "not_found")
        self.assertIn("ERROR", self.env.llm_app.requests[1]["messages"][-1]["content"])

    def test_loop_is_bounded(self):
        self.env.llm_app.set_script(tool_call("clock__current_time", {}))  # model never stops
        out = self.chat().get_json()
        self.assertEqual(len(out["tool_calls"]), self.env.settings.chat_max_steps)
        self.assertIn("stopped", out["reply"])

    def test_member_chat_respects_policy_and_identity(self):
        e = self.env
        e.invite_member()
        e.login(e.b, ("bea@example.com", "bea-long-password"))
        sid = e.ctx.registry.list_servers()[0]["id"]
        e.a.patch(f"/api/servers/{sid}", json={"tool_policy": {"admin_only": ["create_*"]}})
        e.llm_app.set_script(tool_call("woocommerce__create_product", {"name": "x", "regular_price": "1"}), say("denied"))
        out = e.b.post("/api/chat", json={"messages": [{"role": "user", "content": "make a product"}]}).get_json()
        offered = {t["function"]["name"] for t in e.llm_app.requests[0]["tools"]}
        self.assertNotIn("woocommerce__create_product", offered)
        self.assertEqual(out["tool_calls"][0]["outcome"], "denied")
        row = e.audit()[0]
        self.assertEqual((row["user_email"], row["source"], row["outcome"]), ("bea@example.com", "chat", "denied"))

    def test_many_tools_switches_to_search_mode(self):
        env = Env(chat_max_direct_tools=3)
        self.addCleanup(env.close)
        env.add_servers()
        env.llm_app.set_script(tool_call("search_tools", {"query": "orders"}), say("found"))
        out = env.a.post("/api/chat", json={"messages": [{"role": "user", "content": "x"}]}).get_json()
        self.assertEqual(out["tool_mode"], "search")
        self.assertEqual({t["function"]["name"] for t in env.llm_app.requests[0]["tools"]}, {"search_tools", "execute_tool"})


class LifecycleTests(unittest.TestCase):
    def test_seed_and_bootstrap_are_idempotent_and_encrypt_credentials(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        srv = LiveServer(orders_server().flask_app())
        self.addCleanup(srv.stop)
        seeds = ({"slug": "woocommerce", "name": "WooCommerce", "url": srv.url, "credential_env": "WOO_TOKEN"},)
        s = make_settings(tmp.name, seed_servers=seeds)
        os.environ["WOO_TOKEN"] = "orders-secret"
        self.addCleanup(os.environ.pop, "WOO_TOKEN", None)
        app1 = create_app(s, start_background=False)
        app1.extensions["ctx"].registry.refresh_all()
        app2 = create_app(s, start_background=False)  # "pod restart": same DB
        ctx = app2.extensions["ctx"]
        self.assertEqual(len(ctx.registry.list_servers()), 1)
        self.assertEqual(len(ctx.db.all("SELECT * FROM users")), 1)
        row = ctx.registry.list_servers()[0]
        self.assertTrue(row["managed"])
        self.assertNotIn("orders-secret", row["credential_enc"])
        ctx.registry.refresh_all()
        self.assertEqual(ctx.registry.status_of(row), "connected")  # decrypted + sent correctly

    def test_health_loop_detects_death_and_recovery(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        woo = LiveServer(orders_server().flask_app())
        port = woo.port
        s = make_settings(tmp.name, health_interval=0.2, connect_timeout=0.5,
                          seed_servers=({"slug": "woocommerce", "name": "W", "url": woo.url, "credential_env": "T"},))
        os.environ["T"] = "orders-secret"
        self.addCleanup(os.environ.pop, "T", None)
        app = create_app(s, start_background=True)
        reg = app.extensions["ctx"].registry
        self.addCleanup(reg.stop)

        def wait_for(expected, timeout=5):
            end = time.monotonic() + timeout
            while time.monotonic() < end:
                if reg.status_of(reg.list_servers()[0]) == expected:
                    return True
                time.sleep(0.05)
            return False

        self.assertTrue(wait_for("connected"))
        woo.stop()                                   # kubectl delete pod
        self.assertTrue(wait_for("unreachable"))
        revived = LiveServer(orders_server().flask_app(), port=port)  # pod comes back on the same Service address
        self.addCleanup(revived.stop)
        self.assertTrue(wait_for("connected"))


if __name__ == "__main__":
    unittest.main()
