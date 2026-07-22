"""Auto-fetch adapter for official Jamaican agricultural sources.

Fetches Ministry / RADA / FAOSTAT files over HTTPS and hands the bytes to the
same register -> preview -> commit ingestion pipeline used for uploads.

TLS trust
---------
Verification is routed through an explicit CA bundle: the environment's
``REQUESTS_CA_BUNDLE`` / ``SSL_CERT_FILE`` when set (so it works behind a
corporate egress proxy that re-terminates TLS), otherwise ``certifi``'s current
bundle. This fixes the common Streamlit Community Cloud failure where the
system CA store is missing/stale and government sites (e.g. moa.gov.jm) fail
with ``CERTIFICATE_VERIFY_FAILED``. Verification is never disabled.

Error honesty
-------------
Failures are *classified* (``FetchResult.error_class``) so the UI can tell the
truth: a certificate-trust failure is NOT a network block. Classes:
``cert_trust``, ``ssl``, ``timeout``, ``proxy_block``, ``network``,
``forbidden``, ``not_found``, ``http``, ``other``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse

import certifi
import requests

from ..config import settings

# Sources that are directly fetchable files (not portal landing pages).
FETCHABLE_KINDS = {"prices", "production", "area_reaped", "registry"}
_FILE_SUFFIXES = (".csv", ".pdf", ".xlsx", ".xls", ".txt")

# A realistic browser User-Agent + Accept headers. Government portals often sit
# behind bot protection (e.g. Cloudflare) that 403s unknown clients.
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "application/pdf,application/vnd.ms-excel,"
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
               "*/*;q=0.8"),
    "Accept-Language": "en-US,en;q=0.9",
}


def fetchable_sources() -> list[dict]:
    """Official sources that point at a directly downloadable file."""
    out = []
    for s in settings.OFFICIAL_SOURCES:
        url = s.get("url", "")
        if s.get("kind") in FETCHABLE_KINDS and url.lower().endswith(_FILE_SUFFIXES):
            out.append(s)
    return out


def ca_bundle() -> str:
    """Explicit CA bundle: env bundle (proxied envs) else certifi's current one."""
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        p = os.environ.get(var)
        if p and os.path.exists(p):
            return p
    return certifi.where()


@dataclass
class FetchResult:
    ok: bool
    url: str
    filename: str = ""
    content: bytes = b""
    status: int | None = None
    error: str = ""
    error_class: str = ""
    via_proxy: bool = field(default=False)

    @property
    def size(self) -> int:
        return len(self.content)


def _classify(exc: Exception) -> tuple[str, str]:
    """Map a requests exception to (error_class, human message)."""
    # SSL / certificate problems (the case this app used to mislabel).
    if isinstance(exc, requests.exceptions.SSLError):
        msg = str(exc)
        low = msg.lower()
        if ("certificate verify failed" in low or "unable to get local issuer" in low
                or "self-signed" in low or "self signed" in low):
            return ("cert_trust",
                    "TLS certificate could not be verified (the server was "
                    "reached, but its certificate chain is not trusted). This is "
                    "a CA-trust issue, not a network block. The app verifies "
                    "against certifi; if the host serves an incomplete chain, "
                    "download the file in a browser and use the Upload tab.")
        return ("ssl", f"SSL error: {msg}")
    if isinstance(exc, requests.exceptions.ProxyError):
        return ("proxy_block",
                "Blocked by the network/egress proxy (the proxy refused the "
                "tunnel). Deploy where the host is allowed, or use the Upload tab.")
    if isinstance(exc, (requests.exceptions.ConnectTimeout,
                        requests.exceptions.ReadTimeout, requests.exceptions.Timeout)):
        return ("timeout",
                "Connection timed out — the host did not respond in time "
                "(a genuine network/reachability problem).")
    if isinstance(exc, requests.exceptions.ConnectionError):
        return ("network",
                "Connection failed (DNS failure or connection refused) — the "
                "host could not be reached.")
    return ("other", str(exc))


def _via_proxy() -> bool:
    return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))


def _filename_from_url(url: str) -> str:
    return os.path.basename(urlparse(url).path) or "download"


def fetch_url(url: str, *, timeout: int = 45) -> FetchResult:
    """Download a URL via requests+certifi. Never raises."""
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=timeout,
                            verify=ca_bundle(), allow_redirects=True)
    except requests.exceptions.RequestException as exc:
        cls, msg = _classify(exc)
        return FetchResult(ok=False, url=url, error=msg, error_class=cls,
                           via_proxy=_via_proxy())
    if resp.status_code >= 400:
        cls = {403: "forbidden", 404: "not_found"}.get(resp.status_code, "http")
        note = ("Forbidden — likely bot protection or an egress policy."
                if resp.status_code == 403 else
                f"HTTP {resp.status_code} {resp.reason}")
        return FetchResult(ok=False, url=url, status=resp.status_code,
                           error=note, error_class=cls, via_proxy=_via_proxy())
    return FetchResult(ok=True, url=url, filename=_filename_from_url(url),
                       content=resp.content, status=resp.status_code,
                       via_proxy=_via_proxy())


def probe_url(url: str, *, timeout: int = 15) -> FetchResult:
    """Reachability check that does NOT download the whole file. Never raises."""
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=timeout,
                            verify=ca_bundle(), allow_redirects=True, stream=True)
        status = resp.status_code
        try:
            next(resp.iter_content(64), b"")  # touch the stream then close
        finally:
            resp.close()
    except requests.exceptions.RequestException as exc:
        cls, msg = _classify(exc)
        return FetchResult(ok=False, url=url, error=msg, error_class=cls,
                           via_proxy=_via_proxy())
    # An HTTP status (even 403/404) means the host WAS reachable at the TLS level.
    reachable = status < 400 or status in (401, 403, 404)
    cls = "" if status < 400 else {403: "forbidden", 404: "not_found"}.get(status, "http")
    return FetchResult(ok=reachable, url=url, status=status, error_class=cls,
                       error=("" if status < 400 else f"HTTP {status}"),
                       via_proxy=_via_proxy())


def check_all_sources(*, timeout: int = 15) -> list[dict]:
    """Probe every fetchable official source; return a reachability report."""
    report = []
    for s in fetchable_sources():
        res = probe_url(s["url"], timeout=timeout)
        report.append({
            "source": s["name"], "kind": s["kind"], "url": s["url"],
            "reachable": res.ok, "status": res.status,
            "error_class": res.error_class,
            "note": res.error or ("ok" if res.ok else ""),
        })
    return report


def fetch_and_preview(import_service, source: dict, *, timeout: int = 45) -> dict:
    """Fetch an official source and run it through preview (no commit).

    Returns {ok, source_file_id, is_new, preview, fetch: FetchResult}.
    """
    res = fetch_url(source["url"], timeout=timeout)
    if not res.ok:
        return {"ok": False, "fetch": res, "preview": None,
                "source_file_id": None, "is_new": False}
    kind = source.get("kind", "prices")
    provenance = source.get("provenance", "official_observed")
    sfid, is_new = import_service.register_source_file(
        res.filename, res.content, kind, origin_url=source["url"],
        provenance=provenance, source_name=source.get("name"), retrieval="fetch")
    preview = import_service.preview(res.content, res.filename, kind)
    return {"ok": True, "fetch": res, "preview": preview,
            "source_file_id": sfid, "is_new": is_new}
