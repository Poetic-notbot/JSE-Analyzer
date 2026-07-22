"""Tests for the forecasting engine and the spike-alert engine."""

import pytest

from jamaica_crop_intelligence.calculations import forecast as fc
from jamaica_crop_intelligence.services.alert_service import AlertService
from jamaica_crop_intelligence.services.forecast_service import ForecastService


def test_forecast_rising_trend_increases():
    pts = fc.seasonal_trend_forecast([100, 110, 120, 130, 140, 150, 160, 170],
                                     season_length=4, horizon=3)
    assert len(pts) == 3
    assert pts[0].point < pts[1].point < pts[2].point
    for p in pts:
        assert p.lower <= p.point <= p.upper


def test_forecast_short_series_is_naive():
    pts = fc.seasonal_trend_forecast([50, 55], season_length=4, horizon=2)
    assert len(pts) == 2
    assert all(p.point == 55 for p in pts)  # repeats last value


def test_forecast_non_negative_floor():
    # steeply falling series must not forecast below zero when non_negative
    pts = fc.seasonal_trend_forecast([100, 80, 60, 40, 20, 10, 5, 2],
                                     season_length=4, horizon=6)
    assert all(p.point >= 0 and p.lower >= 0 for p in pts)


def test_backtest_mape_reasonable():
    # a clean linear series should backtest with low error
    vals = [100 + 5 * i for i in range(16)]
    mape = fc.backtest_mape(vals, season_length=4, holdout=4)
    assert mape is not None
    assert mape < 5.0


def test_forecast_service_labels_modelled(repo):
    fs = ForecastService(repo)
    cid = repo.crop_id_by_name("Tomato")
    out = fs.forecast_price(cid, horizon=4)
    assert out["provenance"] == "modelled"
    assert not out["history"].empty
    assert len(out["forecast"]) == 4


def test_alert_spikes_and_generation(repo):
    als = AlertService(repo)
    # seasonal seed prices swing enough to trip a low threshold
    spikes = als.price_spikes(threshold_pct=5)
    assert not spikes.empty
    assert (spikes["change_pct"].abs() >= 5).all()

    before = repo.table_count("alerts")
    created = als.generate_alerts(threshold_pct=5)
    assert created > 0
    assert repo.table_count("alerts") == before + created
    # idempotent-ish: same titles are not duplicated
    again = als.generate_alerts(threshold_pct=5)
    assert again == 0


def test_top_volatile_returns_five(repo):
    als = AlertService(repo)
    df = als.top_volatile(5)
    assert len(df) <= 5
    assert "volatility_cv" in df.columns


def test_watchlist_bundle(repo):
    als = AlertService(repo)
    wl = als.watchlist(threshold_pct=10, n=5)
    assert set(wl) == {"top_volatile", "spikes"}
