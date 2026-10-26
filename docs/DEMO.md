# Demo runbook (definition of done, in order)

Two people: **A = admin** (dashboard + chat), **B = member** (external MCP client). Use two browser profiles, or one browser and
one terminal for B. Replace the domain if you changed it.

## 0. Show the platform (Kubernetes section of the video)
```bash
kubectl -n mcp get pods,svc,ingress,pvc          # what runs where; only wordpress/gateway/dashboard have Ingress rules
kubectl -n mcp get secret mcp-secrets -o jsonpath='{.data}' | jq 'keys'     # secrets exist, values not shown
kubectl -n mcp describe deploy mcp-gateway | grep -E "Limits|Requests|Liveness|Readiness"
kubectl -n mcp get networkpolicy
# internal-only proof (needs a CNI that enforces policy, e.g. CALICO=1):
kubectl -n mcp run probe --rm -it --image=curlimages/curl --restart=Never -- curl -m 3 -s -o /dev/null -w "%{http_code}\n" http://mcp-woo-mcp:8080/healthz
#   -> times out (000): the probe pod is not the gateway.   Without a policy-enforcing CNI you still get 401 on /mcp (bearer token).
curl -s -o /dev/null -w "%{http_code}\n" http://gateway.localtest.me/metrics    # -> dashboard HTML/404, never metrics: not routed
```

## 1. Store
1. Open http://shop.localtest.me → add a product → Checkout → *Cash on delivery* → Place order.
2. http://shop.localtest.me/wp-admin → WooCommerce → Orders: the order is there.

## 2. Chat agent as A
1. Sign in at http://gateway.localtest.me as the admin. *Servers*: WooCommerce, Clock, Notes — Connected, with their tools.
2. *Chat*: "Create a product called Blue Mug priced 12.50" → tool call `woocommerce__create_product`; refresh the storefront: it's there.
3. Buy it on the storefront by hand. *Chat*: "What orders came in today?" → `woocommerce__list_orders` returns it.

## 3. External client as B
1. *Team* → create invite for B (member) → open the link in a private window → set password. *API keys* → Generate key (copy it).
2. `claude mcp add --transport http team-gateway http://gateway.localtest.me/mcp --header "Authorization: Bearer gw_..."`
   (or MCP Inspector / Cursor). List tools: every server's tools, no upstream credentials configured anywhere on B's side.
3. Ask: "What orders came in today?" (same answer).
4. Two-server prompt: "What's the current time in Asia/Kolkata, and which orders came in since midnight there? Save a one-line summary as a note."
   → `clock__current_time`, `woocommerce__list_orders`, `notes__save_note`.

## 4. Team and control
* **Revoke B's key** (A: *Team → All API keys → Revoke*) → B's next call returns 401.
* **Disable a server** (A: *Servers → Disable* on Clock) → its tools vanish from the chat's tool panel and from B's/A's `tools/list`.
* **Kill an upstream pod:** `kubectl -n mcp delete pod -l app.kubernetes.io/component=notes-mcp` (or `scale deploy mcp-notes-mcp --replicas=0`
  to hold it down) → within ~10 s *Servers* shows **Unreachable** with the reason; calls to it return a clear error; other servers
  keep working. Scale back up → it returns to Connected on its own.
* **Audit log:** every call above, attributed to A (via=chat) or B (via=mcp), with duration and outcome (including `denied`, `unreachable`).

## 5. Extras to show
* **Tool permissions:** *Servers → Edit WooCommerce →* Admin-only tools `create_*, update_*` → as B, `create_product` disappears from the list and is rejected if called directly (audit: `denied`).
* **SSRF:** *Add server* with `http://169.254.169.254/` or `http://10.0.0.5/mcp` → rejected with the reason.
* **Malicious upstream:** describe the sanitising + threat model (`docs/THREAT_MODEL.md` §3).
* **Helm upgrade/rollback:** `helm upgrade mcp deploy/helm/mcp-platform -n mcp --reuse-values --set gateway.callTimeoutSeconds=5`; `helm history mcp -n mcp`; `helm rollback mcp 1 -n mcp`.
* **Local → VPS:** `diff deploy/helm/mcp-platform/values-local.yaml deploy/helm/mcp-platform/values-prod.yaml`.

## Troubleshooting
| Symptom | Likely cause |
|---|---|
| WordPress pod stays `1/2` | bootstrap still installing (`logs -c bootstrap`); needs egress to wordpress.org |
| `ImagePullBackOff` locally | images not loaded into kind (`kind load docker-image …`) or `images.registry` set |
| Ingress 404 / no address | `ingress.className` doesn't match the controller (nginx vs traefik) |
| WooCommerce tools return "rejected the MCP server's API credentials" | bootstrap hasn't finished, or the Secret changed after install (re-run: `kubectl rollout restart deploy/mcp-wordpress`) |
| `/wp-json/` 404 inside the cluster | permalinks not flushed; restart the WordPress pod (bootstrap re-runs `rewrite structure --hard`) |
| Chat says "not configured" | set `secrets.llmApiKey` and restart the gateway deployment |
| NetworkPolicy not blocking the probe | CNI doesn't enforce policy (plain kind); use `CALICO=1` or k3s |
