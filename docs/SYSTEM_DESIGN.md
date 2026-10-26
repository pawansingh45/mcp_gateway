# System design and tradeoffs

## Architecture

```
                      Internet / laptop
                            │
              ┌─────────────┴─────────────┐   Ingress (nginx locally, Traefik on k3s)
              │ shop.<domain>             │ gateway.<domain>
              ▼                           ▼
        ┌───────────┐          /api,/mcp ─►┌──────────────────────────┐   / ─► ┌───────────┐
        │ WordPress │                      │ GATEWAY (Flask)          │        │ Dashboard │
        │+WooCommerce◄─┐                   │  • MCP server  (/mcp)    │        │ React+nginx│
        └─────┬─────┘  │ REST wc/v3        │  • MCP client → upstreams│        └───────────┘
              │        │ (API key,         │  • REST API   (/api)     │
        ┌─────▼─────┐  │  in-cluster)      │  • chat agent (LLM loop) │──► LLM provider (HTTPS)
        │  MariaDB  │  │                   │  • audit, users, keys    │
        └───────────┘  │                   │  SQLite on PVC           │
                 ┌─────┴──────┐            └───────┬───────┬──────────┘
                 │ woo-mcp    │◄───────────────────┘       │   Streamable HTTP + per-upstream bearer
                 │ clock-mcp  │◄───────────────────────────┤   ClusterIP only, NetworkPolicy: from gateway only
                 │ notes-mcp  │◄───────────────────────────┘
                 └────────────┘
```

| Component | Responsibility | Public? |
|---|---|---|
| WordPress + WooCommerce | The store. Content on a PVC; MariaDB StatefulSet with its own PVC. | Yes: `shop.<domain>` |
| `woo-mcp` | MCP server I wrote (Flask + `mcpkit`). Tools: `list_products`, `create_product`, `list_orders`, `get_order`, `update_order_status`. | **No** (ClusterIP, NetworkPolicy, bearer token) |
| `clock-mcp`, `notes-mcp` | Two more upstreams (time/timezones; stateful notes). Give the "two servers in one prompt" demo and prove the gateway is not WooCommerce-specific. | No |
| Gateway | Registry + MCP client to every upstream; **is itself an MCP server**; REST API; auth; audit; chat agent. | `/api` and `/mcp` only |
| Dashboard | Static React bundle on unprivileged nginx. | Yes: `/` on the gateway host |

## How WooCommerce MCP talks to the store
**WooCommerce REST API (`/wp-json/wc/v3`) with a dedicated read/write API key.** Alternatives considered:
* *WP-CLI*: needs `pods/exec` into WordPress (a large cluster privilege) and a shared filesystem; output is stringly-typed.
* *Direct SQL*: bypasses WooCommerce business logic (stock, tax, hooks, emails) and couples us to the schema.
* *REST*: the supported, versioned contract; keys are scoped and revocable independently of WordPress.

WooCommerce only accepts Basic-auth API keys over HTTPS. Inside the cluster we speak HTTP to the Service, so WordPress is
configured (`WORDPRESS_CONFIG_EXTRA`) to honour `X-Forwarded-Proto: https`, which the MCP server sends. At the public edge the
Ingress overwrites that header, so internet clients cannot spoof it.

## Store bootstrap (no manual wizard)
A **sidecar** in the WordPress pod (`wordpress:cli`) shares the WordPress volume and runs an idempotent script: install core,
set timezone/permalinks, install WooCommerce (version pinnable), enable Cash-on-Delivery, create shop/cart/checkout pages, add a
free-shipping method (physical products otherwise cannot be checked out), and insert the REST key whose value comes from the
Kubernetes Secret (so the MCP server and the store agree without any manual step). The sidecar's readiness probe gates the Pod, so
the Service only receives traffic once the store is fully installed. A sidecar was chosen over a Job to avoid RWO-volume
co-scheduling problems and over an initContainer because it needs the files the WordPress entrypoint creates.

## Tool aggregation, routing, naming
* The gateway keeps, per enabled upstream, a **cached catalog** refreshed by a background health loop (default 10 s) and
  immediately on create/edit/enable. `tools/list` reads only this cache.
* **Naming:** every tool is exposed as `<server-slug>__<tool>`; e.g. `clock__current_time` and `clock2__current_time` coexist.
  Always prefixing (not only on collision) means a name never changes when another server is added or removed. Slugs are immutable,
  limited to `[a-z][a-z0-9-]{0,23}`; names are mapped to `[A-Za-z0-9_-]` and kept ≤ 64 chars (hash suffix when shortened).
* **Routing:** `tools/call` looks the public name up in the catalog → (server, original tool name) → upstream `tools/call`.
* **Many tools:** `?mode=lazy` on the MCP URL (and automatic in the chat agent above 30 tools) exposes only `search_tools` and
  `execute_tool`; the model searches the catalog, then executes by name. Calls are audited as the *real* server/tool.

## Failure handling
| Situation | Behaviour |
|---|---|
| Upstream dead / pod killed | Next health tick (≤10 s) or first failed call marks it **Unreachable**; its tools are dropped from `tools/list`; calls to it return a clear tool error immediately; every other server is untouched. Recovery is automatic (tested: kill + restart on the same address). |
| Upstream slow | Per-call hard deadline (`CALL_TIMEOUT_SECONDS`, default 20 s) covering connect, headers, body *and* SSE streaming. Returns "did not answer within Ns"; outcome `timeout`. A timeout alone does not mark the server down. |
| Listing while an upstream hangs | Impossible by construction: listing never contacts upstreams. |
| Hostile upstream | No redirects followed, response size capped (2 MB), non-text content replaced by a placeholder, descriptions/schemas length-capped and control-char-stripped, annotations whitelisted. |
| Upstream session expires (404) | Client re-initialises once and retries. |
| Changed tool definitions | Detected by hash on refresh and written to the audit log (`tools_changed`) — a cheap "rug-pull" alarm. |

## Transport choices
* Streamable HTTP, JSON responses only (no SSE from *our* servers) and **stateless** (no `Mcp-Session-Id`): any replica can
  serve any request; nothing to garbage-collect. The upstream *client* does support SSE replies and sessions, because other
  people's servers use them.
* `GET /mcp` returns 405 (we never push server-initiated messages); `Origin` is validated (spec's DNS-rebinding guard).

## Chat agent
`ChatAgent` is an OpenAI-compatible tool loop (works with Groq/Gemini/OpenRouter/OpenAI/Ollama). It calls
`GatewayService.call_tool(user, …, source="chat")` — **the same function `/mcp` uses** — so it has no privileges of its own:
the user's role policy, rate limit, timeouts and audit apply identically, and audit rows show `source=chat` under the
logged-in user. It is bounded (8 steps), tolerates malformed model arguments, feeds errors back to the model, and tells the model
that tool output is untrusted. The UI shows every call, its arguments, outcome, duration and a result preview.

## Kubernetes layout
One umbrella Helm chart (`deploy/helm/mcp-platform`), one namespace. Secrets in one Secret (generated once with `lookup`,
reused on upgrade, `resource-policy: keep`; or bring your own via `existingSecret`). PVCs: MariaDB, WordPress, gateway DB, notes
(the three non-DB PVCs carry `resource-policy: keep` so `helm uninstall`/rollback cannot delete data). Every workload has
readiness + liveness probes and requests/limits. Python services and nginx run non-root with `drop: [ALL]`, no privilege
escalation, `RuntimeDefault` seccomp; Python services also have a read-only root filesystem. NetworkPolicies: default-deny
ingress plus exactly the edges in the diagram.

## Local → production (what changes, all in values files)
| | `values-local.yaml` | `values-prod.yaml` |
|---|---|---|
| DNS | `*.localtest.me` → 127.0.0.1 (public wildcard DNS, no hosts-file edits) | your A records → VPS IP |
| Ingress | `nginx`, http, kind port mapping 80/443 | `traefik` (k3s default), https, cert-manager `clusterIssuer` |
| Storage | cluster default (`standard`) | `local-path` (k3s) or a real class; bigger PVCs |
| Secrets | generated by the chart | `existingSecret` created out-of-band (External Secrets/SOPS) |
| Images | `kind load docker-image` | `images.registry` + pushed images; pin WooCommerce/WordPress tags |
| Other | `cookieSecure: false` | `cookieSecure: true`, bigger resources, NetworkPolicy namespace `kube-system` |

**Upgrade / rollback:** `helm upgrade` with a new image tag; `helm history`, `helm rollback <rev>`. Because stateful objects are
PVC-backed with `keep`, rollback changes only code/config. Gateway and WordPress use `Recreate` (RWO volumes), so an upgrade has a
short outage by design. Schema changes in the gateway are additive (`CREATE … IF NOT EXISTS`); a real migration tool is the next step.

## Tradeoffs and known limits (honest list)
1. **Gateway is a single replica.** SQLite on a RWO PVC, plus in-memory health state, rate-limit buckets and login throttle.
   Scale-out path: Postgres (all SQL is in `db.py`/`auth.py`/`audit.py`/`registry.py`), upstream state recomputed per replica
   (already stateless MCP), rate limits in Redis or accepted as per-replica.
2. **No OAuth** on the MCP endpoint (API keys only; spec-compliant 401 + `WWW-Authenticate`).
3. **No email**: invites are links the admin shares.
4. **Tool results are text-only**; images/audio/resources are replaced by a placeholder (deliberate safety trade-off).
5. **WordPress container runs as root** (official image requirement: port 80, file ownership). The bootstrap sidecar and everything
   we wrote run as non-root. A first boot needs egress to wordpress.org to fetch WooCommerce.
6. **Egress is not restricted** by NetworkPolicy (only ingress); LLM and wordpress.org need internet.
7. **No multi-store provisioning bonus** (not attempted; the core is the priority).
