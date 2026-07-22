"""Ingestion normalization/commit and procurement-service tests."""

import pandas as pd

from jamaica_crop_intelligence.ingestion import ministry_ingestion as ing
from jamaica_crop_intelligence.services.import_service import ImportService
from jamaica_crop_intelligence.services.procurement_service import ProcurementService


PRICE_CSV = (
    "Commodity,Year,Quarter,Farmgate Price,Unit\n"
    "Tomato,2025,Q1,300,kg\n"
    "tomatoes,2025,Q2,280,lb\n"       # alias + convertible unit
    "Scotch Bonnet,2025,1,420,kg\n"   # alias
    "Total,2025,1,999,kg\n"           # should be skipped
    "Mystery Crop,2025,1,100,kg\n"    # unmatched -> review
)


def test_column_detection_and_normalization(repo):
    df = pd.read_csv(pd.io.common.StringIO(PRICE_CSV))
    cols = ing.detect_columns(df)
    assert cols["crop"] == "Commodity"
    assert cols["year"] == "Year"
    assert cols["price"] is not None

    amap = ing.build_alias_map(repo)
    canon = [r["canonical_name"] for r in repo.query("SELECT canonical_name FROM crops")]
    cid, sug = ing.normalize_crop("tomatoes", amap, canon)
    assert cid == repo.crop_id_by_name("Tomato")
    cid2, sug2 = ing.normalize_crop("Mystery Crop", amap, canon)
    assert cid2 is None


def test_preview_flags_unmatched_and_totals(repo):
    imp = ImportService(repo)
    preview = imp.preview(PRICE_CSV.encode(), "prices.csv", "prices")
    # Tomato x2 + Scotch Bonnet = 3 normalized rows; Total & Mystery excluded.
    assert preview["summary"]["rows_normalized"] == 3
    raws = {c["raw"] for c in preview["crop_review"]}
    assert "Mystery Crop" in raws
    # lb row got normalized to a different /kg price than its raw price
    norm = preview["normalized"]
    lb_row = norm[norm["crop_raw"] == "tomatoes"].iloc[0]
    assert lb_row["price_per_kg_jmd"] > lb_row["price_jmd"]


def test_commit_is_idempotent(repo):
    imp = ImportService(repo)
    before = repo.table_count("price_records")
    preview = imp.preview(PRICE_CSV.encode(), "prices.csv", "prices")
    sfid, is_new = imp.register_source_file("prices.csv", PRICE_CSV.encode(), "prices")
    assert is_new is True
    res1 = imp.commit_preview(sfid, "prices", preview["normalized"],
                              preview["quality_issues"])
    assert res1["rows_committed"] == 3
    after = repo.table_count("price_records")
    assert after == before + 3
    # committing again writes nothing new
    res2 = imp.commit_preview(sfid, "prices", preview["normalized"])
    assert res2["rows_committed"] == 0
    assert repo.table_count("price_records") == after
    # committed rows are tagged official_observed
    n_official = repo.scalar(
        "SELECT COUNT(*) FROM price_records WHERE provenance='official_observed'")
    assert n_official == 3


def test_duplicate_file_hash_detected(repo):
    imp = ImportService(repo)
    imp.register_source_file("a.csv", PRICE_CSV.encode(), "prices")
    _id, is_new = imp.register_source_file("renamed.csv", PRICE_CSV.encode(), "prices")
    assert is_new is False  # same bytes -> same hash


def test_production_commit(repo):
    imp = ImportService(repo)
    csv = ("Crop,Year,Quarter,Production (tonnes)\n"
           "Yellow Yam,2025,1,5000\nCabbage,2025,1,3000\n")
    preview = imp.preview(csv.encode(), "prod.csv", "production")
    assert preview["summary"]["rows_normalized"] == 2
    sfid, _ = imp.register_source_file("prod.csv", csv.encode(), "production")
    res = imp.commit_preview(sfid, "production", preview["normalized"])
    assert res["rows_committed"] == 2


def test_procurement_coverage(repo):
    proc = ProcurementService(repo)
    cid = repo.crop_id_by_name("Tomato")
    plan = proc.plan_requirement(1000, 0.1, 0.15)
    assert plan["required_kg"] > 1000
    cov = proc.coverage_summary(cid, plan["required_kg"])
    assert cov["required_kg"] == plan["required_kg"]
    assert cov["local_capacity_kg"] >= 0
