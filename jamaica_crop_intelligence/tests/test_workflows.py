"""End-to-end workflow tests across services (RFQ -> delivery, scenarios)."""

from jamaica_crop_intelligence.services.procurement_integration_service import (
    ProcurementIntegrationService,
)
from jamaica_crop_intelligence.services.procurement_service import ProcurementService
from jamaica_crop_intelligence.services.production_service import ProductionService
from jamaica_crop_intelligence.services.scenario_service import ScenarioService
from jamaica_crop_intelligence.services.threat_service import ThreatService


def test_full_procurement_lifecycle(repo):
    integ = ProcurementIntegrationService(repo)
    proc = ProcurementService(repo)
    cid = repo.crop_id_by_name("Tomato")

    rfq_id = integ.create_rfq("Hotel Group", cid, 500, "2026-08-01", "Grade A")
    assert integ.rfq(rfq_id)["status"] == "open"

    sups = proc.suppliers_for_crop(cid)
    assert not sups.empty
    for _, s in sups.iterrows():
        integ.add_quotation(rfq_id, int(s["supplier_id"]), 250.0,
                            float(s["typical_kg_per_week"]),
                            int(s["lead_time_days"]), 4.0)

    scored = integ.score_quotations(rfq_id)
    assert scored.iloc[0]["rank"] == 1
    best_qid = int(scored.iloc[0]["id"])

    con = integ.award_contract(rfq_id, best_qid, 500)
    con_ref = integ.contract(con)["reference"]
    assert integ.rfq(rfq_id)["status"] == "awarded"
    assert integ.contract(con)["status"] == "active"

    integ.record_delivery(con, "2026-07-20", 250, 250, 0, True, 5.0)
    integ.record_delivery(con, "2026-07-27", 250, 250, 0, True, 4.0)
    kpi = integ.contract_kpis(con)
    assert kpi["fill_rate"] >= 0.99
    # contract auto-marked fulfilled once fill rate ~ 100%
    assert integ.contract(con)["status"] == "fulfilled"

    perf = integ.supplier_performance()
    assert not perf.empty
    pva = integ.planned_vs_actual()
    assert (pva["contract"] == con_ref).any()


def test_partial_delivery_shows_shortage(repo):
    integ = ProcurementIntegrationService(repo)
    proc = ProcurementService(repo)
    cid = repo.crop_id_by_name("Cabbage")
    rfq_id = integ.create_rfq("Buyer", cid, 400)
    sup = proc.suppliers_for_crop(cid).iloc[0]
    integ.add_quotation(rfq_id, int(sup["supplier_id"]), 150.0, 400, 5, 4.0)
    scored = integ.score_quotations(rfq_id)
    con = integ.award_contract(rfq_id, int(scored.iloc[0]["id"]), 400)
    con_ref = integ.contract(con)["reference"]
    integ.record_delivery(con, "2026-07-20", 400, 300, 20, False, 3.0)
    sr = integ.shortages_and_rejections()
    assert (sr["contract"] == con_ref).any()
    row = sr[sr["contract"] == con_ref].iloc[0]
    assert row["outstanding_kg"] > 0
    assert row["rejected_kg"] == 20


def test_scenario_supply_and_price(repo):
    scen = ScenarioService(repo)
    cid = repo.crop_id_by_name("Tomato")
    shock = scen.supply_shock(cid, -0.3)
    assert (shock["scenario_supply_index"] <= shock["baseline_supply_index"]).all()
    pr = scen.price_response(cid, -0.2)
    # supply down -> modelled price up
    assert pr["scenario_price_jmd_per_kg"] >= pr["baseline_price_jmd_per_kg"]
    assert pr["provenance"] == "modelled"


def test_seasonality_and_threats_wire_up(repo):
    prod = ProductionService(repo)
    threat = ThreatService(repo)
    cid = repo.crop_id_by_name("Tomato")
    curve = prod.supply_index(cid)
    assert len(curve) == 12 and max(curve) == 1.0
    exp = threat.exposure_calendar(cid)
    assert len(exp) == 12
    assert exp["exposure"].sum() > 0


def test_in_season_matches_supply_index(repo):
    prod = ProductionService(repo)
    # A month where Tomato peaks (Jan) should list Tomato as in season.
    df = prod.crops_in_season(1, threshold=0.9)
    assert "Tomato" in df["crop"].tolist()
