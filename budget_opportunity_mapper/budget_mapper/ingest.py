"""Web-first ingestion with PDF fallback.

- fetch_pdf(): downloads an official PDF on demand (only when user triggers it).
- ingest_web_pdf() / ingest_uploaded_pdf(): produce ReviewItems via extractors.
- accept_review_item() / reject_review_item(): the review-queue gate. New
  figures only enter the verified ledger after explicit acceptance.
"""
from __future__ import annotations

from typing import Optional

from .extractors import extract_lines_from_pdf
from .models import (ExtractionMethod, Ledger, ReviewItem)


def fetch_pdf(url: str, timeout: float = 60.0) -> bytes:
    """Download a PDF. Uses httpx if available, else requests."""
    try:
        import httpx
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.content
    except ImportError:
        import requests
        r = requests.get(url, timeout=timeout)
        r.raise_for_status()
        return r.content


def ingest_web_pdf(ledger: Ledger, url: str, title: str, parent_id: str,
                   max_pages: Optional[int] = None) -> list[ReviewItem]:
    data = fetch_pdf(url)
    items = extract_lines_from_pdf(
        data, source_title=title, source_url=url,
        method=ExtractionMethod.WEB_PDF, parent_id=parent_id, max_pages=max_pages)
    ledger.review_queue.extend(items)
    return items


def ingest_uploaded_pdf(ledger: Ledger, data: bytes, title: str, parent_id: str,
                        max_pages: Optional[int] = None) -> list[ReviewItem]:
    items = extract_lines_from_pdf(
        data, source_title=title, source_url=None,
        method=ExtractionMethod.UPLOADED_PDF, parent_id=parent_id, max_pages=max_pages)
    ledger.review_queue.extend(items)
    return items


def accept_review_item(ledger: Ledger, review_id: str,
                       override_parent: Optional[str] = None) -> bool:
    """Move a reviewed figure into the verified ledger. Does NOT overwrite
    existing verified nodes -- accepted items are added as new verified nodes."""
    item = next((r for r in ledger.review_queue if r.review_id == review_id), None)
    if item is None or item.status != "pending":
        return False
    node = item.proposed_node
    if override_parent:
        node.parent_id = override_parent
    node.verified = True
    ledger.nodes.append(node)
    item.status = "accepted"
    return True


def reject_review_item(ledger: Ledger, review_id: str) -> bool:
    item = next((r for r in ledger.review_queue if r.review_id == review_id), None)
    if item is None or item.status != "pending":
        return False
    item.status = "rejected"
    return True
