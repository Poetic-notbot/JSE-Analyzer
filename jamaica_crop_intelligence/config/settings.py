"""Central configuration for the Jamaica Crop Intelligence module."""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PACKAGE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = PACKAGE_ROOT.parent
DATA_DIR = PACKAGE_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# On Streamlit Community Cloud the filesystem is ephemeral; the DB is rebuilt
# and re-seeded on each cold start. An env override lets an operator point at a
# persistent volume when one is available.
DEFAULT_DB_PATH = DATA_DIR / "crop_intelligence.db"
DB_PATH = Path(os.environ.get("JCI_DB_PATH", str(DEFAULT_DB_PATH)))

# --------------------------------------------------------------------------- #
# App metadata
# --------------------------------------------------------------------------- #
APP_TITLE = "Jamaica Crop Intelligence"
APP_ICON = "🌱"
APP_TAGLINE = (
    "Production - seasonality - parish sourcing - prices - threats - "
    "procurement - RFQ to delivery"
)

# The 14 parishes of Jamaica, grouped by county (used for grouping/labels).
PARISHES = [
    # Surrey
    "Kingston", "St. Andrew", "St. Thomas", "Portland",
    # Middlesex
    "St. Mary", "St. Ann", "St. Catherine", "Clarendon", "Manchester",
    # Cornwall
    "St. Elizabeth", "Westmoreland", "Hanover", "St. James", "Trelawny",
]

PARISH_COUNTY = {
    "Kingston": "Surrey", "St. Andrew": "Surrey", "St. Thomas": "Surrey",
    "Portland": "Surrey", "St. Mary": "Middlesex", "St. Ann": "Middlesex",
    "St. Catherine": "Middlesex", "Clarendon": "Middlesex",
    "Manchester": "Middlesex", "St. Elizabeth": "Cornwall",
    "Westmoreland": "Cornwall", "Hanover": "Cornwall", "St. James": "Cornwall",
    "Trelawny": "Cornwall",
}

MONTHS = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]

QUARTER_MONTHS = {1: [1, 2, 3], 2: [4, 5, 6], 3: [7, 8, 9], 4: [10, 11, 12]}

# --------------------------------------------------------------------------- #
# Data-provenance vocabulary. Every numeric fact carries one of these tags so
# the UI can be honest about what is observed vs. modelled vs. illustrative.
# --------------------------------------------------------------------------- #
PROVENANCE = {
    "official_observed": "Official observed (Ministry / RADA / STATIN)",
    "official_registry": "Official registry footprint (not live harvest)",
    "modelled": "Modelled relationship (research-supported)",
    "illustrative": "Illustrative seed value (not official)",
    "user_entered": "User entered",
}

# Provenance colours for badges (hex).
PROVENANCE_COLOR = {
    "official_observed": "#1b7837",
    "official_registry": "#5aae61",
    "modelled": "#2166ac",
    "illustrative": "#b8860b",
    "user_entered": "#762a83",
}

# --------------------------------------------------------------------------- #
# Verified official source registry (surfaced on the Data & Methodology page).
# Hosts may be blocked by a given network policy; ingestion also accepts manual
# uploads of the same files.
# --------------------------------------------------------------------------- #
OFFICIAL_SOURCES = [
    {
        "name": "MOA Agricultural Data portal",
        "url": "https://www.moa.gov.jm/content/agricultural-data",
        "kind": "portal",
        "provenance": "official_observed",
    },
    {
        "name": "MOA Commodity Prices",
        "url": "https://www.moa.gov.jm/document-categories/commodity-prices",
        "kind": "prices",
        "provenance": "official_observed",
    },
    {
        "name": "All-Island Farmgate Prices by Quarter",
        "url": "https://www.moa.gov.jm/agridata/all-island-estimates-farmgate-prices-quarter",
        "kind": "prices",
        "provenance": "official_observed",
    },
    {
        "name": "All-Island Crop Area Reaped by Quarter",
        "url": "https://www.moa.gov.jm/agridata/all-island-estimates-crop-area-reaped-quarter",
        "kind": "area_reaped",
        "provenance": "official_observed",
    },
    {
        "name": "Crop Production by Quarter 2024 (PDF)",
        "url": "https://www.moa.gov.jm/sites/default/files/crop_production_by_quarter_2024.pdf",
        "kind": "production",
        "provenance": "official_observed",
    },
    {
        "name": "Crop Production by Quarter 2022 (PDF)",
        "url": "https://www.moa.gov.jm/sites/default/files/crop_production_by_quarter_2022.pdf",
        "kind": "production",
        "provenance": "official_observed",
    },
    {
        "name": "Crop Production 10yrs 2015-2024 (PDF)",
        "url": "https://www.moa.gov.jm/sites/default/files/crop_production_10yrs_2015-2024.pdf",
        "kind": "production",
        "provenance": "official_observed",
    },
    {
        "name": "RADA/ABIS public crop registry (CSV)",
        "url": "https://data.gov.jm/sites/default/files/reportfarmers_parameterized_crop_summary-xlsx.csv",
        "kind": "registry",
        "provenance": "official_registry",
    },
    {
        "name": "FAO-RADA crop-calendar initiative (context)",
        "url": "https://www.fao.org/jamaica-bahamas-and-belize/news/detail-events/zh/c/1709422/",
        "kind": "guidance",
        "provenance": "modelled",
    },
]

# Default procurement planning assumptions.
DEFAULT_BUFFER_PCT = 0.10          # safety buffer on demand
DEFAULT_POST_HARVEST_LOSS = 0.15   # typical local post-harvest loss share

# Weights for quotation comparison scoring (must sum to 1.0).
DEFAULT_QUOTE_WEIGHTS = {
    "price": 0.45,
    "lead_time": 0.20,
    "quality": 0.20,
    "reliability": 0.15,
}
