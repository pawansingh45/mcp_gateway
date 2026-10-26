import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { api } from "./api.js";

/* ---------- small shared pieces ---------- */
const fmt = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

function usePolling(fn, ms, deps = []) {
  const [state, setState] = useState({ data: null, error: null });
  const load = useCallback(async () => {
    try { setState({ data: await fn(), error: null }); }
    catch (e) { setState((s) => ({ ...s, error: e.message })); }
  }, deps); // eslint-disable-line
  useEffect(() => {
    load();
    const t = ms ? setInterval(load, ms) : null;
    return () => t && clearInterval(t);
  }, [load, ms]);
  return { ...state, reload: load };
}

function Badge({ status }) {
  const label = { connected: "Connected", unreachable: "Unreachable", disabled: "Disabled", pending: "Connecting…" }[status] || status;
  return <span className={`badge ${status}`}>{label}</span>;
}

function Err({ message }) { return message ? <div className="error" role="alert">{message}</div> : null; }

function Modal({ title, onClose, children }) {
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" role="dialog" aria-label={title} onClick={(e) => e.stopPropagation()}>
        <h3>{title}</h3>
        {children}
      </div>
    </div>
  );
}

function CopyBox({ text }) {
  return (
    <div className="copybox">
      <code>{text}</code>
      <button onClick={() => navigator.clipboard?.writeText(text)}>Copy</button>
    </div>
  );
}

/* ---------- auth screens ---------- */
function Login({ onLogin }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    try { onLogin((await api("POST", "/auth/login", { email, password })).user); }
    catch (err) { setError(err.message); }
  };
  return (
    <div className="center-card">
      <h1>MCP Gateway</h1>
      <form onSubmit={submit}>
        <label>Email<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus required /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        <Err message={error} />
        <button className="primary">Sign in</button>
      </form>
    </div>
  );
}

function AcceptInvite({ token, onLogin }) {
  const [name, setName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    try {
      const { user } = await api("POST", "/invites/accept", { token, name, password });
      window.location.hash = "#/servers";
      onLogin(user);
    } catch (err) { setError(err.message); }
  };
  return (
    <div className="center-card">
      <h1>Join the team</h1>
      <form onSubmit={submit}>
        <label>Your name<input value={name} onChange={(e) => setName(e.target.value)} required /></label>
        <label>Password (min 10 characters)<input type="password" minLength={10} value={password} onChange={(e) => setPassword(e.target.value)} required /></label>
        <Err message={error} />
        <button className="primary">Create account</button>
      </form>
    </div>
  );
}

/* ---------- servers ---------- */
const EMPTY_FORM = { slug: "", name: "", url: "", auth_type: "none", auth_token: "", auth_header_name: "", auth_header_value: "", policy: "" };

function ServerForm({ server, onClose, onSaved }) {
  const editing = !!server;
  const [f, setF] = useState(editing
    ? { ...EMPTY_FORM, name: server.name, url: server.url, auth_type: "none", policy: (server.tool_policy.admin_only || []).join(", ") }
    : EMPTY_FORM);
  const [changeCred, setChangeCred] = useState(!editing);
  const [error, setError] = useState("");
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    const admin_only = f.policy.split(",").map((s) => s.trim()).filter(Boolean);
    const body = { name: f.name, url: f.url, tool_policy: admin_only.length ? { admin_only } : {} };
    if (!editing) body.slug = f.slug;
    if (changeCred) {
      body.auth_type = f.auth_type;
      if (f.auth_type === "bearer") body.auth_token = f.auth_token;
      if (f.auth_type === "header") { body.auth_header_name = f.auth_header_name; body.auth_header_value = f.auth_header_value; }
      if (editing && f.auth_type === "none") body.clear_credential = true;
    }
    try {
      await (editing ? api("PATCH", `/servers/${server.id}`, body) : api("POST", "/servers", body));
      onSaved();
    } catch (err) { setError(err.message); }
  };

  return (
    <Modal title={editing ? `Edit ${server.name}` : "Add MCP server"} onClose={onClose}>
      <form onSubmit={submit}>
        {!editing && <label>Slug (tool prefix, permanent)<input value={f.slug} onChange={set("slug")} pattern="[a-z][a-z0-9-]{0,23}" placeholder="e.g. github" required /></label>}
        <label>Display name<input value={f.name} onChange={set("name")} required /></label>
        <label>Streamable HTTP URL<input value={f.url} onChange={set("url")} placeholder="https://host/mcp" required /></label>
        {editing && server.has_credential && !changeCred && (
          <p className="muted">A credential is stored (hidden). <a href="#/servers" onClick={(e) => { e.preventDefault(); setChangeCred(true); }}>Replace or remove it</a></p>
        )}
        {changeCred && (
          <>
            <label>Credential
              <select value={f.auth_type} onChange={set("auth_type")}>
                <option value="none">None</option>
                <option value="bearer">Bearer token</option>
                <option value="header">Custom header</option>
              </select>
            </label>
            {f.auth_type === "bearer" && <label>Token<input type="password" autoComplete="off" value={f.auth_token} onChange={set("auth_token")} required /></label>}
            {f.auth_type === "header" && (
              <>
                <label>Header name<input value={f.auth_header_name} onChange={set("auth_header_name")} placeholder="X-Api-Key" required /></label>
                <label>Header value<input type="password" autoComplete="off" value={f.auth_header_value} onChange={set("auth_header_value")} required /></label>
              </>
            )}
            <p className="muted">Stored encrypted on the server. It is never shown again and never sent to members or MCP clients.</p>
          </>
        )}
        <label>Admin-only tools (comma-separated globs, optional)
          <input value={f.policy} onChange={set("policy")} placeholder="create_*, update_*" />
        </label>
        <Err message={error} />
        <div className="row"><button type="button" onClick={onClose}>Cancel</button><button className="primary">Save</button></div>
      </form>
    </Modal>
  );
}

function Servers({ me }) {
  const { data, error, reload } = usePolling(() => api("GET", "/servers"), 5000);
  const [editing, setEditing] = useState(null); // null | "new" | server
  const [open, setOpen] = useState({});
  const [actionError, setActionError] = useState("");
  const isAdmin = me.role === "admin";

  const act = async (fn) => { setActionError(""); try { await fn(); reload(); } catch (e) { setActionError(e.message); } };

  return (
    <section>
      <div className="row between">
        <h2>MCP servers</h2>
        {isAdmin && <button className="primary" onClick={() => setEditing("new")}>Add server</button>}
      </div>
      <Err message={error || actionError} />
      {data?.servers.map((s) => (
        <div className="card" key={s.id}>
          <div className="row between">
            <div>
              <strong>{s.name}</strong> <code>{s.slug}</code> <Badge status={s.status} />
              {s.managed && <span className="tag">seeded</span>}
              {isAdmin && <div className="muted small">{s.url} · {s.has_credential ? "credential stored" : "no credential"}</div>}
              {isAdmin && s.status === "unreachable" && s.last_error && <div className="error small">{s.last_error}</div>}
              <div className="muted small">checked {fmt(s.checked_at)}{s.latency_ms != null && ` · ${s.latency_ms} ms`}</div>
            </div>
            <div className="row">
              <button onClick={() => setOpen({ ...open, [s.id]: !open[s.id] })}>{s.tools.length} tools {open[s.id] ? "▴" : "▾"}</button>
              {isAdmin && (
                <>
                  <button onClick={() => act(() => api("PATCH", `/servers/${s.id}`, { enabled: !s.enabled }))}>{s.enabled ? "Disable" : "Enable"}</button>
                  <button onClick={() => setEditing(s)}>Edit</button>
                  <button className="danger" onClick={() => window.confirm(`Remove ${s.name}?`) && act(() => api("DELETE", `/servers/${s.id}`))}>Remove</button>
                </>
              )}
            </div>
          </div>
          {open[s.id] && (
            <ul className="tools">
              {s.tools.map((t) => <li key={t.name}><code>{t.name}</code><span className="muted"> — {t.description}</span></li>)}
              {!s.tools.length && <li className="muted">No tools{s.status === "unreachable" ? " known (server never connected)" : ""}.</li>}
            </ul>
          )}
        </div>
      ))}
      {data && !data.servers.length && <p className="muted">No servers registered yet.</p>}
      {editing && <ServerForm server={editing === "new" ? null : editing} onClose={() => setEditing(null)} onSaved={() => { setEditing(null); reload(); }} />}
    </section>
  );
}

/* ---------- API keys ---------- */
function Keys({ me }) {
  const { data, error, reload } = usePolling(() => api("GET", "/keys"), 0);
  const cfg = usePolling(() => api("GET", "/config"), 0);
  const [name, setName] = useState("");
  const [fresh, setFresh] = useState(null);
  const [err, setErr] = useState("");
  const create = async (e) => {
    e.preventDefault();
    try { setFresh((await api("POST", "/keys", { name })).secret); setName(""); reload(); }
    catch (x) { setErr(x.message); }
  };
  const url = cfg.data?.gateway_url || "";
  return (
    <section>
      <h2>Your API keys</h2>
      <p className="muted">One key per person. Add the gateway URL and your key to any MCP client; you never need the upstream servers' credentials.</p>
      <form className="row" onSubmit={create}>
        <input placeholder="Key name, e.g. laptop" value={name} onChange={(e) => setName(e.target.value)} />
        <button className="primary">Generate key</button>
      </form>
      <Err message={error || err} />
      {fresh && (
        <div className="card highlight">
          <strong>Copy your key now — it will not be shown again.</strong>
          <CopyBox text={fresh} />
          <p className="muted small">Claude Code:</p>
          <CopyBox text={`claude mcp add --transport http team-gateway ${url} --header "Authorization: Bearer ${fresh}"`} />
          <p className="muted small">Cursor / generic JSON config:</p>
          <CopyBox text={JSON.stringify({ mcpServers: { "team-gateway": { url, headers: { Authorization: `Bearer ${fresh}` } } } })} />
        </div>
      )}
      <table>
        <thead><tr><th>Name</th><th>Prefix</th><th>Created</th><th>Last used</th><th>Status</th><th /></tr></thead>
        <tbody>
          {data?.keys.map((k) => (
            <tr key={k.id}>
              <td>{k.name}</td><td><code>{k.prefix}…</code></td><td>{fmt(k.created_at)}</td><td>{fmt(k.last_used_at)}</td>
              <td>{k.revoked_at ? <span className="badge disabled">Revoked</span> : <span className="badge connected">Active</span>}</td>
              <td>{!k.revoked_at && <button className="danger" onClick={async () => { await api("DELETE", `/keys/${k.id}`); reload(); }}>Revoke</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/* ---------- team (admin) ---------- */
function Team() {
  const users = usePolling(() => api("GET", "/users"), 0);
  const allKeys = usePolling(() => api("GET", "/keys?all=1"), 0);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [invite, setInvite] = useState(null);
  const [err, setErr] = useState("");
  const reloadAll = () => { users.reload(); allKeys.reload(); };
  const guard = (fn) => async () => { setErr(""); try { await fn(); reloadAll(); } catch (e) { setErr(e.message); } };

  const send = async (e) => {
    e.preventDefault();
    try { const r = await api("POST", "/invites", { email, role }); setInvite(`${window.location.origin}/#/invite/${r.token}`); setEmail(""); reloadAll(); }
    catch (x) { setErr(x.message); }
  };
  return (
    <section>
      <h2>Team</h2>
      <form className="row" onSubmit={send}>
        <input type="email" placeholder="teammate@company.com" value={email} onChange={(e) => setEmail(e.target.value)} required />
        <select value={role} onChange={(e) => setRole(e.target.value)}><option>member</option><option>admin</option></select>
        <button className="primary">Create invite</button>
      </form>
      <Err message={err || users.error} />
      {invite && <div className="card highlight"><strong>Share this link (valid once, expires in 72h; no email is sent):</strong><CopyBox text={invite} /></div>}
      <table>
        <thead><tr><th>Name</th><th>Email</th><th>Role</th><th>Active keys</th><th /></tr></thead>
        <tbody>
          {users.data?.users.map((u) => (
            <tr key={u.id} className={u.disabled ? "dim" : ""}>
              <td>{u.name}</td><td>{u.email}</td>
              <td>
                <select value={u.role} onChange={(e) => guard(() => api("PATCH", `/users/${u.id}`, { role: e.target.value }))()}>
                  <option>member</option><option>admin</option>
                </select>
              </td>
              <td>{u.active_keys}</td>
              <td>
                <button onClick={guard(() => api("POST", "/keys", { user_id: u.id, name: "issued by admin" }).then((r) => setInvite(`Key for ${u.email}: ${r.secret}`)))}>Issue key</button>{" "}
                <button className={u.disabled ? "" : "danger"} onClick={guard(() => api("PATCH", `/users/${u.id}`, { disabled: !u.disabled }))}>{u.disabled ? "Enable" : "Disable"}</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>All API keys</h3>
      <table>
        <thead><tr><th>Owner</th><th>Name</th><th>Prefix</th><th>Last used</th><th>Status</th><th /></tr></thead>
        <tbody>
          {allKeys.data?.keys.map((k) => (
            <tr key={k.id}>
              <td>{k.user_email}</td><td>{k.name}</td><td><code>{k.prefix}…</code></td><td>{fmt(k.last_used_at)}</td>
              <td>{k.revoked_at ? <span className="badge disabled">Revoked</span> : <span className="badge connected">Active</span>}</td>
              <td>{!k.revoked_at && <button className="danger" onClick={guard(() => api("DELETE", `/keys/${k.id}`))}>Revoke</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

/* ---------- audit ---------- */
function Audit({ me }) {
  const [outcome, setOutcome] = useState("");
  const [offset, setOffset] = useState(0);
  const limit = 25;
  const { data, error } = usePolling(
    () => api("GET", `/audit?limit=${limit}&offset=${offset}${outcome ? `&outcome=${outcome}` : ""}`), 5000, [outcome, offset]);
  return (
    <section>
      <h2>Audit log {me.role !== "admin" && <span className="muted small">(your calls only)</span>}</h2>
      <div className="row">
        <select value={outcome} onChange={(e) => { setOffset(0); setOutcome(e.target.value); }}>
          <option value="">All outcomes</option>
          {["success", "tool_error", "timeout", "unreachable", "upstream_error", "denied", "not_found", "rate_limited", "tools_changed"].map((o) => <option key={o}>{o}</option>)}
        </select>
        <span className="muted">{data?.total ?? 0} entries</span>
      </div>
      <Err message={error} />
      <table>
        <thead><tr><th>Time</th><th>User</th><th>Via</th><th>Server</th><th>Tool</th><th>Duration</th><th>Outcome</th><th>Detail</th></tr></thead>
        <tbody>
          {data?.rows.map((r) => (
            <tr key={r.id}>
              <td>{fmt(r.ts)}</td><td>{r.user_email}</td><td>{r.source}</td><td>{r.server_slug || "—"}</td><td><code>{r.tool || "—"}</code></td>
              <td>{r.duration_ms} ms</td><td><span className={`badge ${r.outcome === "success" ? "connected" : "unreachable"}`}>{r.outcome}</span></td>
              <td className="small muted" title={r.args || ""}>{r.detail || ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="row">
        <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>Previous</button>
        <button disabled={!data || offset + limit >= data.total} onClick={() => setOffset(offset + limit)}>Next</button>
      </div>
    </section>
  );
}

/* ---------- chat ---------- */
function Chat() {
  const cfg = usePolling(() => api("GET", "/config"), 0);
  const [messages, setMessages] = useState([]); // {role, content, tool_calls?}
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const end = useRef(null);
  useEffect(() => { end.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, busy]);

  const send = async (e) => {
    e.preventDefault();
    if (!input.trim() || busy) return;
    const next = [...messages, { role: "user", content: input }];
    setMessages(next); setInput(""); setBusy(true); setError("");
    try {
      const out = await api("POST", "/chat", { messages: next.map(({ role, content }) => ({ role, content })) });
      setMessages([...next, { role: "assistant", content: out.reply, tool_calls: out.tool_calls }]);
    } catch (err) { setError(err.message); }
    finally { setBusy(false); }
  };

  return (
    <section className="chat">
      <h2>Chat agent {cfg.data && <span className="muted small">model: {cfg.data.chat_model}</span>}</h2>
      {cfg.data && !cfg.data.chat_enabled && <div className="error">No LLM API key is configured on the gateway (see README → Chat agent).</div>}
      <div className="messages">
        {!messages.length && <p className="muted">Try: “What orders came in today?” or “Create a product called Blue Mug priced 12.50”.</p>}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            <div className="bubble">{m.content}</div>
            {m.tool_calls?.length > 0 && (
              <details className="calls" open>
                <summary>{m.tool_calls.length} tool call{m.tool_calls.length > 1 ? "s" : ""}</summary>
                {m.tool_calls.map((c, j) => (
                  <div key={j} className="call">
                    <code>{c.tool}</code> <span className={`badge ${c.outcome === "success" ? "connected" : "unreachable"}`}>{c.outcome}</span>
                    {c.duration_ms != null && <span className="muted small"> {c.duration_ms} ms</span>}
                    <pre>{JSON.stringify(c.arguments)}</pre>
                    <pre className="result">{c.result}</pre>
                  </div>
                ))}
              </details>
            )}
          </div>
        ))}
        {busy && <div className="msg assistant"><div className="bubble muted">Thinking and calling tools…</div></div>}
        <div ref={end} />
      </div>
      <Err message={error} />
      <form className="row" onSubmit={send}>
        <input className="grow" value={input} onChange={(e) => setInput(e.target.value)} placeholder="Ask about your store…" />
        <button className="primary" disabled={busy}>Send</button>
      </form>
    </section>
  );
}

/* ---------- shell ---------- */
function useHash() {
  const [hash, setHash] = useState(window.location.hash);
  useEffect(() => {
    const on = () => setHash(window.location.hash);
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return hash;
}

function App() {
  const [me, setMe] = useState(undefined); // undefined = loading, null = signed out
  const hash = useHash();
  useEffect(() => { api("GET", "/me").then((r) => setMe(r.user)).catch(() => setMe(null)); }, []);

  const invite = hash.match(/^#\/invite\/(.+)$/);
  if (me === undefined) return <p className="muted pad">Loading…</p>;
  if (invite && !me) return <AcceptInvite token={invite[1]} onLogin={setMe} />;
  if (!me) return <Login onLogin={setMe} />;

  const tabs = [["servers", "Servers"], ["chat", "Chat"], ["keys", "API keys"], ...(me.role === "admin" ? [["team", "Team"]] : []), ["audit", "Audit log"]];
  const page = hash.replace(/^#\//, "").split("/")[0] || "servers";
  const logout = async () => { await api("POST", "/auth/logout"); setMe(null); window.location.hash = ""; };

  return (
    <>
      <header>
        <strong>MCP Gateway</strong>
        <nav>{tabs.map(([id, label]) => <a key={id} href={`#/${id}`} className={page === id ? "active" : ""}>{label}</a>)}</nav>
        <span className="who">{me.name} <span className="tag">{me.role}</span> <button onClick={logout}>Sign out</button></span>
      </header>
      <main>
        {page === "servers" && <Servers me={me} />}
        {page === "chat" && <Chat />}
        {page === "keys" && <Keys me={me} />}
        {page === "team" && me.role === "admin" && <Team />}
        {page === "audit" && <Audit me={me} />}
      </main>
    </>
  );
}

createRoot(document.getElementById("root")).render(<App />);
