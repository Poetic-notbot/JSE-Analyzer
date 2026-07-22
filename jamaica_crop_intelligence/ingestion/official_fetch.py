"""Auto-fetch adapter for official Jamaican agricultural sources.

Fetches Ministry / RADA / (optionally JAMIS) files over HTTPS and hands the bytes
to the same register -> preview -> commit ingestion pipeline used for uploads.

Environment behaviour
---------------------
* Works **directly** where the network permits (e.g. Streamlit Community Cloud).
* Honours an HTTP(S) proxy from the environment (``HTTPS_PROXY``) and a CA bundle
  (``SSL_CERT_FILE`` / ``REQUESTS_CA_BUNDLE``) when present, so it also works
  behind a corporate egress proxy.
* **Fails gracefully**: if a host is blocked by policy (e.g. this build sandbox
  returns HTTP 403 at the proxy for moa.gov.jm / data.gov.jm), ``fetch_url``
  returns ``ok=False`` with the reason instead of raising. Nothing is fabricated.
"""

from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..config import settings

# Sources that are directly fetchable files (not portal landing pages).
FETCHABLE_KINDS = {"prices", "production", "area_reaped", "registry"}
_FILE_SUFFIXES = (".csv", ".pdf", ".xlsx", ".xls", ".txt")


def fetchable_sources() -> list[dict]:
    """Official sources that point at a directly downloadable file."""
    out = []
    for s in settings.OFFICIAL_SOURCES:
        url = s.get("url", "")
        if s.get("kind") in FETCHABLE_KINDS and url.lower().endswith(_FILE_SUFFIXES):
            out.append(s)
    return out


@dataclass
class FetchResult:
    ok: bool
    url: str
    filename: str = ""
    content: bytes = b""
    status: int | None = None
    error: str = ""
    via_proxy: bool = field(default=False)

    @property
    def size(self) -> int:
        return len(self.content)


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ca = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    if ca and os.path.exists(ca):
        try:
            ctx.load_verify_locations(ca)
        except Exception:  # noqa: BLE001 - fall back to system trust
            pass
    return ctx


def _filename_from_url(url: str) -> str:
    name = os.path.basename(urlparse(url).path) or "download"
    return name


def fetch_url(url: str, *, timeout: int = 45) -> FetchResult:
    """Download a URL. Never raises — returns a FetchResult with ok/error."""
    proxies = urllib.request.getproxies()  # reads HTTP(S)_PROXY
    handlers: list = []
    via_proxy = False
    if proxies:
        handlers.append(urllib.request.ProxyHandler(proxies))
        via_proxy = True
    handlers.append(urllib.request.HTTPSHandler(context=_ssl_context()))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url, headers={"User-Agent": "JamaicaCropIntelligence/1.0 (+ingestion)"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            content = resp.read()
            return FetchResult(ok=True, url=url, filename=_filename_from_url(url),
                               content=content, status=getattr(resp, "status", 200),
                               via_proxy=via_proxy)
    except urllib.error.HTTPError as exc:
        return FetchResult(ok=False, url=url, status=exc.code, via_proxy=via_proxy,
                           error=f"HTTP {exc.code} {exc.reason}")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        msg = str(reason)
        if "403" in msg or "tunnel" in msg.lower() or "forbidden" in msg.lower():
            msg = (f"{msg} - host likely blocked by the network/egress policy. "
                   "Download the file in a browser and upload it instead.")
        return FetchResult(ok=False, url=url, via_proxy=via_proxy, error=msg)
    except Exception as exc:  # noqa: BLE001 - adapter must never crash the app
        return FetchResult(ok=False, url=url, via_proxy=via_proxy, error=str(exc))


def fetch_and_preview(import_service, source: dict, *, timeout: int = 45) -> dict:
    """Fetch an official source and run it through preview (no commit).

    Returns a dict:
      {ok, source_file_id, is_new, preview, fetch: FetchResult}
    On fetch failure, ok=False and preview is None.
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
