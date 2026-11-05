from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


def _is_public_ip(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_trusted_https_url(
    url: str,
    *,
    allowed_hosts: set[str],
    resolver=socket.getaddrinfo,
) -> str:
    """
    Enforce HTTPS-only outbound calls to trusted hosts and reject SSRF targets.
    """
    parts = urlsplit(url)
    if parts.scheme.lower() != "https":
        raise ValueError("Only https scheme is allowed")
    if not parts.hostname:
        raise ValueError("URL must include a hostname")
    if parts.username or parts.password:
        raise ValueError("Credentialed URLs are not allowed")
    if parts.hostname not in allowed_hosts:
        raise ValueError(f"Untrusted destination host: {parts.hostname}")
    if parts.port not in (None, 443):
        raise ValueError("Only default HTTPS port is allowed")

    try:
        infos = resolver(parts.hostname, 443, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise ValueError(f"Host resolution failed for {parts.hostname}") from exc

    for info in infos:
        ip_text = info[4][0]
        if not _is_public_ip(ip_text):
            raise ValueError(f"Resolved non-public IP for {parts.hostname}: {ip_text}")

    return url

