"""SSRF guard for outbound machine-control requests.

The machine endpoints fetch a caller-supplied URL server-side (Moonraker). Left
open, that is a server-side request forgery vector: a caller could aim it at
cloud metadata (169.254.169.254), loopback admin ports, or internal services and
use the backend as a proxy/port-scanner. This module constrains what the server
may connect to.

Policy (most restrictive that still works wins):
  1. If an allowlist is configured (`MOONRAKER_URLS` printers and/or
     `FORGE_MACHINE_URL_ALLOWLIST`), only hosts on it are permitted — the right
     posture for a hosted/multi-tenant deployment.
  2. Otherwise, deny requests that resolve to non-global addresses. Loopback and
     private LAN ranges — where hobby printers actually live, including the very
     common "Klipper on this same box" — are permitted only when
     `FORGE_ALLOW_PRIVATE_MACHINE_URLS=1` is set, the opt-in for a self-hosted,
     single-tenant deployment.

     Link-local (169.254.0.0/16, and therefore cloud metadata at
     169.254.169.254), multicast, reserved and unspecified addresses are refused
     **always**, flag or no flag. There is no deployment in which the printer is
     the instance metadata service, so nothing legitimate is lost and the worst
     SSRF target stays shut.

     Loopback is grouped with private rather than with metadata deliberately.
     `_configured_printers()` defaults to `http://localhost:7125`, so a policy
     that refused loopback unconditionally shipped a default printer the guard
     could never allow — the Machine panel polled it and showed a bare 400 on
     every fresh install.

DNS is resolved and every resolved address is checked, so a hostname that
resolves to an internal IP (DNS-rebinding) is still rejected.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse


class UrlNotAllowed(Exception):
    """Raised when an outbound machine URL fails the SSRF policy."""


def _allowlist() -> set[str]:
    hosts: set[str] = set()
    for var in ("MOONRAKER_URLS", "FORGE_MACHINE_URL_ALLOWLIST"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        for entry in raw.split(","):
            entry = entry.strip()
            if not entry:
                continue
            # MOONRAKER_URLS entries may be "Name|url"; take the url part.
            candidate = entry.split("|", 1)[-1].strip()
            host = urlparse(candidate if "://" in candidate else f"http://{candidate}").hostname
            if host:
                hosts.add(host.lower())
    return hosts


def _allow_private() -> bool:
    return os.environ.get("FORGE_ALLOW_PRIVATE_MACHINE_URLS", "").lower() in ("1", "true", "yes")


def _resolved_addresses(host: str) -> list[ipaddress._BaseAddress]:
    infos = socket.getaddrinfo(host, None)
    addrs = []
    for info in infos:
        ip = info[4][0]
        # Strip IPv6 scope id if present.
        addrs.append(ipaddress.ip_address(ip.split("%", 1)[0]))
    return addrs


def validate_url(url: str) -> str:
    """Return the URL if it satisfies the SSRF policy, else raise UrlNotAllowed."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https"):
        raise UrlNotAllowed("only http(s) machine URLs are permitted")
    host = parsed.hostname
    if not host:
        raise UrlNotAllowed("machine URL has no host")

    allow = _allowlist()
    if allow:
        if host.lower() not in allow:
            raise UrlNotAllowed(f"host '{host}' is not in the configured printer allowlist")
        return url  # explicitly trusted target

    # No allowlist: block by resolved address class.
    try:
        addresses = _resolved_addresses(host)
    except socket.gaierror:
        raise UrlNotAllowed(f"could not resolve host '{host}'")

    allow_private = _allow_private()
    for ip in addresses:
        # Never reachable, whatever the flag says: 169.254.169.254 is link-local,
        # and no printer is the cloud metadata service.
        #
        # Loopback is held out of that class deliberately, and it is not a
        # loosening — it still has to clear the flag in the branch below, exactly
        # as 127.0.0.1 always did. `::1` is *both* loopback and, in Python's
        # `ipaddress`, `is_reserved`, because it sits inside `::/8`. Testing the
        # never-permitted class first therefore refused IPv6 loopback outright,
        # so on any dual-stack host FORGE_ALLOW_PRIVATE_MACHINE_URLS could not
        # allow `http://localhost:7125` — the printer this app ships as its
        # default, and the exact case the flag exists for. It passed on an
        # IPv4-only box, where `localhost` resolves to 127.0.0.1 alone, and
        # failed everywhere else.
        if not ip.is_loopback and (
            ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified
        ):
            raise UrlNotAllowed(
                "machine URL resolves to a link-local, multicast or reserved "
                "address; those are never permitted"
            )
        if (ip.is_loopback or ip.is_private) and not allow_private:
            where = "this server itself" if ip.is_loopback else "a private address"
            raise UrlNotAllowed(
                f"machine URL resolves to {where}, which is refused by default so "
                f"a hosted deployment cannot be used to reach its own internal "
                f"network. On a self-hosted box set "
                f"FORGE_ALLOW_PRIVATE_MACHINE_URLS=1, or list the printer in "
                f"MOONRAKER_URLS to allow it explicitly."
            )
    return url
