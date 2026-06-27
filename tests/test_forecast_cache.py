"""The daemon must be the sole Open-Meteo caller, and within the freshness TTL it
should reuse the cached forecast instead of re-fetching every tick."""
import datetime as dt

import numpy as np

from src.paper import store, forecast_cache
from src.forecast.openmeteo import MaxTempForecast
from src.polymarket.gamma import TempMarket


def _market(station, date):
    return TempMarket("e", "m", "q", "c", "Y", "N", 0.5, 0.5,
                      "exact", 25, station, date + "T21:00:00Z")


def test_forecast_ttl_reuses_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    con = store.connect()

    calls = {"n": 0}
    def fake_fetch(lat, lon, date, tz, st=""):
        calls["n"] += 1
        return MaxTempForecast(st, date, np.linspace(24, 26, 30))
    monkeypatch.setattr(forecast_cache, "fetch_max_temp_distribution", fake_fetch)

    # A future date so the same-day nowcast path is skipped (ensemble only).
    future = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    markets = [_market("RJTT", future)]

    forecast_cache.refresh_forecast_cache(con, markets)   # cold: must fetch
    forecast_cache.refresh_forecast_cache(con, markets)   # warm: must reuse cache
    assert calls["n"] == 1


def test_forecast_ttl_refetches_when_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(forecast_cache, "FORECAST_TTL", 0)   # everything is "stale"
    con = store.connect()

    calls = {"n": 0}
    def fake_fetch(lat, lon, date, tz, st=""):
        calls["n"] += 1
        return MaxTempForecast(st, date, np.linspace(24, 26, 30))
    monkeypatch.setattr(forecast_cache, "fetch_max_temp_distribution", fake_fetch)

    future = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    markets = [_market("RJTT", future)]
    forecast_cache.refresh_forecast_cache(con, markets)
    forecast_cache.refresh_forecast_cache(con, markets)
    assert calls["n"] == 2   # TTL=0 -> re-fetches each tick


def test_stale_cache_used_when_fetch_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DB_PATH", tmp_path / "t.db")
    con = store.connect()
    future = (dt.date.today() + dt.timedelta(days=3)).isoformat()
    markets = [_market("RJTT", future)]

    # First tick: a real (fake) forecast lands in the cache.
    monkeypatch.setattr(forecast_cache, "fetch_max_temp_distribution",
                        lambda *a, **k: MaxTempForecast("RJTT", future, np.linspace(24, 26, 30)))
    forecast_cache.refresh_forecast_cache(con, markets)

    # Later tick past the TTL but the API is down (429): must reuse the stale copy,
    # not drop the station.
    monkeypatch.setattr(forecast_cache, "FORECAST_TTL", 0)
    def boom(*a, **k):
        raise RuntimeError("429 Too Many Requests")
    monkeypatch.setattr(forecast_cache, "fetch_max_temp_distribution", boom)
    scorers = forecast_cache.refresh_forecast_cache(con, markets)
    assert ("RJTT", future) in scorers
