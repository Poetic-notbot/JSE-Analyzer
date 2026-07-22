"""Seed the database with curated Jamaican reference data.

Provenance honesty
------------------
* Crop taxonomy, aliases, parish sourcing associations, seasonal windows,
  threats and their seasonality are **modelled / research-supported** domain
  knowledge about Jamaican agriculture.
* Production tonnages and prices seeded here are **illustrative** placeholders
  (``provenance='illustrative'``) so the app is usable before official files
  are ingested. They are labelled as such everywhere in the UI and are meant to
  be replaced by official Ministry/RADA data via the Administration page.
* Registry footprint rows are tagged ``official_registry`` in shape but seeded
  with illustrative magnitudes until the real RADA CSV is ingested.

Nothing seeded here should be read as observed monthly output.
"""

from __future__ import annotations

from ..calculations import core
from ..services.repository import Repository

# --------------------------------------------------------------------------- #
# Crops: (canonical, category, scientific, seasonal?, harvest_months,
#         [aliases], [sourcing parishes], illustrative farmgate JMD/kg)
# harvest_months drive the MODELLED monthly supply index.
# --------------------------------------------------------------------------- #
CROPS = [
    ("Yellow Yam", "root/tuber", "Dioscorea cayenensis", 0, [10, 11, 12, 1],
     ["yam", "yellow yam", "yampi"], ["Trelawny", "St. Elizabeth", "Manchester", "Clarendon"], 190),
    ("Sweet Potato", "root/tuber", "Ipomoea batatas", 0, [3, 4, 9, 10],
     ["sweet potato", "sweetpotato"], ["St. Elizabeth", "St. Catherine", "Clarendon"], 130),
    ("Irish Potato", "root/tuber", "Solanum tuberosum", 0, [1, 2, 6, 7],
     ["irish potato", "potato"], ["Manchester", "St. Elizabeth", "St. Ann"], 210),
    ("Dasheen", "root/tuber", "Colocasia esculenta", 0, [5, 6, 11, 12],
     ["dasheen", "taro"], ["Portland", "St. Mary", "St. Thomas"], 170),
    ("Cassava", "root/tuber", "Manihot esculenta", 0, [7, 8, 9, 12, 1],
     ["cassava", "yuca"], ["St. Elizabeth", "Clarendon", "St. Catherine"], 120),
    ("Coco", "root/tuber", "Xanthosoma sagittifolium", 0, [6, 7, 11, 12],
     ["coco", "cocoyam"], ["Portland", "St. Mary"], 160),
    ("Tomato", "vegetable", "Solanum lycopersicum", 0, [1, 2, 3, 11, 12],
     ["tomato", "tomatoes"], ["St. Elizabeth", "St. Catherine", "Clarendon"], 280),
    ("Cabbage", "vegetable", "Brassica oleracea var. capitata", 0, [1, 2, 3, 12],
     ["cabbage"], ["Manchester", "St. Ann", "St. Elizabeth"], 150),
    ("Carrot", "vegetable", "Daucus carota", 0, [1, 2, 3, 12],
     ["carrot", "carrots"], ["Manchester", "St. Ann"], 170),
    ("Callaloo", "vegetable", "Amaranthus viridis", 0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
     ["callaloo", "calaloo", "amaranth"], ["St. Catherine", "Clarendon", "St. Elizabeth"], 140),
    ("Pak Choi", "vegetable", "Brassica rapa subsp. chinensis", 0, [1, 2, 3, 10, 11, 12],
     ["pak choi", "bok choy", "pakchoi"], ["St. Catherine", "Manchester"], 160),
    ("Lettuce", "vegetable", "Lactuca sativa", 0, [1, 2, 11, 12],
     ["lettuce"], ["Manchester", "St. Ann"], 220),
    ("Sweet Pepper", "vegetable", "Capsicum annuum", 0, [2, 3, 4, 11, 12],
     ["sweet pepper", "bell pepper"], ["St. Elizabeth", "St. Catherine"], 320),
    ("Scotch Bonnet Pepper", "vegetable", "Capsicum chinense", 0, [3, 4, 5, 10, 11],
     ["scotch bonnet", "hot pepper", "pepper"], ["St. Elizabeth", "Clarendon", "St. Catherine"], 420),
    ("Cucumber", "vegetable", "Cucumis sativus", 0, [1, 2, 3, 11, 12],
     ["cucumber", "cuke"], ["St. Elizabeth", "St. Catherine"], 130),
    ("Pumpkin", "vegetable", "Cucurbita moschata", 0, [1, 2, 6, 7, 12],
     ["pumpkin"], ["St. Elizabeth", "Clarendon", "Manchester"], 110),
    ("String Bean", "vegetable", "Phaseolus vulgaris", 0, [2, 3, 10, 11],
     ["string bean", "string beans", "green beans"], ["Manchester", "St. Ann"], 260),
    ("Escallion", "herb", "Allium fistulosum", 0, [1, 2, 3, 4, 11, 12],
     ["escallion", "scallion", "escalion"], ["St. Elizabeth", "Manchester"], 340),
    ("Thyme", "herb", "Thymus vulgaris", 0, [1, 2, 3, 4, 11, 12],
     ["thyme"], ["St. Elizabeth", "Manchester"], 520),
    ("Ginger", "cash", "Zingiber officinale", 0, [11, 12, 1],
     ["ginger"], ["St. Elizabeth", "Manchester", "Clarendon"], 600),
    ("Onion", "vegetable", "Allium cepa", 0, [2, 3, 4],
     ["onion", "onions"], ["St. Elizabeth"], 300),
    ("Plantain", "fruit", "Musa paradisiaca", 0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
     ["plantain", "plantains"], ["St. Mary", "Portland", "St. Thomas", "Clarendon"], 150),
    ("Banana", "fruit", "Musa acuminata", 0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
     ["banana", "bananas"], ["St. Mary", "Portland", "St. Thomas", "Clarendon"], 110),
    ("Pineapple", "fruit", "Ananas comosus", 0, [5, 6, 7, 8],
     ["pineapple", "pine"], ["St. Catherine", "Clarendon"], 180),
    ("Watermelon", "fruit", "Citrullus lanatus", 0, [2, 3, 4, 11, 12],
     ["watermelon", "melon"], ["St. Elizabeth", "St. Catherine"], 100),
    ("Ackee", "fruit", "Blighia sapida", 1, [1, 2, 3, 6, 7, 8],
     ["ackee", "akee"], ["Clarendon", "St. Andrew", "St. Catherine"], 260),
    ("Mango", "fruit", "Mangifera indica", 1, [5, 6, 7, 8],
     ["mango", "mangoes", "east indian mango"], ["Clarendon", "St. Elizabeth", "St. Catherine"], 130),
    ("Papaya", "fruit", "Carica papaya", 0, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12],
     ["papaya", "pawpaw", "paw paw"], ["St. Catherine", "St. Elizabeth"], 140),
    ("Sorrel", "cash", "Hibiscus sabdariffa", 1, [11, 12],
     ["sorrel", "roselle"], ["St. Elizabeth", "Clarendon", "St. Catherine"], 380),
    ("Gungo Peas", "legume", "Cajanus cajan", 1, [11, 12, 1],
     ["gungo peas", "pigeon peas", "gungo"], ["Clarendon", "St. Elizabeth", "Manchester"], 400),
    ("Red Peas", "legume", "Phaseolus vulgaris", 0, [1, 2, 6, 7],
     ["red peas", "kidney beans", "red kidney beans"], ["St. Elizabeth", "Clarendon"], 460),
    ("Corn", "vegetable", "Zea mays", 0, [3, 4, 5, 9, 10],
     ["corn", "maize"], ["St. Elizabeth", "Clarendon", "Manchester"], 90),
]

# --------------------------------------------------------------------------- #
# Markets
# --------------------------------------------------------------------------- #
MARKETS = [
    ("Coronation Market", "Kingston", "wholesale"),
    ("Papine Market", "St. Andrew", "retail"),
    ("Constant Spring Market", "St. Andrew", "retail"),
    ("Cross Roads Market", "St. Andrew", "retail"),
    ("Spanish Town Market", "St. Catherine", "wholesale"),
    ("Linstead Market", "St. Catherine", "wholesale"),
    ("May Pen Market", "Clarendon", "wholesale"),
    ("Santa Cruz Market", "St. Elizabeth", "wholesale"),
    ("Black River Market", "St. Elizabeth", "retail"),
    ("Charles Gordon Market (Montego Bay)", "St. James", "wholesale"),
    ("Ocho Rios Market", "St. Ann", "retail"),
    ("Savanna-la-Mar Market", "Westmoreland", "retail"),
    ("Port Antonio Market", "Portland", "retail"),
    ("Farmgate (all-island)", None, "farmgate"),
]

# --------------------------------------------------------------------------- #
# Units with kg equivalents (None = not mass-convertible without more context)
# --------------------------------------------------------------------------- #
UNITS = [
    ("kg", 1.0, "kilogram"),
    ("lb", 0.453592, "pound"),
    ("tonne", 1000.0, "metric ton"),
    ("g", 0.001, "gram"),
    ("bag (50 lb)", 22.6796, "typical produce bag"),
    ("box (40 lb)", 18.1437, "typical produce box"),
    ("crate", None, "variable weight"),
    ("dozen", None, "count based"),
    ("each", None, "count based"),
    ("bunch", None, "count based"),
    ("quart", None, "volume"),
]

# --------------------------------------------------------------------------- #
# Threats with type, severity, seasonal intensity by month (12), affected crops
# --------------------------------------------------------------------------- #
THREATS = [
    ("Drought", "climate", "Dry-season moisture stress reducing yields.", 4,
     [0.6, 0.7, 0.8, 0.7, 0.3, 0.2, 0.4, 0.4, 0.2, 0.1, 0.1, 0.3],
     ["Tomato", "Cabbage", "Carrot", "Escallion", "Sweet Pepper", "Corn", "Callaloo"]),
    ("Hurricane / Tropical Storm", "climate", "Wind and flood damage in the Atlantic season.", 5,
     [0.0, 0.0, 0.0, 0.0, 0.1, 0.3, 0.4, 0.7, 0.9, 0.7, 0.3, 0.0],
     ["Banana", "Plantain", "Papaya", "Pineapple", "Yellow Yam", "Dasheen"]),
    ("Flood Rains", "climate", "Excess rainfall causing waterlogging and losses.", 4,
     [0.2, 0.1, 0.1, 0.2, 0.4, 0.4, 0.3, 0.4, 0.5, 0.7, 0.6, 0.3],
     ["Callaloo", "Pak Choi", "Lettuce", "Tomato", "Sweet Pepper"]),
    ("Fall Armyworm", "pest", "Spodoptera frugiperda attacking maize and vegetables.", 4,
     [0.2, 0.3, 0.5, 0.6, 0.6, 0.5, 0.4, 0.4, 0.5, 0.4, 0.3, 0.2],
     ["Corn", "Callaloo", "Pak Choi"]),
    ("Beet Armyworm", "pest", "Spodoptera exigua damaging leafy greens.", 3,
     [0.3, 0.4, 0.5, 0.5, 0.4, 0.3, 0.3, 0.3, 0.4, 0.4, 0.3, 0.3],
     ["Callaloo", "Cabbage", "Pak Choi"]),
    ("Whitefly / Gall Midge", "pest", "Sap-sucking vectors affecting peppers/solanaceae.", 3,
     [0.3, 0.4, 0.5, 0.5, 0.4, 0.3, 0.3, 0.3, 0.4, 0.4, 0.4, 0.3],
     ["Sweet Pepper", "Scotch Bonnet Pepper", "Tomato"]),
    ("Bacterial Wilt", "disease", "Ralstonia wilt of solanaceous crops.", 4,
     [0.2, 0.2, 0.3, 0.4, 0.5, 0.5, 0.5, 0.5, 0.5, 0.4, 0.3, 0.2],
     ["Tomato", "Sweet Pepper", "Scotch Bonnet Pepper", "Irish Potato"]),
    ("Anthracnose", "disease", "Colletotrichum fruit/leaf rot in humid periods.", 3,
     [0.2, 0.2, 0.2, 0.3, 0.5, 0.6, 0.6, 0.6, 0.5, 0.4, 0.3, 0.2],
     ["Mango", "Yellow Yam", "Sweet Pepper"]),
    ("Papaya Ringspot Virus", "disease", "Aphid-borne virus devastating papaya.", 4,
     [0.4, 0.4, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5, 0.4, 0.4],
     ["Papaya"]),
    ("Praedial Larceny", "security", "Theft of crops/livestock before or at harvest.", 4,
     [0.3, 0.3, 0.3, 0.3, 0.4, 0.4, 0.4, 0.4, 0.5, 0.6, 0.8, 0.9],
     ["Yellow Yam", "Gungo Peas", "Sorrel", "Ginger", "Scotch Bonnet Pepper"]),
]

# --------------------------------------------------------------------------- #
# Suppliers (illustrative) with crop capabilities
# --------------------------------------------------------------------------- #
SUPPLIERS = [
    ("St. Elizabeth Farmers Co-op", "St. Elizabeth", "cooperative", 0.88,
     [("Tomato", 900, 4), ("Escallion", 400, 3), ("Scotch Bonnet Pepper", 300, 5),
      ("Pumpkin", 1200, 6), ("Thyme", 120, 3)]),
    ("Manchester Highland Growers", "Manchester", "cooperative", 0.85,
     [("Cabbage", 1500, 5), ("Carrot", 1000, 5), ("Irish Potato", 2000, 7),
      ("Lettuce", 400, 3)]),
    ("Clarendon Produce Aggregators", "Clarendon", "aggregator", 0.80,
     [("Yellow Yam", 2500, 7), ("Gungo Peas", 600, 10), ("Ackee", 500, 4),
      ("Sorrel", 400, 6), ("Mango", 800, 4), ("Tomato", 700, 6),
      ("Scotch Bonnet Pepper", 400, 6)]),
    ("St. Mary Banana & Plantain", "St. Mary", "farmer", 0.78,
     [("Banana", 1800, 3), ("Plantain", 1400, 3), ("Dasheen", 700, 5)]),
    ("Bog Walk Fresh (St. Catherine)", "St. Catherine", "aggregator", 0.82,
     [("Callaloo", 1000, 2), ("Pak Choi", 600, 2), ("Pineapple", 900, 5),
      ("Cucumber", 700, 3), ("Papaya", 800, 4), ("Tomato", 850, 4),
      ("Cabbage", 900, 5)]),
]

# --------------------------------------------------------------------------- #
# Illustrative production (all-island tonnes by quarter) for a few crops.
# --------------------------------------------------------------------------- #
ILLUSTRATIVE_PRODUCTION_YEARS = [2022, 2023, 2024]


def _seed_crops(repo: Repository) -> dict[str, int]:
    crop_ids: dict[str, int] = {}
    for (name, category, sci, seasonal, harvest, aliases, parishes, price) in CROPS:
        cid = repo.insert("crops", {
            "canonical_name": name, "category": category,
            "scientific_name": sci, "is_seasonal": seasonal,
            "notes": None,
        }, or_ignore=True)
        if cid == 0:
            cid = repo.crop_id_by_name(name)
        crop_ids[name] = cid
        for alias in aliases:
            repo.insert("crop_aliases",
                        {"crop_id": cid, "alias": alias, "source": "seed"},
                        or_ignore=True)
        # Modelled monthly supply index -> stored as calendar peak events.
        for m in harvest:
            repo.insert("crop_calendar_events", {
                "crop_id": cid, "event_type": "peak", "month": m,
                "weight": 1.0, "provenance": "modelled",
                "notes": "Modelled harvest peak (Jamaican seasonal knowledge)",
            })
        # A representative crop cycle.
        repo.insert("crop_cycles", {
            "crop_id": cid,
            "days_to_maturity_min": None, "days_to_maturity_max": None,
            "cycle_notes": None,
        })
    return crop_ids


def _seed_markets_units(repo: Repository) -> None:
    for (name, parish, mtype) in MARKETS:
        repo.insert("markets", {"name": name, "parish": parish,
                                "market_type": mtype}, or_ignore=True)
    for (name, kg, notes) in UNITS:
        repo.insert("units", {"name": name, "kg_equivalent": kg,
                              "notes": notes}, or_ignore=True)


def _seed_parish_registry(repo: Repository, crop_ids: dict[str, int]) -> None:
    """Illustrative registry footprint tying crops to sourcing parishes."""
    for (name, *_rest) in CROPS:
        parishes = _rest[5]  # sourcing parishes
        cid = crop_ids[name]
        for i, parish in enumerate(parishes):
            # Lead parish gets the largest footprint.
            farmers = 220 - i * 45
            hectares = round((180 - i * 35) * 0.9, 1)
            key = f"reg:{cid}:{parish}"
            repo.insert("parish_crop_registry", {
                "crop_id": cid, "parish": parish,
                "farmer_count": max(farmers, 25),
                "registered_hectares": max(hectares, 12.0),
                "provenance": "illustrative",
                "record_key": key,
            }, or_ignore=True)


def _seed_prices(repo: Repository, crop_ids: dict[str, int]) -> None:
    """Illustrative farmgate prices (JMD/kg) with a seasonal wobble.

    Price is higher in a crop's off-season (scarcity) and lower at peak. This
    is a modelled relationship over an illustrative base price, tagged
    ``illustrative`` so the UI never presents it as an official quote.
    """
    kg_unit = repo.query_one("SELECT id FROM units WHERE name='kg'")["id"]
    farmgate = repo.query_one("SELECT id FROM markets WHERE name LIKE 'Farmgate%'")["id"]
    for (name, category, sci, seasonal, harvest, aliases, parishes, base) in CROPS:
        cid = crop_ids[name]
        supply = core.build_supply_index(harvest)
        for year in ILLUSTRATIVE_PRODUCTION_YEARS:
            for q in range(1, 5):
                months = core.months_of_quarter(q)
                avail = sum(supply[m - 1] for m in months) / 3.0
                # +/- 30% around base, inverse to availability.
                factor = 1.30 - 0.6 * avail
                # Mild upward drift across years.
                drift = 1.0 + 0.05 * (year - ILLUSTRATIVE_PRODUCTION_YEARS[0])
                price = round(base * factor * drift, 1)
                key = f"price:{cid}:{year}:{q}:farmgate"
                repo.insert("price_records", {
                    "crop_id": cid, "market_id": farmgate, "unit_id": kg_unit,
                    "parish": None, "year": year, "quarter": q, "month": None,
                    "price_jmd": price, "price_per_kg_jmd": price,
                    "price_type": "farmgate", "provenance": "illustrative",
                    "record_key": key,
                }, or_ignore=True)


def _seed_production(repo: Repository, crop_ids: dict[str, int]) -> None:
    """Illustrative all-island quarterly production tonnes."""
    base_tonnes = {
        "Yellow Yam": 42000, "Sweet Potato": 26000, "Irish Potato": 12000,
        "Dasheen": 9000, "Cassava": 15000, "Tomato": 18000, "Cabbage": 22000,
        "Carrot": 14000, "Callaloo": 20000, "Pumpkin": 30000, "Escallion": 6000,
        "Plantain": 40000, "Banana": 60000, "Pineapple": 16000, "Scotch Bonnet Pepper": 5000,
        "Sweet Pepper": 6000, "Gungo Peas": 3000, "Sorrel": 1500, "Corn": 11000,
        "Watermelon": 20000, "Ackee": 8000,
    }
    for name, annual in base_tonnes.items():
        cid = crop_ids.get(name)
        if not cid:
            continue
        harvest = next(c[4] for c in CROPS if c[0] == name)
        supply = core.build_supply_index(harvest)
        for year in ILLUSTRATIVE_PRODUCTION_YEARS:
            q_shares = []
            for q in range(1, 5):
                months = core.months_of_quarter(q)
                q_shares.append(sum(supply[m - 1] for m in months))
            tot = sum(q_shares) or 1.0
            drift = 1.0 + 0.03 * (year - ILLUSTRATIVE_PRODUCTION_YEARS[0])
            for q in range(1, 5):
                tonnes = round(annual * drift * q_shares[q - 1] / tot, 1)
                key = f"prod:{cid}:{year}:{q}:allisland"
                repo.insert("production_records", {
                    "crop_id": cid, "parish": None, "year": year, "quarter": q,
                    "tonnes": tonnes, "provenance": "illustrative",
                    "record_key": key,
                }, or_ignore=True)


def _seed_threats(repo: Repository, crop_ids: dict[str, int]) -> None:
    for (name, ttype, desc, severity, monthly, crops) in THREATS:
        tid = repo.insert("threats", {
            "name": name, "threat_type": ttype, "description": desc,
            "severity": severity,
        }, or_ignore=True)
        if tid == 0:
            tid = repo.query_one("SELECT id FROM threats WHERE name=?", (name,))["id"]
        for m in range(1, 13):
            repo.insert("threat_seasonality", {
                "threat_id": tid, "month": m, "intensity": monthly[m - 1],
            }, or_ignore=True)
        for crop_name in crops:
            cid = crop_ids.get(crop_name)
            if cid:
                repo.insert("crop_threat_links", {
                    "crop_id": cid, "threat_id": tid,
                    "impact": min(5, severity),
                }, or_ignore=True)


def _seed_suppliers(repo: Repository, crop_ids: dict[str, int]) -> None:
    for (name, parish, stype, reliability, caps) in SUPPLIERS:
        sid = repo.insert("suppliers", {
            "name": name, "parish": parish, "contact": None,
            "supplier_type": stype, "reliability": reliability,
        }, or_ignore=True)
        if sid == 0:
            sid = repo.query_one("SELECT id FROM suppliers WHERE name=?", (name,))["id"]
        for (crop_name, kg_week, lead) in caps:
            cid = crop_ids.get(crop_name)
            if cid:
                repo.insert("supplier_crop_capabilities", {
                    "supplier_id": sid, "crop_id": cid,
                    "typical_kg_per_week": kg_week, "lead_time_days": lead,
                }, or_ignore=True)


def _seed_alerts(repo: Repository, crop_ids: dict[str, int]) -> None:
    repo.insert("alerts", {
        "level": "info",
        "title": "Illustrative seed data in use",
        "body": ("Prices and production tonnes are illustrative placeholders. "
                 "Ingest official Ministry/RADA files on the Administration page "
                 "to replace them with observed data."),
    })


def _seed_demo_procurement(repo: Repository, crop_ids: dict[str, int]) -> None:
    """Seed one worked RFQ -> quotation -> contract -> delivery lifecycle so the
    Procurement Integration KPIs are populated on first load. Clearly a demo.
    """
    # Imported here to avoid a circular import at module load time.
    from ..services.procurement_integration_service import (
        ProcurementIntegrationService,
    )
    from ..services.procurement_service import ProcurementService

    integ = ProcurementIntegrationService(repo)
    proc = ProcurementService(repo)
    cid = crop_ids.get("Tomato")
    if not cid:
        return

    rfq_id = integ.create_rfq("Kingston Hotel Group (demo)", cid, 600,
                              "2025-08-15", "Grade A, firm ripe")
    sups = proc.suppliers_for_crop(cid)
    demo_prices = [255.0, 268.0, 249.0]
    for i, (_, s) in enumerate(sups.iterrows()):
        integ.add_quotation(
            rfq_id, int(s["supplier_id"]),
            demo_prices[i % len(demo_prices)],
            float(s["typical_kg_per_week"]),
            int(s["lead_time_days"]), 3.5 + 0.4 * i)
    scored = integ.score_quotations(rfq_id)
    if scored.empty:
        return
    best_qid = int(scored.iloc[0]["id"])
    con = integ.award_contract(rfq_id, best_qid, 600, "2025-07-01", "2025-08-31")
    # A few deliveries: mostly good, one late with a small rejection.
    integ.record_delivery(con, "2025-07-08", 200, 200, 0, True, 4.5)
    integ.record_delivery(con, "2025-07-15", 200, 190, 10, True, 4.0)
    integ.record_delivery(con, "2025-07-22", 200, 205, 0, False, 3.5)
    integ.supplier_performance()  # persist rollups


def seed_all(repo: Repository) -> None:
    """Populate all reference and illustrative fact tables (idempotent)."""
    crop_ids = _seed_crops(repo)
    _seed_markets_units(repo)
    _seed_parish_registry(repo, crop_ids)
    _seed_prices(repo, crop_ids)
    _seed_production(repo, crop_ids)
    _seed_threats(repo, crop_ids)
    _seed_suppliers(repo, crop_ids)
    _seed_alerts(repo, crop_ids)
    _seed_demo_procurement(repo, crop_ids)
    repo.conn.commit()
