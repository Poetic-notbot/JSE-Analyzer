"""SQLite schema for the Jamaica Crop Intelligence data model.

The schema is intentionally normalized around a small set of reference tables
(crops, parishes-as-text, markets, units, threats, suppliers) and fact tables
(production_records, area_reaped_records, price_records, deliveries ...). Every
fact table carries a ``provenance`` column so the UI can distinguish observed,
registry, modelled and illustrative values.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

# --------------------------------------------------------------------------- #
# DDL. Ordered so that foreign-key parents are created before children.
# --------------------------------------------------------------------------- #
DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ---- Reference: crops -------------------------------------------------------
CREATE TABLE IF NOT EXISTS crops (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL UNIQUE,
    category      TEXT NOT NULL,           -- root/tuber, vegetable, fruit, legume, herb, cash
    scientific_name TEXT,
    is_seasonal   INTEGER NOT NULL DEFAULT 0,
    notes         TEXT
);

CREATE TABLE IF NOT EXISTS crop_aliases (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_id   INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,
    source    TEXT,
    UNIQUE (crop_id, alias)
);
CREATE INDEX IF NOT EXISTS idx_alias_lower ON crop_aliases(alias);

CREATE TABLE IF NOT EXISTS crop_varieties (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_id   INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    variety   TEXT NOT NULL,
    notes     TEXT,
    UNIQUE (crop_id, variety)
);

CREATE TABLE IF NOT EXISTS crop_cycles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_id        INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    days_to_maturity_min INTEGER,
    days_to_maturity_max INTEGER,
    cycle_notes    TEXT
);

-- Modelled monthly availability profile (12 comma-separated 0..1 weights) plus
-- explicit planting/harvest windows. Always provenance='modelled'.
CREATE TABLE IF NOT EXISTS crop_calendar_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_id       INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    event_type    TEXT NOT NULL,           -- planting, harvest, peak
    month         INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    weight        REAL NOT NULL DEFAULT 1.0,
    provenance    TEXT NOT NULL DEFAULT 'modelled',
    notes         TEXT
);

-- ---- Fact: observed production (quarterly) ---------------------------------
CREATE TABLE IF NOT EXISTS production_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_id       INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    parish        TEXT,                     -- NULL = all-island
    year          INTEGER NOT NULL,
    quarter       INTEGER CHECK (quarter BETWEEN 1 AND 4),
    tonnes        REAL,
    original_crop_name TEXT,
    confidence    REAL DEFAULT 1.0,
    provenance    TEXT NOT NULL DEFAULT 'illustrative',
    source_file_id INTEGER REFERENCES source_files(id),
    record_key    TEXT UNIQUE,             -- dedupe key
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS area_reaped_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_id       INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    parish        TEXT,
    year          INTEGER NOT NULL,
    quarter       INTEGER CHECK (quarter BETWEEN 1 AND 4),
    hectares      REAL,
    provenance    TEXT NOT NULL DEFAULT 'illustrative',
    source_file_id INTEGER REFERENCES source_files(id),
    record_key    TEXT UNIQUE,
    created_at    TEXT DEFAULT (datetime('now'))
);

-- ---- Reference/fact: parish registry footprint (NOT live harvest) ----------
CREATE TABLE IF NOT EXISTS parish_crop_registry (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_id       INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    parish        TEXT NOT NULL,
    farmer_count  INTEGER,
    registered_hectares REAL,
    provenance    TEXT NOT NULL DEFAULT 'official_registry',
    source_file_id INTEGER REFERENCES source_files(id),
    record_key    TEXT UNIQUE
);

-- ---- Reference: markets & units --------------------------------------------
CREATE TABLE IF NOT EXISTS markets (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL UNIQUE,
    parish    TEXT,
    market_type TEXT                        -- farmgate, wholesale, retail
);

CREATE TABLE IF NOT EXISTS units (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    kg_equivalent REAL,                      -- NULL if not mass-convertible
    notes         TEXT
);

-- ---- Fact: prices ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS price_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_id       INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    market_id     INTEGER REFERENCES markets(id),
    unit_id       INTEGER REFERENCES units(id),
    parish        TEXT,
    year          INTEGER NOT NULL,
    quarter       INTEGER CHECK (quarter BETWEEN 1 AND 4),
    month         INTEGER CHECK (month BETWEEN 1 AND 12),
    price_jmd     REAL NOT NULL,
    price_per_kg_jmd REAL,                   -- normalized where convertible
    price_type    TEXT DEFAULT 'farmgate',   -- farmgate, wholesale, urban_retail, rural_retail
    price_date    TEXT,                      -- ISO date or week-ending, when known
    week_ending   TEXT,                      -- week-ending label from weekly workbooks
    market_location TEXT,                    -- free-text market/location as published
    original_crop_name TEXT,                 -- raw crop name before normalization (lineage)
    confidence    REAL DEFAULT 1.0,          -- 0..1 ingestion confidence
    provenance    TEXT NOT NULL DEFAULT 'illustrative',
    source_file_id INTEGER REFERENCES source_files(id),
    record_key    TEXT UNIQUE,
    created_at    TEXT DEFAULT (datetime('now'))
);

-- ---- Threats ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS threats (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    threat_type   TEXT NOT NULL,            -- climate, pest, disease, security
    description   TEXT,
    severity      INTEGER DEFAULT 3         -- 1..5
);

CREATE TABLE IF NOT EXISTS crop_threat_links (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    crop_id   INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    threat_id INTEGER NOT NULL REFERENCES threats(id) ON DELETE CASCADE,
    impact    INTEGER DEFAULT 3,           -- 1..5
    UNIQUE (crop_id, threat_id)
);

CREATE TABLE IF NOT EXISTS threat_seasonality (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    threat_id INTEGER NOT NULL REFERENCES threats(id) ON DELETE CASCADE,
    month     INTEGER NOT NULL CHECK (month BETWEEN 1 AND 12),
    intensity REAL NOT NULL DEFAULT 0.0,    -- 0..1
    UNIQUE (threat_id, month)
);

-- ---- Suppliers & procurement reference -------------------------------------
CREATE TABLE IF NOT EXISTS suppliers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    parish        TEXT,
    contact       TEXT,
    supplier_type TEXT DEFAULT 'farmer',    -- farmer, cooperative, aggregator
    reliability   REAL DEFAULT 0.8          -- 0..1 baseline reliability
);

CREATE TABLE IF NOT EXISTS supplier_crop_capabilities (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id   INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    crop_id       INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    typical_kg_per_week REAL,
    lead_time_days INTEGER DEFAULT 7,
    UNIQUE (supplier_id, crop_id)
);

CREATE TABLE IF NOT EXISTS buyer_requirements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    buyer_name    TEXT NOT NULL,
    crop_id       INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    weekly_kg     REAL,
    quality_spec  TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS procurement_plans (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    crop_id       INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    target_month  INTEGER CHECK (target_month BETWEEN 1 AND 12),
    demand_kg     REAL NOT NULL,
    buffer_pct    REAL DEFAULT 0.10,
    loss_pct      REAL DEFAULT 0.15,
    required_kg   REAL,
    status        TEXT DEFAULT 'draft',
    notes         TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scenarios (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    crop_id       INTEGER REFERENCES crops(id) ON DELETE CASCADE,
    params_json   TEXT,
    result_json   TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alerts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    level         TEXT NOT NULL,            -- info, watch, warning
    title         TEXT NOT NULL,
    body          TEXT,
    crop_id       INTEGER REFERENCES crops(id),
    threat_id     INTEGER REFERENCES threats(id),
    created_at    TEXT DEFAULT (datetime('now'))
);

-- ---- Procurement transaction lifecycle -------------------------------------
CREATE TABLE IF NOT EXISTS rfqs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reference     TEXT NOT NULL UNIQUE,
    buyer_name    TEXT NOT NULL,
    crop_id       INTEGER NOT NULL REFERENCES crops(id) ON DELETE CASCADE,
    quantity_kg   REAL NOT NULL,
    needed_by     TEXT,
    quality_spec  TEXT,
    status        TEXT DEFAULT 'open',      -- open, awarded, cancelled, closed
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS quotations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id        INTEGER NOT NULL REFERENCES rfqs(id) ON DELETE CASCADE,
    supplier_id   INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    unit_price_jmd_per_kg REAL NOT NULL,
    available_kg  REAL NOT NULL,
    lead_time_days INTEGER DEFAULT 7,
    quality_rating REAL DEFAULT 3.0,        -- 1..5 self/assessed
    score         REAL,                     -- computed comparison score
    status        TEXT DEFAULT 'received',  -- received, shortlisted, rejected, accepted
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contracts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    reference     TEXT NOT NULL UNIQUE,
    rfq_id        INTEGER REFERENCES rfqs(id),
    quotation_id  INTEGER REFERENCES quotations(id),
    supplier_id   INTEGER NOT NULL REFERENCES suppliers(id),
    crop_id       INTEGER NOT NULL REFERENCES crops(id),
    contracted_kg REAL NOT NULL,
    unit_price_jmd_per_kg REAL NOT NULL,
    start_date    TEXT,
    end_date      TEXT,
    status        TEXT DEFAULT 'active',    -- active, fulfilled, breached, closed
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS deliveries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    contract_id   INTEGER NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    delivery_date TEXT,
    ordered_kg    REAL,
    delivered_kg  REAL NOT NULL DEFAULT 0,
    rejected_kg   REAL NOT NULL DEFAULT 0,
    on_time       INTEGER NOT NULL DEFAULT 1,
    quality_rating REAL DEFAULT 3.0,
    notes         TEXT,
    created_at    TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS supplier_performance (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id   INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    period        TEXT,                     -- e.g. 2024-Q1 or rolling
    fill_rate     REAL,
    on_time_rate  REAL,
    reject_rate   REAL,
    avg_quality   REAL,
    deliveries_count INTEGER,
    updated_at    TEXT DEFAULT (datetime('now')),
    UNIQUE (supplier_id, period)
);

-- ---- Data stewardship / ingestion ------------------------------------------
CREATE TABLE IF NOT EXISTS source_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    filename      TEXT NOT NULL,
    file_hash     TEXT NOT NULL,
    kind          TEXT,                     -- prices, production, area_reaped, registry
    origin_url    TEXT,
    source_name   TEXT,                     -- human label e.g. 'Ministry Weekly Prices', 'JAMIS'
    retrieval     TEXT DEFAULT 'upload',    -- upload | fetch
    size_bytes    INTEGER,
    provenance    TEXT DEFAULT 'official_observed',
    uploaded_at   TEXT DEFAULT (datetime('now')),
    UNIQUE (file_hash)
);

CREATE TABLE IF NOT EXISTS import_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_file_id INTEGER REFERENCES source_files(id),
    kind          TEXT,
    status        TEXT DEFAULT 'previewed',  -- previewed, approved, rejected, committed
    rows_parsed   INTEGER DEFAULT 0,
    rows_committed INTEGER DEFAULT 0,
    rows_skipped  INTEGER DEFAULT 0,
    message       TEXT,
    started_at    TEXT DEFAULT (datetime('now')),
    finished_at   TEXT
);

CREATE TABLE IF NOT EXISTS data_quality_flags (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    import_run_id INTEGER REFERENCES import_runs(id) ON DELETE CASCADE,
    severity      TEXT DEFAULT 'warning',    -- info, warning, error
    category      TEXT,                      -- crop_name, unit, duplicate, range, parse
    detail        TEXT,
    raw_value     TEXT,
    resolved      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    """Open a connection with sensible pragmas and row access by name."""
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the initial DDL. Applied idempotently so an existing DB
# (e.g. a warm-restarted Streamlit Cloud instance) gains new columns without a
# destructive rebuild. Each entry: (table, column, column_def).
MIGRATIONS: list[tuple[str, str, str]] = [
    ("price_records", "price_date", "TEXT"),
    ("price_records", "week_ending", "TEXT"),
    ("price_records", "market_location", "TEXT"),
    ("price_records", "original_crop_name", "TEXT"),
    ("price_records", "confidence", "REAL DEFAULT 1.0"),
    ("production_records", "original_crop_name", "TEXT"),
    ("production_records", "confidence", "REAL DEFAULT 1.0"),
    ("area_reaped_records", "original_crop_name", "TEXT"),
    ("area_reaped_records", "confidence", "REAL DEFAULT 1.0"),
    ("source_files", "source_name", "TEXT"),
    ("source_files", "retrieval", "TEXT DEFAULT 'upload'"),
]


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}
    except sqlite3.OperationalError:
        return set()


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Add any columns missing from an older database (idempotent)."""
    for table, column, coldef in MIGRATIONS:
        cols = _existing_columns(conn, table)
        if cols and column not in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
            except sqlite3.OperationalError:
                pass  # already added by a concurrent connection
    conn.commit()


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all tables (idempotent), apply column migrations, stamp version."""
    conn.executescript(DDL)
    apply_migrations(conn)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def is_seeded(conn: sqlite3.Connection) -> bool:
    """A DB is 'seeded' once the crops reference table has rows."""
    try:
        row = conn.execute("SELECT COUNT(*) AS n FROM crops").fetchone()
        return bool(row and row["n"] > 0)
    except sqlite3.OperationalError:
        return False
