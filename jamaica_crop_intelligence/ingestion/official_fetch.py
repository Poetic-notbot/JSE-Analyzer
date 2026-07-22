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
import re
import tempfile
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import certifi
import requests

from ..config import settings

# Cloudflare/origin errors that are typically transient -> worth a retry.
_RETRY_STATUS = {502, 503, 504, 521, 522, 523, 524}
_SYSTEM_CA_PATHS = (
    "/etc/ssl/certs/ca-certificates.crt",   # Debian/Ubuntu (Streamlit Cloud)
    "/etc/pki/tls/certs/ca-bundle.crt",     # RHEL/CentOS
    "/etc/ssl/cert.pem",                    # Alpine/macOS
)
_CERT_BLOCK = re.compile(
    r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----", re.DOTALL)
_combined_bundle_path: str | None = None

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


def combined_ca_bundle() -> str:
    """Build a CA bundle merging certifi + the OS trust store (+ env bundles).

    Some government hosts verify against the *system* store but not certifi
    alone (e.g. data.gov.jm), while others need a *fresh* certifi that a stale
    system store lacks (e.g. moa.gov.jm). Merging both maximizes the chance of
    building a valid chain — every anchor is a legitimate CA, verification is
    never disabled. Cached to a temp file after the first build.
    """
    global _combined_bundle_path
    if _combined_bundle_path and os.path.exists(_combined_bundle_path):
        return _combined_bundle_path

    paths: list[str] = [certifi.where()]
    for var in ("REQUESTS_CA_BUNDLE", "SSL_CERT_FILE", "CURL_CA_BUNDLE"):
        p = os.environ.get(var)
        if p:
            paths.append(p)
    paths.extend(_SYSTEM_CA_PATHS)

    seen: set[str] = set()
    blocks: list[str] = []
    for p in paths:
        try:
            if p and os.path.exists(p):
                with open(p, "r", errors="ignore") as fh:
                    for block in _CERT_BLOCK.findall(fh.read()):
                        key = block.strip()
                        if key and key not in seen:
                            seen.add(key)
                            blocks.append(key)
        except OSError:
            continue
    if not blocks:
        return certifi.where()
    fd, path = tempfile.mkstemp(prefix="jci_ca_", suffix=".pem")
    with os.fdopen(fd, "w") as fh:
        fh.write("\n".join(blocks) + "\n")
    _combined_bundle_path = path
    return path


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


def _http_error(url: str, status: int, reason: str) -> FetchResult:
    if status == 403:
        cls, note = "forbidden", "Forbidden — likely bot protection or an egress policy."
    elif status == 404:
        cls, note = "not_found", "Not found (404) — the file URL may have changed."
    elif status in _RETRY_STATUS:
        cls, note = "http", (f"HTTP {status} — the origin server was temporarily "
                             "unavailable (e.g. Cloudflare 521/523). This is a "
                             "server-side transient issue; retry shortly or use "
                             "the Upload tab.")
    else:
        cls, note = "http", f"HTTP {status} {reason}"
    return FetchResult(ok=False, url=url, status=status, error=note,
                       error_class=cls, via_proxy=_via_proxy())


def fetch_url(url: str, *, timeout: int = 45, retries: int = 3) -> FetchResult:
    """Download a URL via requests + combined CA bundle, with retry/backoff on
    transient (5xx / timeout / connection) failures. Never raises.
    """
    bundle = combined_ca_bundle()
    last: FetchResult | None = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=timeout,
                                verify=bundle, allow_redirects=True)
        except requests.exceptions.SSLError as exc:
            cls, msg = _classify(exc)  # cert errors won't fix on retry -> stop
            return FetchResult(ok=False, url=url, error=msg, error_class=cls,
                               via_proxy=_via_proxy())
        except requests.exceptions.RequestException as exc:
            cls, msg = _classify(exc)
            last = FetchResult(ok=False, url=url, error=msg, error_class=cls,
                               via_proxy=_via_proxy())
            if cls in ("timeout", "network") and attempt < retries - 1:
                time.sleep(0.6 * (2 ** attempt))
                continue
            return last
        if resp.status_code in _RETRY_STATUS and attempt < retries - 1:
            last = _http_error(url, resp.status_code, resp.reason)
            time.sleep(0.6 * (2 ** attempt))
            continue
        if resp.status_code >= 400:
            return _http_error(url, resp.status_code, resp.reason)
        return FetchResult(ok=True, url=url, filename=_filename_from_url(url),
                           content=resp.content, status=resp.status_code,
                           via_proxy=_via_proxy())
    return last or FetchResult(ok=False, url=url, error="exhausted retries",
                               error_class="http", via_proxy=_via_proxy())


def probe_url(url: str, *, timeout: int = 15) -> FetchResult:
    """Reachability check that does NOT download the whole file. Never raises."""
    try:
        resp = requests.get(url, headers=_BROWSER_HEADERS, timeout=timeout,
                            verify=combined_ca_bundle(), allow_redirects=True,
                            stream=True)
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
