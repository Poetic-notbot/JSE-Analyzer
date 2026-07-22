"""FAOSTAT adapter — Jamaica annual crop production & area (open REST API).

FAOSTAT is the cleanest programmatic source: a real REST API returning flat JSON
records, no PDF/xlsx wrangling. This adapter fetches the QCL (Crops & Livestock
Products) domain for Jamaica (area code 109), normalizes item names to the app's
crop taxonomy, and produces a ministry-style preview so the existing
approve/commit + lineage + dedup flow applies unchanged.

Annual vs quarterly: FAOSTAT is **annual**. Rows are committed with quarter=NULL
and are surfaced as annual observations (never presented as monthly output).
"""

from __future__ import annotations

import json
from typing import Any

import pandas as pd

from ..calculations import core  # noqa: F401  (kept for parity/future use)
from . import validation
from .ministry_ingestion import _norm, build_alias_map, normalize_crop

FAOSTAT_BASE = "https://fenixservices.fao.org/faostat/api/v1/en/data/QCL"
JAMAICA_AREA = 109

ELEMENTS = {
    "production": 5510,   # Production quantity (tonnes)
    "area": 5312,         # Area harvested (ha)
    "yield": 5419,        # Yield (kg/ha) — not stored as a fact (no yield table)
}
ELEMENT_TO_KIND = {"production": "production", "area": "area_reaped"}

# FAOSTAT item names -> app canonical crop names. FAOSTAT uses aggregate/botanical
# labels that won't match our Jamaican names by default; this bridges the common
# ones. Unmatched items are surfaced for review, never invented.
# Keys are FAOSTAT item names AFTER normalization (lowercased, whitespace
# collapsed) exactly as they appear in the Jamaica QCL bulk CSV / API.
FAOSTAT_ITEM_TO_CANON = {
    "yams": "Yellow Yam",
    "sweet potatoes": "Sweet Potato",
    "potatoes": "Irish Potato",
    "cassava, fresh": "Cassava",
    "cassava": "Cassava",
    "tomatoes": "Tomato",
    "cabbages": "Cabbage",
    "carrots and turnips": "Carrot",
    "lettuce and chicory": "Lettuce",
    "cucumbers and gherkins": "Cucumber",
    "pumpkins, squash and gourds": "Pumpkin",
    "chillies and peppers, green (capsicum spp. and pimenta spp.)": "Sweet Pepper",
    "chillies and peppers, green": "Sweet Pepper",
    "chillies and peppers, dry (capsicum spp., pimenta spp.), raw": "Scotch Bonnet Pepper",
    "onions and shallots, green": "Escallion",
    "onions and shallots, dry (excluding dehydrated)": "Onion",
    "plantains and cooking bananas": "Plantain",
    "bananas": "Banana",
    "pineapples": "Pineapple",
    "watermelons": "Watermelon",
    "mangoes, guavas and mangosteens": "Mango",
    "papayas": "Papaya",
    "maize (corn)": "Corn",
    "ginger, raw": "Ginger",
    "string beans": "String Bean",
    "pigeon peas, dry": "Gungo Peas",
    "beans, dry": "Red Peas",
}

# FAOSTAT Element -> our fact-table kind (+ value column).
FAOSTAT_ELEMENT_TO_KIND = {
    "production": ("production", "tonnes"),
    "area harvested": ("area_reaped", "hectares"),
}


def build_url(element_key: str, years: list[int] | None = None,
              output_objects: bool = True) -> str:
    element = ELEMENTS[element_key]
    params = [f"area={JAMAICA_AREA}", f"element={element}"]
    if years:
        params.append("year=" + ",".join(str(y) for y in years))
    if output_objects:
        params.append("output_type=objects")
    return f"{FAOSTAT_BASE}?{'&'.join(params)}"


def parse_json(content: bytes) -> list[dict]:
    """Extract the list of records from a FAOSTAT API response."""
    obj = json.loads(content.decode("utf-8", errors="replace"))
    if isinstance(obj, dict) and "data" in obj:
        return obj["data"]
    if isinstance(obj, list):
        return obj
    return []


def _get(rec: dict, *keys: str) -> Any:
    for k in keys:
        if k in rec and rec[k] not in (None, ""):
            return rec[k]
    return None


def _resolve_crop(raw_item: str, alias_map, canonical) -> tuple[int | None, str | None]:
    override = FAOSTAT_ITEM_TO_CANON.get(_norm(raw_item))
    if override:
        cid = alias_map.get(_norm(override))
        if cid:
            return cid, None
    return normalize_crop(raw_item, alias_map, canonical)


def build_preview(records: list[dict], element_key: str, repo) -> dict:
    """Turn FAOSTAT records into a ministry-style preview dict for one element."""
    kind = ELEMENT_TO_KIND.get(element_key)
    result: dict[str, Any] = {
        "kind": kind, "filename": f"faostat_{element_key}.json",
        "raw_preview": pd.DataFrame(records[:15]),
        "normalized": pd.DataFrame(), "quality_issues": [],
        "crop_review": [], "unit_review": [], "columns_detected": {"source": "FAOSTAT"},
        "price_type_columns": {}, "summary": {},
    }
    if kind is None:
        result["quality_issues"].append(
            {"severity": "warning", "category": "parse",
             "detail": f"FAOSTAT element '{element_key}' is not stored as a fact "
                       "table (e.g. yield); skipped."})
        return result

    alias_map = build_alias_map(repo)
    canonical = [r["canonical_name"] for r in
                 repo.query("SELECT canonical_name FROM crops")]
    value_col = "tonnes" if kind == "production" else "hectares"

    rows: list[dict] = []
    unmatched: dict[str, str | None] = {}
    for rec in records:
        item = _get(rec, "Item", "item")
        year = _get(rec, "Year", "year")
        value = _get(rec, "Value", "value")
        if item is None or year is None or value is None:
            continue
        cid, suggestion = _resolve_crop(str(item), alias_map, canonical)
        if cid is None:
            unmatched[str(item)] = suggestion
            continue
        try:
            yr = int(float(year))
            val = float(value)
        except (TypeError, ValueError):
            continue
        rows.append({
            "crop_id": cid, "crop_raw": str(item), "original_crop_name": str(item),
            "year": yr, "quarter": None, "parish": None,
            value_col: val, "confidence": 1.0,
        })

    normalized = pd.DataFrame(rows)
    normalized, vflags = validation.validate_quantity_rows(normalized, value_col)
    result["normalized"] = normalized
    result["quality_issues"].extend(vflags)
    result["crop_review"] = [{"raw": k, "suggestion": v} for k, v in sorted(unmatched.items())]
    for item in result["crop_review"]:
        result["quality_issues"].append(
            {"severity": "warning", "category": "crop_name",
             "detail": f"Unmatched FAOSTAT item '{item['raw']}'"
                       + (f" (suggest: {item['suggestion']})" if item["suggestion"] else ""),
             "raw_value": item["raw"]})
    result["summary"] = {
        "rows_in_file": len(records),
        "rows_normalized": int(len(normalized)),
        "unmatched_crops": len(result["crop_review"]),
        "unknown_units": 0,
        "quality_issues": len(result["quality_issues"]),
    }
    return result


_FAOSTAT_CSV_SIGNATURE = {"item", "element", "value"}


def is_faostat_csv(df: pd.DataFrame) -> bool:
    """Detect a FAOSTAT bulk CSV by its signature columns."""
    cols = {str(c).strip().lower() for c in df.columns}
    return _FAOSTAT_CSV_SIGNATURE.issubset(cols) and (
        "area" in cols or any("area" in c for c in cols))


def build_preview_from_df(df: pd.DataFrame, repo) -> dict:
    """Build a ministry-style preview from a FAOSTAT bulk CSV DataFrame.

    Handles a mixed CSV (Production + Area harvested + Yield + livestock): only
    Production and Area-harvested rows for mapped crops are normalized, each
    tagged with a ``target_kind`` so commit routes it to the right fact table.
    Yield and livestock rows are ignored.
    """
    colmap = {str(c).strip().lower(): c for c in df.columns}
    item_c = colmap.get("item")
    elem_c = colmap.get("element")
    year_c = colmap.get("year") or colmap.get("year code")
    value_c = colmap.get("value")

    result: dict = {
        "kind": "production", "filename": "faostat.csv",
        "raw_preview": df.head(15), "normalized": pd.DataFrame(),
        "quality_issues": [], "crop_review": [], "unit_review": [],
        "columns_detected": {"source": "FAOSTAT bulk CSV", "item": item_c,
                             "element": elem_c, "value": value_c},
        "price_type_columns": {}, "summary": {},
    }
    if not all([item_c, elem_c, year_c, value_c]):
        result["quality_issues"].append(
            {"severity": "error", "category": "parse",
             "detail": "FAOSTAT CSV missing Item/Element/Year/Value columns."})
        return result

    alias_map = build_alias_map(repo)
    canonical = [r["canonical_name"] for r in
                 repo.query("SELECT canonical_name FROM crops")]

    rows: list[dict] = []
    unmatched: dict[str, str | None] = {}
    ignored_elements: set[str] = set()
    for _, r in df.iterrows():
        element = _norm(r[elem_c])
        target = FAOSTAT_ELEMENT_TO_KIND.get(element)
        if target is None:
            ignored_elements.add(str(r[elem_c]))
            continue
        target_kind, value_col = target
        item = r[item_c]
        cid, suggestion = _resolve_crop(str(item), alias_map, canonical)
        if cid is None:
            unmatched[str(item)] = suggestion
            continue
        try:
            yr = int(float(r[year_c]))
            val = float(r[value_c])
        except (TypeError, ValueError):
            continue
        rows.append({
            "crop_id": cid, "crop_raw": str(item), "original_crop_name": str(item),
            "year": yr, "quarter": None, "parish": None,
            "target_kind": target_kind, value_col: val, "confidence": 1.0,
        })

    normalized = pd.DataFrame(rows)
    # Validate each element subset on its own value column.
    if not normalized.empty:
        clean_parts = []
        for tk, vcol in [("production", "tonnes"), ("area_reaped", "hectares")]:
            sub = normalized[normalized["target_kind"] == tk]
            if not sub.empty:
                sub, vflags = validation.validate_quantity_rows(sub, vcol)
                result["quality_issues"].extend(vflags)
                clean_parts.append(sub)
        normalized = (pd.concat(clean_parts, ignore_index=True)
                      if clean_parts else pd.DataFrame())

    result["normalized"] = normalized
    result["crop_review"] = [{"raw": k, "suggestion": v} for k, v in sorted(unmatched.items())]
    for item in result["crop_review"]:
        result["quality_issues"].append(
            {"severity": "info", "category": "crop_name",
             "detail": f"FAOSTAT item not in taxonomy: '{item['raw']}'"
                       + (f" (suggest: {item['suggestion']})" if item["suggestion"] else ""),
             "raw_value": item["raw"]})
    prod_n = int((normalized["target_kind"] == "production").sum()) if not normalized.empty else 0
    area_n = int((normalized["target_kind"] == "area_reaped").sum()) if not normalized.empty else 0
    result["summary"] = {
        "rows_in_file": int(len(df)),
        "rows_normalized": int(len(normalized)),
        "production_rows": prod_n, "area_rows": area_n,
        "unmatched_crops": len(result["crop_review"]),
        "unknown_units": 0,
        "quality_issues": len(result["quality_issues"]),
    }
    return result


def fetch_and_preview(import_service, element_key: str,
                      years: list[int] | None = None, *, timeout: int = 60) -> dict:
    """Fetch a FAOSTAT element for Jamaica and build an approve-ready preview."""
    from . import official_fetch  # local import to avoid a cycle at module load
    url = build_url(element_key, years)
    res = official_fetch.fetch_url(url, timeout=timeout)
    if not res.ok:
        return {"ok": False, "fetch": res, "preview": None,
                "source_file_id": None, "is_new": False, "kind": None}
    try:
        records = parse_json(res.content)
    except Exception as exc:  # noqa: BLE001
        res.ok = False
        res.error = f"FAOSTAT JSON parse error: {exc}"
        res.error_class = "parse"
        return {"ok": False, "fetch": res, "preview": None,
                "source_file_id": None, "is_new": False, "kind": None}

    preview = build_preview(records, element_key, import_service.repo)
    kind = preview["kind"]
    sfid, is_new = import_service.register_source_file(
        f"faostat_{element_key}.json", res.content, kind or "production",
        origin_url=url, provenance="official_observed",
        source_name="FAOSTAT (Jamaica, QCL)", retrieval="fetch")
    return {"ok": True, "fetch": res, "preview": preview,
            "source_file_id": sfid, "is_new": is_new, "kind": kind}
