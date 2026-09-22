"""Small bounded HTTP client; DNS results are validated AND pinned per request."""
import ipaddress
import socket
import time
from urllib.parse import urlsplit, urljoin, urlencode

import certifi
import urllib3


def safe_url(url):
    if not isinstance(url, str) or any(ord(c) < 33 for c in url) or "\\" in url:
        raise ValueError("Invalid URL")
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if p.port not in (None, 80, 443):
        raise ValueError("Nonstandard port")
    host = p.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Internal host")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not public_ip(address):
        raise ValueError("Nonpublic IP")
    return url


def public_ip(ip):
    return ip.is_global and not ip.is_multicast and not ip.is_reserved and not getattr(ip, "ipv4_mapped", None)


def resolve_public(url):
    safe_url(url)
    p = urlsplit(url)
    answers = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(a[4][0] for a in answers))
    if not ips or any(not public_ip(ipaddress.ip_address(ip)) for ip in ips):
        raise ValueError("DNS resolved to nonpublic address")
    return ips


class Client:
    def __init__(self, delay=1.0):
        self.delay = delay
        self.last_request = 0.0
        self.count = 0

    def fetch(self, url, method="GET", fields=None, limit=2_000_000):
        for _ in range(6):
            ips = resolve_public(url)
            p = urlsplit(url)
            time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            self.count += 1
            options = dict(port=p.port or (443 if p.scheme == "https" else 80),
                           timeout=urllib3.Timeout(connect=4, read=7), maxsize=1)
            if p.scheme == "https":
                pool = urllib3.HTTPSConnectionPool(ips[0], server_hostname=p.hostname,
                    assert_hostname=p.hostname, cert_reqs="CERT_REQUIRED", ca_certs=certifi.where(), **options)
            else:
                pool = urllib3.HTTPConnectionPool(ips[0], **options)
            response = None
            try:
                target = (p.path or "/") + ("?" + p.query if p.query else "")
                response = pool.request(method, target,
                    body=urlencode(fields).encode() if fields else None, headers={"Host": p.netloc,
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "LocalNewsRSS/1.0 (+https://github.com; news feed metadata collector)",
                    "Accept": "text/html,application/rss+xml,application/xml;q=0.9,*/*;q=0.1",
                    "Accept-Encoding": "identity"}, redirect=False, retries=False, preload_content=False)
                if response.status in (301, 302, 303, 307, 308):
                    url = urljoin(url, response.headers.get("Location", ""))
                    if response.status in (301, 302, 303):
                        method, fields = "GET", None
                    continue
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status}")
                data = bytearray()
                start = time.monotonic()
                for chunk in response.stream(65536, decode_content=True):
                    data.extend(chunk)
                    if len(data) > limit or time.monotonic() - start > 15:
                        raise ValueError("Response limit exceeded")
                return url, bytes(data)
            finally:
                if response is not None:
                    response.close()
                pool.close()
        raise ValueError("Too many redirects")
