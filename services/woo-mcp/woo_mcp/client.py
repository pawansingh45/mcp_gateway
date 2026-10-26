"""Thin WooCommerce REST API client (wc/v3).

Design choice - REST API rather than WP-CLI or direct SQL:
  * REST is the supported, versioned public contract of WooCommerce. It runs all
    WooCommerce business logic (stock, taxes, order status hooks, emails).
  * WP-CLI would need exec access into the WordPress pod (cluster RBAC for
    pods/exec = a big privilege) and a shared filesystem.
  * Direct SQL would bypass business logic and couple us to the schema.
  * Keys are scoped (read/write) and revocable without touching WordPress.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

log = logging.getLogger("woo_mcp.client")


class WooError(Exception):
    """A failure talking to the store, with a message safe to show to a model."""


class WooClient:
    def __init__(self, base_url: str, consumer_key: str, consumer_secret: str,
                 timeout: float = 15.0, session: requests.Session | None = None):
        self.base = base_url.rstrip("/") + "/wp-json/wc/v3"
        self.auth = (consumer_key, consumer_secret)
        self.timeout = timeout
        self.http = session or requests.Session()
        # WooCommerce only accepts HTTP Basic auth over TLS. Inside the cluster we
        # talk plain HTTP to the Service, so we tell WordPress (which is configured
        # to trust this header from the pod network only - see Helm configmap) that
        # the original request was HTTPS.
        self.http.headers.update({"X-Forwarded-Proto": "https", "Accept": "application/json"})

    def request(self, method: str, path: str, params: dict | None = None, json: Any = None) -> Any:
        url = f"{self.base}/{path.lstrip('/')}"
        try:
            resp = self.http.request(method, url, params=params, json=json, auth=self.auth, timeout=self.timeout)
        except requests.Timeout as exc:
            raise WooError("The store did not respond in time.") from exc
        except requests.RequestException as exc:
            log.warning("store unreachable: %s", exc)
            raise WooError("The store is unreachable.") from exc

        if resp.status_code >= 400:
            message = _extract_message(resp)
            if resp.status_code in (401, 403):
                log.error("store rejected our API key: %s", message)
                raise WooError("The store rejected the MCP server's API credentials.")
            if resp.status_code == 404:
                raise WooError(f"Not found: {message}")
            raise WooError(f"Store error ({resp.status_code}): {message}")
        try:
            return resp.json()
        except ValueError as exc:
            raise WooError("The store returned a non-JSON response (is WooCommerce installed?).") from exc

    def get(self, path, **params):
        return self.request("GET", path, params={k: v for k, v in params.items() if v is not None})

    def post(self, path, body):
        return self.request("POST", path, json=body)

    def put(self, path, body):
        return self.request("PUT", path, json=body)


def _extract_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])[:300]
    except ValueError:
        pass
    return resp.reason or "unknown error"
