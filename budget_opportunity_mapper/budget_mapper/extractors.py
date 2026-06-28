"""Page-level PDF extraction (web PDF + uploaded PDF).

Uses pdfplumber as primary engine and PyMuPDF (fitz) as a fast fallback.
Every extracted figure keeps its page number and the text snippet it came from,
so provenance is preserved end-to-end. Extracted figures are returned as
ReviewItems -- they are NEVER written straight into the verified ledger.
"""
from __future__ import annotations

import io
import re
import uuid
from datetime import datetime
from typing import Optional

from .models import (ExtractionMethod, LedgerNode, NodeType, ReviewItem, Source)

# JMD amounts like "1,234,567" or "1,234,567.0"; capture with surrounding context
_AMOUNT = re.compile(r"([0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)")
_LINE = re.compile(r"^(?P<label>.+?)\s+(?P<amt>[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?)\s*$")


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def _open_pdf_text(data: bytes):
    """Yield (page_number, page_text). Try pdfplumber, fall back to PyMuPDF."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                yield i, (page.extract_text() or "")
        return
    except Exception:
        pass
    import fitz  # PyMuPDF
    doc = fitz.open(stream=data, filetype="pdf")
    for i in range(doc.page_count):
        yield i + 1, doc.load_page(i).get_text("text")


def extract_lines_from_pdf(
    data: bytes,
    source_title: str,
    source_url: Optional[str],
    method: ExtractionMethod,
    parent_id: str,
    max_pages: Optional[int] = None,
    confidence: float = 0.4,
) -> list[ReviewItem]:
    """Extract 'label  amount' lines and return them as pending ReviewItems."""
    items: list[ReviewItem] = []
    for page_no, text in _open_pdf_text(data):
        if max_pages and page_no > max_pages:
            break
        for raw in text.splitlines():
            line = raw.strip()
            m = _LINE.match(line)
            if not m:
                continue
            label = m.group("label").strip(" .:-")
            if len(label) < 3:
                continue
            amount = _to_float(m.group("amt"))
            if amount <= 0:
                continue
            src = Source(
                source_id=str(uuid.uuid4())[:8],
                title=source_title,
                url=source_url,
                page=page_no,
                quote=line[:300],
                extraction_method=method,
                confidence=confidence,
                retrieved_at=datetime.utcnow(),
            )
            node = LedgerNode(
                node_id="rev_" + str(uuid.uuid4())[:8],
                name=label[:120],
                node_type=NodeType.CHILD,
                amount=amount,
                parent_id=parent_id,
                verified=False,
                source=src,
            )
            items.append(ReviewItem(
                review_id=node.node_id,
                proposed_node=node,
                reason=f"extracted from p.{page_no} of '{source_title}'",
            ))
    return items
