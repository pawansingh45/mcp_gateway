#!/usr/bin/env bash
# One-command local environment: kind cluster + ingress-nginx + images + Helm release.
#   LLM_API_KEY=...  scripts/local-up.sh            # chat agent enabled
#   CALICO=1         scripts/local-up.sh            # enforce NetworkPolicies (recommended for the isolation demo)
set -euo pipefail
cd "$(dirname "$0")/.."
CLUSTER=${CLUSTER:-mcp}
NS=${NS:-mcp}
RELEASE=${RELEASE:-mcp}
VERSION=$(grep '^appVersion' deploy/helm/mcp-platform/Chart.yaml | tr -d '"' | awk '{print $2}')

for bin in docker kind kubectl helm; do command -v "$bin" >/dev/null || { echo "missing: $bin"; exit 1; }; done

if ! kind get clusters | grep -qx "$CLUSTER"; then
  cfg=deploy/kind/kind-config.yaml; [ "${CALICO:-0}" = 1 ] && cfg=deploy/kind/kind-config-calico.yaml
  kind create cluster --name "$CLUSTER" --config "$cfg"
  if [ "${CALICO:-0}" = 1 ]; then
    kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/calico.yaml
    kubectl -n kube-system rollout status ds/calico-node --timeout=300s
  fi
  kubectl apply -f https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.11.2/deploy/static/provider/kind/deploy.yaml
  kubectl -n ingress-nginx wait --for=condition=ready pod -l app.kubernetes.io/component=controller --timeout=300s
fi

echo ">> building images ($VERSION)"
docker build -q -f services/gateway/Dockerfile   -t mcp-platform/gateway:"$VERSION"   .
docker build -q -f services/woo-mcp/Dockerfile   -t mcp-platform/woo-mcp:"$VERSION"   .
docker build -q -f services/ref-mcp/Dockerfile   -t mcp-platform/ref-mcp:"$VERSION"   .
docker build -q -f services/dashboard/Dockerfile -t mcp-platform/dashboard:"$VERSION" services/dashboard
for img in gateway woo-mcp ref-mcp dashboard; do kind load docker-image "mcp-platform/$img:$VERSION" --name "$CLUSTER"; done

echo ">> helm upgrade --install"
helm upgrade --install "$RELEASE" deploy/helm/mcp-platform -n "$NS" --create-namespace \
  -f deploy/helm/mcp-platform/values-local.yaml \
  ${LLM_API_KEY:+--set secrets.llmApiKey="$LLM_API_KEY"} --wait --timeout 15m

echo
echo "Storefront: http://shop.localtest.me     Dashboard: http://gateway.localtest.me"
echo "Admin password: kubectl -n $NS get secret $RELEASE-secrets -o jsonpath='{.data.gateway-admin-password}' | base64 -d; echo"
