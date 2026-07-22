"""Tests for dynamic-data ingestion: weekly parsing, validation, lineage,
fetch adapter, and the enhanced price service."""

import pandas as pd

from jamaica_crop_intelligence.ingestion import official_fetch
from jamaica_crop_intelligence.ingestion import validation
from jamaica_crop_intelligence.services.import_service import ImportService
from jamaica_crop_intelligence.services.price_service import PriceService


WEEKLY_CSV = (
    "Commodity,Week Ending,Market,Farmgate Price,Wholesale Price,Retail Price,Unit\n"
    "Tomato,2025-03-07,Coronation Market,255,300,360,kg\n"
    "Tomato,2025-03-14,Coronation Market,262,310,370,kg\n"
    "Escallion,2025-03-07,Santa Cruz Market,340,390,450,kg\n"
    "Tomato,2025-03-07,Coronation Market,-5,300,360,kg\n"   # negative farmgate
)


def test_weekly_wide_format_melts_to_price_types(repo):
    imp = ImportService(repo)
    prev = imp.preview(WEEKLY_CSV.encode(), "weekly.csv", "prices")
    assert set(prev["price_type_columns"]) == {"farmgate", "wholesale", "retail"}
    types = set(prev["normalized"]["price_type"].unique())
    assert types == {"farmgate", "wholesale", "retail"}
    # dates and markets captured as lineage
    row = prev["normalized"].iloc[0]
    assert row["price_date"] == "2025-03-07"
    assert row["market_location"] == "Coronation Market"
    assert row["original_crop_name"] == "Tomato"


def test_negative_price_dropped_and_flagged(repo):
    imp = ImportService(repo)
    prev = imp.preview(WEEKLY_CSV.encode(), "weekly.csv", "prices")
    # the -5 farmgate row must not survive
    assert not ((prev["normalized"]["price_type"] == "farmgate")
                & (prev["normalized"]["price_jmd"] < 0)).any()
    cats = {f["category"] for f in prev["quality_issues"]}
    assert "range" in cats  # negative dropped -> range flag


def test_commit_writes_lineage_and_no_negatives(repo):
    imp = ImportService(repo)
    prev = imp.preview(WEEKLY_CSV.encode(), "weekly.csv", "prices")
    sfid, _ = imp.register_source_file("weekly.csv", WEEKLY_CSV.encode(), "prices",
                                       source_name="Ministry Weekly Prices",
                                       retrieval="upload")
    imp.commit_preview(sfid, "prices", prev["normalized"], prev["quality_issues"])
    # lineage persisted
    row = repo.query_one(
        "SELECT price_date, market_location, original_crop_name, provenance, confidence "
        "FROM price_records WHERE source_file_id=? LIMIT 1", (sfid,))
    assert row["price_date"] == "2025-03-07"
    assert row["market_location"] == "Coronation Market"
    assert row["provenance"] == "official_observed"
    # NO negative prices reached the fact table
    neg = repo.scalar("SELECT COUNT(*) FROM price_records WHERE price_jmd < 0")
    assert neg == 0
    # source_name recorded
    sf = repo.query_one("SELECT source_name, retrieval FROM source_files WHERE id=?", (sfid,))
    assert sf["source_name"] == "Ministry Weekly Prices"


def test_every_price_record_has_provenance(repo):
    # includes seeds + any commits: evidence classification is never null
    missing = repo.scalar(
        "SELECT COUNT(*) FROM price_records WHERE provenance IS NULL OR provenance=''")
    assert missing == 0


def test_validation_helpers_direct():
    df = pd.DataFrame({
        "crop_id": [1, 1, 1, 1],
        "crop_raw": ["Tomato"] * 4,
        "price_jmd": [100, 102, 98, 100],
        "price_per_kg_jmd": [100, 102, 98, 5000],  # last is an outlier + unit mismatch
        "year": [2025] * 4,
    })
    clean, flags = validation.validate_price_rows(df)
    assert len(clean) == 4  # none dropped (all positive)
    cats = {f["category"] for f in flags}
    assert "outlier" in cats or "unit" in cats


def test_fetch_adapter_graceful_failure():
    # A blocked/unreachable host must return ok=False, never raise.
    res = official_fetch.fetch_url("https://data.gov.jm/nonexistent-file.csv", timeout=20)
    assert res.ok is False
    assert res.error  # a human-readable reason is present
    # fetchable sources are file URLs only (no portal landing pages)
    for s in official_fetch.fetchable_sources():
        assert s["url"].lower().endswith((".csv", ".pdf", ".xlsx", ".xls", ".txt"))


def test_migration_adds_missing_columns():
    import sqlite3
    from jamaica_crop_intelligence.database import schema
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # An old-style DB missing the new lineage columns.
    conn.execute("CREATE TABLE price_records(id INTEGER PRIMARY KEY, crop_id INT, "
                 "price_jmd REAL, provenance TEXT)")
    conn.execute("CREATE TABLE production_records(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE area_reaped_records(id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE source_files(id INTEGER PRIMARY KEY)")
    schema.apply_migrations(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(price_records)")}
    assert {"price_date", "week_ending", "market_location",
            "original_crop_name", "confidence"}.issubset(cols)
    # idempotent: running again does not error
    schema.apply_migrations(conn)


def test_price_service_dynamic_dimensions(repo):
    imp = ImportService(repo)
    prev = imp.preview(WEEKLY_CSV.encode(), "weekly.csv", "prices")
    sfid, _ = imp.register_source_file("weekly.csv", WEEKLY_CSV.encode(), "prices")
    imp.commit_preview(sfid, "prices", prev["normalized"], prev["quality_issues"])

    ps = PriceService(repo)
    cid = repo.crop_id_by_name("Tomato")
    types = ps.distinct_price_types(cid)
    assert {"farmgate", "wholesale", "retail"}.issubset(set(types))
    assert "Coronation Market" in ps.distinct_markets(cid)

    spread = ps.spread_frame(cid)
    assert "spread_jmd_per_kg" in spread.columns
    assert (spread["spread_jmd_per_kg"].dropna() >= 0).all()

    metrics = ps.change_metrics(cid, price_type="farmgate")
    assert "latest_jmd_per_kg" in metrics

    refresh = ps.last_refresh()
    assert refresh  # non-empty
    assert refresh.get("provenance") in ("official_observed", "illustrative")
