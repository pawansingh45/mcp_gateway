# MCP Gateway on Kubernetes: WooCommerce hosting with a team MCP gateway

A WooCommerce store, an MCP server for it (written from scratch), a **team MCP gateway** (MCP client to every upstream *and*
an MCP server itself), a React dashboard and an LLM chat agent — deployed on Kubernetes with one Helm chart, intended for
kind locally and k3s on a VPS with **values changes only**.

* Design & tradeoffs: [`docs/SYSTEM_DESIGN.md`](docs/SYSTEM_DESIGN.md)  ·  Threat model: [`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)
* Demo runbook (maps 1:1 to the assignment's definition of done): [`docs/DEMO.md`](docs/DEMO.md)

## What I ran
I ran the project end to end on kind under WSL2, deployed with Helm. I verified these parts by hand:

* The WooCommerce store.
* The chat agent creating a product through the gateway.
* The dashboard.

Issues found while running it for real:

* WordPress needed its database environment variables.
* The `wordpress:apache` image and WordPress core update steps were needed during bootstrap.
* Reference-server probe timeouts needed tuning.
* The schema sanitizer mishandled a property named `description`.
* The LLM model and prompt needed tuning.


## Repository layout
```
libs/mcpkit/              minimal MCP server toolkit (Streamable HTTP) used by woo-mcp and ref-mcp
services/woo-mcp/         WooCommerce MCP server (REST API client + 5 tools)
services/ref-mcp/         "clock" and "notes" MCP servers (one image, REF_SERVER=clock|notes)
services/gateway/         gateway: registry, MCP client+server, REST API, auth, audit, chat agent (Flask, SQLite)
services/dashboard/       React dashboard (esbuild bundle served by unprivileged nginx)
deploy/helm/mcp-platform/ the Helm chart + values.yaml / values-local.yaml / values-prod.yaml
deploy/kind/              kind cluster configs (plain, and Calico variant to enforce NetworkPolicy)
scripts/                  local-up.sh / local-down.sh, dev_stack.py (no-Kubernetes dev mode), lint_chart.py
docs/                     system design, threat model, demo runbook
```

## Run the tests (no Docker/Kubernetes needed)
```bash
pip install -r services/gateway/requirements.txt -r services/woo-mcp/requirements.txt   # Flask, requests, cryptography
make test            # mcpkit, woo-mcp, ref-mcp, gateway end-to-end, chart lint
```

## Try it without Kubernetes (30 seconds)
```bash
make ui && python3 scripts/dev_stack.py       # needs node/npm once
# http://localhost:8080   admin@example.com / admin-password-123   (fake store + offline scripted LLM)
```

## Local Kubernetes (kind)
Prerequisites: Docker, [kind](https://kind.sigs.k8s.io), kubectl, Helm 3.

```bash
LLM_API_KEY=<your free-tier key> CALICO=1 scripts/local-up.sh
```
That creates the cluster (host ports 80/443 → ingress-nginx), builds and loads the four images, and runs
`helm upgrade --install … -f values-local.yaml`. `CALICO=1` swaps kind's default CNI for Calico, which supports NetworkPolicy
enforcement; enforcement has not yet been verified. Then:

* Storefront **http://shop.localtest.me** · Dashboard **http://gateway.localtest.me**
* **Local domain approach:** `*.localtest.me` is public wildcard DNS that resolves to `127.0.0.1`, so there is nothing to put in
  `/etc/hosts`; kind forwards host ports 80/443 to ingress-nginx. (Alternative: nip.io, or add hosts entries.)
* First boot of the store takes 3–6 minutes: `kubectl -n mcp logs deploy/mcp-wordpress -c bootstrap -f`.
* Passwords are generated; the chart's NOTES print the commands, e.g.
  `kubectl -n mcp get secret mcp-secrets -o jsonpath='{.data.gateway-admin-password}' | base64 -d`

minikube/k3d work too: set `ingress.className`, `storageClass` and `networkPolicy.ingressControllerNamespace` accordingly.

### Chat agent / LLM
Any OpenAI-compatible endpoint with tool calling. The configured Groq endpoint defaults to `openai/gpt-oss-120b`.
To select another Groq model, set `gateway.llm.model`:
```bash
helm upgrade mcp deploy/helm/mcp-platform -n mcp --reuse-values --set secrets.llmApiKey="$LLM_API_KEY" \
  --set gateway.llm.model=llama-3.3-70b-versatile
# other providers: --set gateway.llm.baseUrl=https://generativelanguage.googleapis.com/v1beta/openai --set gateway.llm.model=gemini-2.0-flash
# local model:     --set gateway.llm.baseUrl=http://host.docker.internal:11434/v1 --set gateway.llm.model=llama3.1   (Ollama)
```
The key lives only in the Kubernetes Secret (`llm-api-key`) and is injected as an env var; the browser never sees it. Without a
key the Chat page says so and everything else works.

## Using it
**Place an order:** open the storefront → add a product to the cart → Checkout → choose *Cash on delivery* → Place order. Confirm
in **WordPress admin** (`/wp-admin` → WooCommerce → Orders; admin password from the Secret).

**Chat agent:** Dashboard → *Chat*: "Create a product called Blue Mug priced 12.50", then place an order by hand, then "What orders
came in today?". The panel under each answer lists the tool calls (name, arguments, outcome, duration, result).

**External MCP client:** Dashboard → *API keys* → *Generate key* (shown once; the page prints ready-to-paste snippets):
```bash
claude mcp add --transport http team-gateway http://gateway.localtest.me/mcp --header "Authorization: Bearer gw_..."
# Cursor (~/.cursor/mcp.json):  {"mcpServers":{"team-gateway":{"url":"http://gateway.localtest.me/mcp","headers":{"Authorization":"Bearer gw_..."}}}}
npx @modelcontextprotocol/inspector      # Transport: Streamable HTTP, URL …/mcp, header Authorization: Bearer gw_...
```
Tools appear as `woocommerce__list_orders`, `clock__current_time`, `notes__save_note`, … Add `?mode=lazy` to the URL for the
search/execute pair when there are very many tools.

**Admin:** *Servers* (add/edit/disable/remove, status, tools; optional admin-only tool globs), *Team* (invite → one-time link,
disable users, issue/revoke keys), *Audit log*.

## Production-like VPS (k3s)
1. VPS with k3s (`curl -sfL https://get.k3s.io | sh -`; Traefik and `local-path` storage come with it; k3s enforces NetworkPolicy).
   Install cert-manager and a `letsencrypt-prod` ClusterIssuer if you want TLS.
2. DNS: A records for `shop.example.com` and `gateway.example.com` → VPS IP.
3. Push images: `docker build … && docker push ghcr.io/<org>/{gateway,dashboard,woo-mcp,ref-mcp}:1.0.0` (same Dockerfiles).
4. Edit `values-prod.yaml` (domains, registry, `wooVersion`, admin email), then:
```bash
helm upgrade --install mcp deploy/helm/mcp-platform -n mcp --create-namespace -f deploy/helm/mcp-platform/values-prod.yaml \
  --set secrets.llmApiKey="$LLM_API_KEY"
```
Same chart, same templates. The diff between environments is exactly `values-local.yaml` vs `values-prod.yaml`
(see the table in `docs/SYSTEM_DESIGN.md`). **Secrets strategy:** by default the chart generates strong random values once and keeps them on upgrade; for
production create the Secret out-of-band and set `existingSecret: <name>` with keys `mariadb-root-password, mariadb-password,
wp-admin-password, woo-consumer-key (ck_…), woo-consumer-secret (cs_…), mcp-woo-token, mcp-clock-token, mcp-notes-token,
gateway-encryption-key (a Fernet key), gateway-admin-password, llm-api-key`.
**Upgrade / rollback:** `helm upgrade … --set images.gateway.tag=1.0.1`; `helm history mcp -n mcp`; `helm rollback mcp <rev> -n mcp`.

## Mapping to the requirements
| Requirement | Where |
|---|---|
| Helm only, local/prod values | `deploy/helm/mcp-platform` (+ `values-local.yaml`, `values-prod.yaml`) |
| Persistent storage for DB + WP content | StatefulSet PVC (MariaDB), PVC (WordPress); gateway DB & notes PVCs |
| Ingress with stable URLs | `templates/ingress.yaml`; `shop.localtest.me`, `gateway.localtest.me` |
| Probes + requests/limits everywhere | every workload template |
| No hardcoded secrets | `templates/secret.yaml` (generated/`existingSecret`), env via `secretKeyRef` |
| WooCommerce MCP: own code, Streamable HTTP, ≥5 tools, internal only | `services/woo-mcp`, ClusterIP + NetworkPolicy + bearer token |
| Gateway: CRUD servers, one endpoint, collisions, dead/slow upstreams, credentials server-side, ≥3 upstreams | `services/gateway/gateway/{registry,service,upstream,mcp_endpoint}.py`; servers: woocommerce, clock, notes |
| Roles, per-user hashed keys, audit log | `auth.py`, `audit.py`, dashboard *Team* / *Audit log* |
| Chat agent through the gateway as the user | `chat.py` → `GatewayService.call_tool(user, …)` |

## Stand-out items attempted (and where)
* **Tool permissions** (#2): per-server `admin_only` / `deny` globs; hidden from lists *and* rejected on call (`service.py`, tested).
* **Handling many tools** (#4): `search_tools` + `execute_tool` (`?mode=lazy`; automatic in chat above 30 tools).
* **Security hardening** (#6): SSRF guard (`ssrf.py`), credentials encrypted at rest with key rotation (`security.py`), non-root
  containers/read-only FS/dropped caps, default-deny NetworkPolicies, no Kubernetes API access for any pod.
* **Scaling (partial)** (#7): per-user rate limits (`ratelimit.py`). *Not* multi-replica (see tradeoffs).
* **Observability** (#8): Prometheus `/metrics` (calls by server/outcome, latency histogram, upstream up), explicit failure reasons
  in the dashboard and audit log, `tools_changed` events.
* **Production VPS** (#1) and **Upgrades/rollback** (#9): chart + `values-prod.yaml` + documented procedure — *written, not deployed by me*.
* **Not attempted:** multi-store provisioning bonus, OAuth, hosted MCP servers.
