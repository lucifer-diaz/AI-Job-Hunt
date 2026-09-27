"""Which URLs Job Hunt OS may fetch on a user's behalf: public http(s) hosts only.

A posting URL comes from a job board or a user, so it is untrusted. Reading it must never reach the machine's own
network (localhost, the cloud metadata address, a private database). Every page fetch — the "Fetch from posting"
button, job-description recovery over HTTP and in the browser — checks the URL here first, and the browser checks
each navigation again, so a redirect cannot lead somewhere the first URL could not.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURL(ValueError):
    """The URL is not a public http(s) address. The message is safe to show to a user."""


def check_public_url(url: str, *, allow_private: bool = False) -> None:
    """Raise UnsafeURL unless `url` is an http(s) URL whose host resolves only to public addresses."""
    parsed = urlparse(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeURL("Enter a full http(s) job URL")
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(parsed.hostname, None)
    except socket.gaierror as exc:
        raise UnsafeURL("That host could not be resolved") from exc
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            raise UnsafeURL("Private network addresses are not allowed")


INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def obviously_private(url: str) -> bool:
    """A cheap, DNS-free check for every browser sub-request: an IP literal in a private range, localhost, or an
    internal-only domain. Navigations get the full check_public_url (with DNS) as well."""
    host = (urlparse(url or "").hostname or "").lower().strip("[]")
    if not host:
        return False
    if host == "localhost" or host.endswith(INTERNAL_SUFFIXES):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved
