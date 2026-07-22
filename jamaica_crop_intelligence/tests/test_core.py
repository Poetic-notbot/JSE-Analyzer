"""Unit tests for pure calculation functions and seeding invariants."""

import pytest

from jamaica_crop_intelligence.calculations import core


def test_quarter_month_helpers():
    assert core.quarter_of_month(1) == 1
    assert core.quarter_of_month(12) == 4
    assert core.months_of_quarter(2) == (4, 5, 6)
    with pytest.raises(ValueError):
        core.quarter_of_month(13)


def test_supply_index_shape():
    curve = core.build_supply_index([1, 12])
    assert len(curve) == 12
    assert curve[0] == 1.0 and curve[11] == 1.0
    # shoulders around the peaks
    assert curve[1] == pytest.approx(0.4)   # Feb next to Jan
    assert curve[10] == pytest.approx(0.4)  # Nov next to Dec
    # wrap-around shoulder: Feb..Nov baseline elsewhere
    assert 0 < curve[5] < 0.4


def test_supply_index_wraparound():
    # Dec peak should give January a shoulder (calendar wraps).
    curve = core.build_supply_index([12])
    assert curve[0] == pytest.approx(0.4)


def test_normalize_and_peaks():
    curve = core.build_supply_index([6])
    norm = core.normalize_curve(curve)
    assert sum(norm) == pytest.approx(1.0)
    assert core.peak_months(curve) == [6]
    assert core.normalize_curve([0, 0, 0]) == [0, 0, 0]


def test_price_statistics():
    stats = core.price_statistics([100, 200, 300])
    assert stats["mean"] == pytest.approx(200)
    assert stats["median"] == pytest.approx(200)
    assert stats["min"] == 100 and stats["max"] == 300
    assert stats["cv"] > 0
    empty = core.price_statistics([])
    assert empty["count"] == 0


def test_to_price_per_kg():
    assert core.to_price_per_kg(100, 0.5) == pytest.approx(200)
    assert core.to_price_per_kg(100, None) is None
    assert core.to_price_per_kg(100, 0) is None


def test_required_procurement_and_gap():
    req = core.required_procurement_kg(1000, buffer_pct=0.1, loss_pct=0.15)
    # 1000*1.1/0.85
    assert req == pytest.approx(1294.117, rel=1e-3)
    assert core.supply_gap_kg(req, 1000) == pytest.approx(req - 1000)
    assert core.supply_gap_kg(500, 800) == 0.0
    assert core.coverage_ratio(800, 0) == 1.0
    with pytest.raises(ValueError):
        core.required_procurement_kg(100, loss_pct=1.0)


def test_score_quotations_ranks_cheaper_first():
    quotes = [
        {"id": 1, "price": 300, "lead_time": 5, "quality": 4, "reliability": 0.9},
        {"id": 2, "price": 200, "lead_time": 5, "quality": 4, "reliability": 0.9},
    ]
    scored = core.score_quotations(quotes)
    assert scored[0]["id"] == 2  # cheaper wins with equal everything
    assert scored[0]["rank"] == 1
    assert scored[0]["score"] >= scored[1]["score"]


def test_score_quotations_empty():
    assert core.score_quotations([]) == []


def test_contract_fulfillment():
    kpi = core.contract_fulfillment(500, [
        {"delivered_kg": 240, "rejected_kg": 10, "on_time": 1, "quality_rating": 4},
        {"delivered_kg": 260, "rejected_kg": 0, "on_time": 0, "quality_rating": 5},
    ])
    assert kpi["delivered_kg"] == 500
    assert kpi["accepted_kg"] == 490
    assert kpi["reject_rate"] == pytest.approx(10 / 500)
    assert kpi["on_time_rate"] == pytest.approx(0.5)
    assert kpi["avg_quality"] == pytest.approx(4.5)
    assert kpi["outstanding_kg"] == pytest.approx(10)


def test_supplier_rollup_and_planned_vs_actual():
    roll = core.supplier_rollup([
        {"ordered_kg": 100, "delivered_kg": 100, "rejected_kg": 0, "on_time": 1, "quality_rating": 5},
        {"ordered_kg": 100, "delivered_kg": 80, "rejected_kg": 20, "on_time": 0, "quality_rating": 3},
    ])
    assert roll["deliveries_count"] == 2
    assert roll["fill_rate"] == pytest.approx((100 + 60) / 200)
    pva = core.planned_vs_actual(500, 480)
    assert pva["status"] == "shortfall"
    assert pva["variance_kg"] == -20


def test_threat_exposure_monotonic():
    low = core.threat_exposure_score(2, 0.2, 0.2)
    high = core.threat_exposure_score(5, 0.9, 1.0)
    assert 0 <= low < high <= 100
