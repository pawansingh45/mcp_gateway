"""Server-side request forgery guard for admin-supplied upstream URLs.

An admin (or an attacker with a stolen admin session) can make the gateway issue
HTTP requests from inside the cluster. This module limits where.

Policy
  * http/https only, no embedded credentials in the URL.
  * ALWAYS blocked: loopback (unless explicitly enabled for tests), link-local
    (169.254.0.0/16 - the cloud metadata range - and fe80::/10), unspecified,
    multicast, reserved, and well-known metadata hostnames.
  * Private ranges (10/8, 172.16/12, 192.168/16, CGNAT, fc00::/7 - where pods and
    Services live) are blocked UNLESS the hostname matches the operator-controlled
    allow-list (SSRF_ALLOWED_HOSTS, set via Helm values), e.g. `woo-mcp`.
  * Every address a hostname resolves to must pass (defends against a name that
    has one public and one internal A record).
  * The check re-runs whenever an upstream session is (re)established, which
    narrows (does not fully close) DNS-rebinding windows; redirects are never
    followed by the upstream client.
"""
from __future__ import annotations

import fnmatch
import ipaddress
import socket
from urllib.parse import urlsplit

_METADATA_HOSTS = {"metadata.google.internal", "metadata", "instance-data", "metadata.azure.com"}
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


class SsrfError(ValueError):
    pass


def _host_allowed(host: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(host, p) for p in patterns)


def _resolve(host: str) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfError(f"cannot resolve host '{host}'") from exc
    return [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]


def check_url(url: str, allowed_hosts: tuple[str, ...] = (), allow_loopback: bool = False,
              resolver=_resolve) -> None:
    """Raise SsrfError if the URL must not be contacted."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise SsrfError("URL must start with http:// or https://")
    if not parts.hostname:
        raise SsrfError("URL has no host")
    if parts.username or parts.password:
        raise SsrfError("credentials must not be embedded in the URL; use the credential field")
    host = parts.hostname.lower().rstrip(".")
    if host in _METADATA_HOSTS:
        raise SsrfError("cloud metadata endpoints are not allowed")

    explicitly_allowed = _host_allowed(host, allowed_hosts)
    for ip in resolver(host):
        if ip.is_loopback:
            if not allow_loopback:
                raise SsrfError("loopback addresses are not allowed")
            continue
        if ip.is_link_local or ip.is_unspecified or ip.is_multicast or ip.is_reserved:
            raise SsrfError(f"address {ip} is not allowed (link-local/metadata/reserved)")
        private = ip.is_private or (ip.version == 4 and ip in _CGNAT)
        if private and not explicitly_allowed:
            raise SsrfError(f"host '{host}' resolves to private address {ip}; "
                            "ask the operator to add it to gateway.ssrf.allowedHosts")
