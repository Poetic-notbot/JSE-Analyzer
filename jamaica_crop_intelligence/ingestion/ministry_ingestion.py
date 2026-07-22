"""Resilient parser for Ministry / RADA official files.

Design goals
------------
* **Layout resilience** — Ministry workbooks change column order and headers
  between releases. We fuzzy-detect columns by keyword rather than by position,
  and never hard-crash: anything unparseable becomes a data-quality flag.
* **Crop-name normalization** — raw names are matched against canonical names
  and aliases (case/whitespace-insensitive). Unmatched names are surfaced for a
  human review step instead of being silently dropped or invented.
* **Unit mapping** — raw units are mapped to the ``units`` table; unknown units
  are surfaced for review and prices are only normalized to /kg when the unit is
  mass-convertible.
* **Duplicate prevention** — every committed row carries a deterministic
  ``record_key`` with a UNIQUE constraint, so re-committing the same file (or an
  overlapping one) is idempotent.
"""

from __future__ import annotations

import io
import re
from typing import Any

import pandas as pd

from ..calculations import core
from . import moa_weekly, validation


def _infer_price_type(header: str | None) -> str:
    """Infer price_type from a single price column's header text."""
    low = str(header or "").lower()
    for price_type, keywords in PRICE_TYPE_KEYWORDS:
        if any(kw in low for kw in keywords):
            return price_type
    return "farmgate"

# --------------------------------------------------------------------------- #
# Column keyword maps (lowercased substring match against header cells).
# --------------------------------------------------------------------------- #
COLUMN_KEYWORDS = {
    "crop": ["crop", "commodity", "product", "item", "produce"],
    "parish": ["parish"],
    "year": ["year", "yr"],
    "quarter": ["quarter", "qtr", "q"],
    "month": ["month", "mth"],
    "date": ["date", "week ending", "week-ending", "week_ending", "weekending",
             "period", "week"],
    "price": ["price", "cost", "value", "j$", "jmd", "farmgate", "rate", "$"],
    "tonnes": ["tonne", "tonnes", "production", "output", "mt", "metric ton"],
    "hectares": ["hectare", "hectares", "ha", "area", "reaped"],
    "farmers": ["farmer", "farmers", "count", "growers"],
    "unit": ["unit", "uom", "measure"],
    "market": ["market", "location", "outlet"],
}

# Price-type columns as published by the Ministry / JAMIS price chain. Order
# matters: more specific labels first so 'urban retail' beats bare 'retail'.
PRICE_TYPE_KEYWORDS = [
    ("farmgate", ["farmgate", "farm gate", "farm-gate", "farmer"]),
    ("wholesale", ["wholesale", "whole sale"]),
    ("urban_retail", ["urban retail", "urban", "kingston retail"]),
    ("rural_retail", ["rural retail", "rural"]),
    ("retail", ["retail"]),
]

QUARTER_RE = re.compile(r"q?\s*([1-4])", re.IGNORECASE)
_DATE_RE = re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})|(\d{1,2})[-/](\d{1,2})[-/](\d{4})")
_MONTHS_ABBR = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
     "nov", "dec"], start=1)}


# --------------------------------------------------------------------------- #
# Tabular loading
# --------------------------------------------------------------------------- #
def load_tabular(content: bytes, filename: str) -> tuple[pd.DataFrame, list[dict]]:
    """Load CSV/XLSX/PDF bytes into a DataFrame. Returns (df, issues)."""
    issues: list[dict] = []
    name = filename.lower()
    try:
        if name.endswith((".csv", ".txt")):
            df = pd.read_csv(io.BytesIO(content), encoding_errors="replace")
        elif name.endswith((".xlsx", ".xls", ".xlsm")):
            df = pd.read_excel(io.BytesIO(content))
        elif name.endswith(".pdf"):
            df, pdf_issues = _load_pdf(content)
            issues.extend(pdf_issues)
        else:
            # Best-effort: try CSV.
            df = pd.read_csv(io.BytesIO(content), encoding_errors="replace")
    except Exception as exc:  # noqa: BLE001 - parser resilience is the point
        issues.append({"severity": "error", "category": "parse",
                       "detail": f"Could not read file: {exc}", "raw_value": filename})
        return pd.DataFrame(), issues

    # Normalize column names to strings.
    df.columns = [str(c).strip() for c in df.columns]
    return df, issues


def _load_pdf(content: bytes) -> tuple[pd.DataFrame, list[dict]]:
    """Extract the largest table from a PDF via pdfplumber (lazy import)."""
    issues: list[dict] = []
    try:
        import pdfplumber  # noqa: PLC0415 - optional/lazy dependency
    except Exception as exc:  # noqa: BLE001
        issues.append({"severity": "error", "category": "parse",
                       "detail": f"PDF support unavailable (pdfplumber): {exc}"})
        return pd.DataFrame(), issues

    frames: list[pd.DataFrame] = []
    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables() or []:
                    if not table or len(table) < 2:
                        continue
                    header, *rows = table
                    header = [str(h or f"col{i}") for i, h in enumerate(header)]
                    frames.append(pd.DataFrame(rows, columns=header))
    except Exception as exc:  # noqa: BLE001
        issues.append({"severity": "error", "category": "parse",
                       "detail": f"PDF table extraction failed: {exc}"})
        return pd.DataFrame(), issues

    if not frames:
        issues.append({"severity": "warning", "category": "parse",
                       "detail": "No tables detected in PDF; manual mapping needed."})
        return pd.DataFrame(), issues
    # Concatenate frames with the widest column set.
    widest = max(frames, key=lambda f: f.shape[1])
    combined = pd.concat([f for f in frames if f.shape[1] == widest.shape[1]],
                         ignore_index=True)
    return combined, issues


# --------------------------------------------------------------------------- #
# Column detection
# --------------------------------------------------------------------------- #
def detect_columns(df: pd.DataFrame) -> dict[str, str | None]:
    """Map logical fields to actual column names by keyword matching."""
    mapping: dict[str, str | None] = {k: None for k in COLUMN_KEYWORDS}
    lower_cols = {col: col.lower() for col in df.columns}
    for field, keywords in COLUMN_KEYWORDS.items():
        for col, low in lower_cols.items():
            if any(kw == low or kw in low for kw in keywords):
                mapping[field] = col
                break
    return mapping


def detect_price_type_columns(df: pd.DataFrame) -> dict[str, str]:
    """Detect wide price-type columns -> {price_type: column_name}.

    Handles Ministry/JAMIS workbooks that publish farmgate / wholesale /
    urban-retail / rural-retail as separate columns in one sheet.
    """
    found: dict[str, str] = {}
    claimed: set[str] = set()
    for price_type, keywords in PRICE_TYPE_KEYWORDS:
        for col in df.columns:
            if col in claimed:
                continue
            low = str(col).lower()
            if any(kw in low for kw in keywords):
                found[price_type] = col
                claimed.add(col)
                break
    return found


def _parse_date(val: Any) -> str | None:
    """Best-effort parse of a date / week-ending cell to ISO 'YYYY-MM-DD'."""
    if val is None:
        return None
    s = str(val).strip()
    if not s or s.lower() in ("nan", "nat"):
        return None
    m = _DATE_RE.search(s)
    if m:
        if m.group(1):
            y, mo, d = m.group(1), m.group(2), m.group(3)
        else:
            d, mo, y = m.group(4), m.group(5), m.group(6)
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    # 'Mar 2024' / '2024 Mar'
    low = s.lower()
    for name, num in _MONTHS_ABBR.items():
        if name in low:
            ym = re.search(r"(20\d{2})", low)
            if ym:
                return f"{int(ym.group(1)):04d}-{num:02d}-01"
    return None


def _year_from_date(iso: str | None) -> int | None:
    if iso and len(iso) >= 4 and iso[:4].isdigit():
        return int(iso[:4])
    return None


def _quarter_from_date(iso: str | None) -> int | None:
    if iso and len(iso) >= 7 and iso[5:7].isdigit():
        return core.quarter_of_month(int(iso[5:7]))
    return None


# --------------------------------------------------------------------------- #
# Crop-name normalization
# --------------------------------------------------------------------------- #
def _norm(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lower())


def build_alias_map(repo) -> dict[str, int]:
    """Map normalized canonical names AND aliases to crop_id."""
    amap: dict[str, int] = {}
    for r in repo.query("SELECT id, canonical_name FROM crops"):
        amap[_norm(r["canonical_name"])] = r["id"]
    for r in repo.query("SELECT crop_id, alias FROM crop_aliases"):
        amap.setdefault(_norm(r["alias"]), r["crop_id"])
    return amap


def normalize_crop(raw: Any, alias_map: dict[str, int],
                   canonical: list[str]) -> tuple[int | None, str | None]:
    """Return (crop_id, suggestion). crop_id None if unmatched."""
    key = _norm(raw)
    if key in alias_map:
        return alias_map[key], None
    # Substring / token overlap suggestion for the review flow.
    suggestion = None
    for name in canonical:
        n = _norm(name)
        if key and (key in n or n in key or _token_overlap(key, n)):
            suggestion = name
            break
    return None, suggestion


def _token_overlap(a: str, b: str) -> bool:
    ta, tb = set(a.split()), set(b.split())
    return bool(ta & tb)


# --------------------------------------------------------------------------- #
# Unit mapping
# --------------------------------------------------------------------------- #
def build_unit_map(repo) -> dict[str, tuple[int, float | None]]:
    umap: dict[str, tuple[int, float | None]] = {}
    for r in repo.query("SELECT id, name, kg_equivalent FROM units"):
        umap[_norm(r["name"])] = (r["id"], r["kg_equivalent"])
    # Common synonyms.
    synonyms = {
        "kilogram": "kg", "kilograms": "kg", "kgs": "kg",
        "pound": "lb", "pounds": "lb", "lbs": "lb",
        "metric ton": "tonne", "metric tonne": "tonne", "mt": "tonne", "ton": "tonne",
    }
    for syn, target in synonyms.items():
        if _norm(target) in umap:
            umap.setdefault(_norm(syn), umap[_norm(target)])
    return umap


# --------------------------------------------------------------------------- #
# Value helpers
# --------------------------------------------------------------------------- #
def _to_float(val: Any) -> float | None:
    if val is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(val))
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_int(val: Any) -> int | None:
    f = _to_float(val)
    return int(f) if f is not None else None


def _parse_quarter(val: Any) -> int | None:
    if val is None:
        return None
    m = QUARTER_RE.search(str(val))
    if m:
        q = int(m.group(1))
        return q if 1 <= q <= 4 else None
    return None


# --------------------------------------------------------------------------- #
# Main parse -> normalized preview
# --------------------------------------------------------------------------- #
def _build_moa_price_preview(moa_df: pd.DataFrame, filename: str, repo) -> dict:
    """Normalize a MoA weekly tidy price frame into a preview payload."""
    alias_map = build_alias_map(repo)
    canonical = [r["canonical_name"] for r in
                 repo.query("SELECT canonical_name FROM crops")]
    result: dict[str, Any] = {
        "kind": "prices", "filename": filename,
        "raw_preview": moa_df.head(15), "normalized": pd.DataFrame(),
        "quality_issues": [], "crop_review": [], "unit_review": [],
        "columns_detected": {"source": "MoA weekly workbook",
                             "report_type": moa_df["price_type"].iloc[0]
                             if not moa_df.empty else None},
        "price_type_columns": {}, "summary": {},
    }
    rows: list[dict] = []
    unmatched: dict[str, str | None] = {}
    for _, r in moa_df.iterrows():
        raw_crop = r["commodity"]
        cid, suggestion = normalize_crop(raw_crop, alias_map, canonical)
        if cid is None:
            unmatched[str(raw_crop)] = suggestion
            continue
        iso = r.get("week_ending")
        rows.append({
            "crop_id": cid, "crop_raw": str(raw_crop),
            "original_crop_name": str(raw_crop),
            "year": _year_from_date(iso), "quarter": _quarter_from_date(iso),
            "parish": None, "price_date": iso, "week_ending": iso,
            "market_location": r.get("location"),
            "price_jmd": float(r["price_jmd"]),
            "price_per_kg_jmd": float(r["price_jmd"]),  # files are already J$/kg
            "price_type": r.get("price_type", "farmgate"), "confidence": 1.0,
        })
    normalized = pd.DataFrame(rows)
    normalized, vflags = validation.validate_price_rows(normalized)
    result["normalized"] = normalized
    result["quality_issues"].extend(vflags)
    result["crop_review"] = [{"raw": k, "suggestion": v}
                             for k, v in sorted(unmatched.items())]
    for item in result["crop_review"]:
        result["quality_issues"].append(
            {"severity": "warning", "category": "crop_name",
             "detail": f"Unmatched crop '{item['raw']}'"
                       + (f" (suggest: {item['suggestion']})" if item["suggestion"] else ""),
             "raw_value": item["raw"]})
    result["summary"] = {
        "rows_in_file": int(len(moa_df)),
        "rows_normalized": int(len(normalized)),
        "unmatched_crops": len(result["crop_review"]),
        "unknown_units": 0,
        "quality_issues": len(result["quality_issues"]),
    }
    return result


def parse_file(content: bytes, filename: str, kind: str, repo) -> dict:
    """Parse and normalize a file into a preview payload (nothing committed)."""
    # Special case: MoA weekly price workbooks have bespoke multi-row/merged-cell
    # layouts that the generic tabular reader cannot handle. Try them first.
    if kind == "prices" and filename.lower().endswith((".xlsx", ".xls", ".xlsm")):
        try:
            moa_df = moa_weekly.parse_workbook(content, filename)
        except Exception:  # noqa: BLE001 - fall back to generic on any failure
            moa_df = pd.DataFrame()
        if moa_df is not None and not moa_df.empty:
            return _build_moa_price_preview(moa_df, filename, repo)

    df, issues = load_tabular(content, filename)
    result: dict[str, Any] = {
        "kind": kind, "filename": filename,
        "raw_preview": df.head(15) if not df.empty else pd.DataFrame(),
        "normalized": pd.DataFrame(),
        "quality_issues": issues,
        "crop_review": [],   # [{raw, suggestion}]
        "unit_review": [],   # [raw, ...]
        "columns_detected": {},
        "summary": {},
    }
    if df.empty:
        result["quality_issues"].append(
            {"severity": "error", "category": "parse",
             "detail": "No tabular rows to normalize."})
        return result

    cols = detect_columns(df)
    result["columns_detected"] = cols
    alias_map = build_alias_map(repo)
    canonical = [r["canonical_name"] for r in
                 repo.query("SELECT canonical_name FROM crops")]
    unit_map = build_unit_map(repo)

    value_field = {"prices": "price", "production": "tonnes",
                   "area_reaped": "hectares", "registry": "farmers"}.get(kind, "price")
    if cols.get("crop") is None:
        result["quality_issues"].append(
            {"severity": "error", "category": "crop_name",
             "detail": "No crop/commodity column detected. Map columns manually."})
        return result
    if cols.get(value_field) is None and kind != "registry":
        result["quality_issues"].append(
            {"severity": "warning", "category": "range",
             "detail": f"No '{value_field}' value column detected for kind='{kind}'."})

    rows: list[dict] = []
    unmatched_crops: dict[str, str | None] = {}
    unknown_units: set[str] = set()

    date_col = cols.get("date")
    market_col = cols.get("market")
    unit_col = cols.get("unit")
    reserved = {cols.get(k) for k in
                ("crop", "date", "market", "unit", "parish", "year",
                 "quarter", "month")}
    price_type_cols: dict[str, str] = {}
    if kind == "prices":
        price_type_cols = {pt: c for pt, c in detect_price_type_columns(df).items()
                           if c not in reserved}
    result["price_type_columns"] = price_type_cols

    def _resolve_kg_eq(raw_unit_val: Any) -> float | None:
        ru = _norm(raw_unit_val)
        if ru and ru not in ("nan", ""):
            if ru in unit_map:
                return unit_map[ru][1]
            unknown_units.add(str(raw_unit_val))
        return None

    for _, r in df.iterrows():
        raw_crop = r.get(cols["crop"])
        if raw_crop is None or _norm(raw_crop) in ("", "nan", "total", "all crops"):
            continue
        crop_id, suggestion = normalize_crop(raw_crop, alias_map, canonical)
        if crop_id is None:
            unmatched_crops[str(raw_crop)] = suggestion
            continue

        iso = _parse_date(r.get(date_col)) if date_col else None
        year = _to_int(r.get(cols["year"])) if cols.get("year") else None
        if year is None:
            year = _year_from_date(iso)
        quarter = _parse_quarter(r.get(cols["quarter"])) if cols.get("quarter") else None
        if quarter is None:
            quarter = _quarter_from_date(iso)
        parish = (str(r.get(cols["parish"])).strip()
                  if cols.get("parish") and pd.notna(r.get(cols["parish"])) else None)
        market_location = (str(r.get(market_col)).strip()
                           if market_col and pd.notna(r.get(market_col)) else None)
        week_ending = (str(r.get(date_col)).strip()
                       if date_col and pd.notna(r.get(date_col)) else None)

        if year is not None and not (2000 <= year <= 2100):
            result["quality_issues"].append(
                {"severity": "warning", "category": "range",
                 "detail": f"Suspicious year {year} for {raw_crop}",
                 "raw_value": str(year)})

        base: dict[str, Any] = {
            "crop_id": crop_id, "crop_raw": str(raw_crop),
            "original_crop_name": str(raw_crop), "year": year,
            "quarter": quarter, "parish": parish, "price_date": iso,
            "week_ending": week_ending, "market_location": market_location,
        }

        if kind == "prices":
            kg_eq = _resolve_kg_eq(r.get(unit_col)) if unit_col else None
            if price_type_cols:  # wide: one row per published price type
                for price_type, pcol in price_type_cols.items():
                    price = _to_float(r.get(pcol))
                    if price is None:
                        continue
                    row = dict(base)
                    row["price_jmd"] = price
                    row["price_per_kg_jmd"] = (core.to_price_per_kg(price, kg_eq)
                                               if kg_eq else price)
                    row["price_type"] = price_type
                    row["confidence"] = 1.0 if kg_eq else 0.7
                    rows.append(row)
            else:
                price = _to_float(r.get(cols["price"])) if cols.get("price") else None
                if price is None:
                    continue
                row = dict(base)
                row["price_jmd"] = price
                row["price_per_kg_jmd"] = (core.to_price_per_kg(price, kg_eq)
                                           if kg_eq else price)
                row["price_type"] = _infer_price_type(cols.get("price"))
                row["confidence"] = 1.0 if kg_eq else 0.7
                rows.append(row)
        elif kind == "production":
            tonnes = _to_float(r.get(cols["tonnes"])) if cols.get("tonnes") else None
            if tonnes is None:
                continue
            row = dict(base)
            row["tonnes"] = tonnes
            row["confidence"] = 1.0
            rows.append(row)
        elif kind == "area_reaped":
            ha = _to_float(r.get(cols["hectares"])) if cols.get("hectares") else None
            if ha is None:
                continue
            row = dict(base)
            row["hectares"] = ha
            row["confidence"] = 1.0
            rows.append(row)
        elif kind == "registry":
            if parish is None:
                continue
            row = dict(base)
            row["farmer_count"] = _to_int(r.get(cols["farmers"])) if cols.get("farmers") else None
            row["registered_hectares"] = (_to_float(r.get(cols["hectares"]))
                                          if cols.get("hectares") else None)
            rows.append(row)

    normalized = pd.DataFrame(rows)

    # Data-quality validation (drops invalid rows, appends flags).
    if kind == "prices":
        normalized, vflags = validation.validate_price_rows(normalized)
        result["quality_issues"].extend(vflags)
    elif kind in ("production", "area_reaped"):
        vcol = "tonnes" if kind == "production" else "hectares"
        normalized, vflags = validation.validate_quantity_rows(normalized, vcol)
        result["quality_issues"].extend(vflags)

    result["normalized"] = normalized
    result["crop_review"] = [{"raw": k, "suggestion": v}
                             for k, v in sorted(unmatched_crops.items())]
    result["unit_review"] = sorted(unknown_units)

    for item in result["crop_review"]:
        result["quality_issues"].append(
            {"severity": "warning", "category": "crop_name",
             "detail": f"Unmatched crop '{item['raw']}'"
                       + (f" (suggest: {item['suggestion']})" if item["suggestion"] else ""),
             "raw_value": item["raw"]})
    for u in result["unit_review"]:
        result["quality_issues"].append(
            {"severity": "warning", "category": "unit",
             "detail": f"Unknown unit '{u}' - price not normalized to /kg",
             "raw_value": u})

    result["summary"] = {
        "rows_in_file": int(len(df)),
        "rows_normalized": int(len(normalized)),
        "unmatched_crops": len(result["crop_review"]),
        "unknown_units": len(result["unit_review"]),
        "quality_issues": len(result["quality_issues"]),
    }
    return result


# --------------------------------------------------------------------------- #
# Commit
# --------------------------------------------------------------------------- #
def _record_key(kind: str, row: dict) -> str:
    cid = row["crop_id"]
    y = row.get("year")
    q = row.get("quarter")
    parish = row.get("parish") or "allisland"
    if kind == "prices":
        # Include date/week and market so weekly rows in the same quarter and
        # different markets do not collapse into one another.
        when = row.get("price_date") or row.get("week_ending") or f"{y}Q{q}"
        market = row.get("market_location") or "na"
        return (f"price:{cid}:{y}:{q}:{row.get('price_type', 'farmgate')}:"
                f"{parish}:{market}:{when}")
    if kind == "production":
        return f"prod:{cid}:{y}:{q}:{parish}"
    if kind == "area_reaped":
        return f"area:{cid}:{y}:{q}:{parish}"
    if kind == "registry":
        return f"reg:{cid}:{parish}"
    return f"{kind}:{cid}:{y}:{q}:{parish}"


def commit_rows(normalized: pd.DataFrame, kind: str, source_file_id: int,
                repo) -> int:
    """Insert normalized rows into fact tables, deduped by record_key.

    Returns the number of rows actually committed (INSERT OR IGNORE means
    duplicates are skipped, keeping the operation idempotent).
    """
    if normalized is None or normalized.empty:
        return 0
    committed = 0
    for _, r in normalized.iterrows():
        row = r.to_dict()
        key = _record_key(kind, row)
        before = repo.table_count(_TABLE_FOR_KIND[kind])
        if kind == "prices":
            repo.insert("price_records", {
                "crop_id": row["crop_id"], "parish": row.get("parish"),
                "year": row.get("year"), "quarter": row.get("quarter"),
                "month": row.get("month"),
                "price_jmd": row["price_jmd"],
                "price_per_kg_jmd": row.get("price_per_kg_jmd"),
                "price_type": row.get("price_type", "farmgate"),
                "price_date": row.get("price_date"),
                "week_ending": row.get("week_ending"),
                "market_location": row.get("market_location"),
                "original_crop_name": row.get("original_crop_name"),
                "confidence": row.get("confidence", 1.0),
                "provenance": "official_observed",
                "source_file_id": source_file_id, "record_key": key,
            }, or_ignore=True)
        elif kind == "production":
            repo.insert("production_records", {
                "crop_id": row["crop_id"], "parish": row.get("parish"),
                "year": row.get("year"), "quarter": row.get("quarter"),
                "tonnes": row["tonnes"],
                "original_crop_name": row.get("original_crop_name"),
                "confidence": row.get("confidence", 1.0),
                "provenance": "official_observed",
                "source_file_id": source_file_id, "record_key": key,
            }, or_ignore=True)
        elif kind == "area_reaped":
            repo.insert("area_reaped_records", {
                "crop_id": row["crop_id"], "parish": row.get("parish"),
                "year": row.get("year"), "quarter": row.get("quarter"),
                "hectares": row["hectares"],
                "original_crop_name": row.get("original_crop_name"),
                "confidence": row.get("confidence", 1.0),
                "provenance": "official_observed",
                "source_file_id": source_file_id, "record_key": key,
            }, or_ignore=True)
        elif kind == "registry":
            repo.insert("parish_crop_registry", {
                "crop_id": row["crop_id"], "parish": row.get("parish"),
                "farmer_count": row.get("farmer_count"),
                "registered_hectares": row.get("registered_hectares"),
                "provenance": "official_registry",
                "source_file_id": source_file_id, "record_key": key,
            }, or_ignore=True)
        after = repo.table_count(_TABLE_FOR_KIND[kind])
        if after > before:
            committed += 1
    return committed


_TABLE_FOR_KIND = {
    "prices": "price_records",
    "production": "production_records",
    "area_reaped": "area_reaped_records",
    "registry": "parish_crop_registry",
}
