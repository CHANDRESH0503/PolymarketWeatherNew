"""Daemon-side forecast cache: the single place that calls Open-Meteo.

Each tick the trader calls `refresh_forecast_cache(con, markets)`, which fetches
the ensemble (and, for same-day markets, the intraday nowcast) once per
(station, date), persists each distribution for the dashboard, and returns the
in-memory scorers so `edge.generate_signals` can score against them *without*
fetching again. The dashboard then reads the persisted distributions and makes
zero Open-Meteo calls of its own.
"""
from __future__ import annotations

import sqlite3
import time

from ..config import STATIONS, CALIBRATION, NOWCAST, FORECAST_TTL
from ..forecast.openmeteo import fetch_max_temp_distribution
from ..forecast.model import apply_calibration
from ..forecast import nowcast as nowcast_mod
from ..forecast import dist_cache
from ..strategy.edge import _is_today
from ..polymarket.gamma import TempMarket
from . import store


def refresh_forecast_cache(con: sqlite3.Connection,
                           markets: list[TempMarket]) -> dict:
    """Fetch + persist the ensemble (and same-day nowcast) for every
    (station, date) in `markets`. Returns
    {(station, date): {"ensemble": MaxTempForecast(calibrated), "nowcast": Nowcast|None}}."""
    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for m in markets:
        st = m.station_code
        if not st or st not in STATIONS:
            continue
        key = (st, m.end_date[:10])
        if key not in seen:
            seen.add(key)
            keys.append(key)

    now = time.time()
    scorers: dict[tuple[str, str], dict] = {}
    for st, date in keys:
        s = STATIONS[st]
        # Reuse the cached ensemble if it's still fresh (models update ~every 6h),
        # so the daemon doesn't re-hit Open-Meteo every 15-min tick.
        cached = store.load_forecast_dist(con, st, date, "ensemble")
        if cached and (now - cached["ts"]) < FORECAST_TTL:
            fc = dist_cache.ensemble_from_payload(st, date, cached, CALIBRATION)
        else:
            try:
                fc = fetch_max_temp_distribution(s["lat"], s["lon"], date, s["tz"], st)
            except Exception as e:  # noqa: BLE001 — upstream rate limit / outage
                print(f"  ! forecast fetch failed for {st} {date}: {e}")
                # fall back to a stale cached forecast if we have one, so a
                # transient 429 doesn't drop the station from this tick entirely
                if cached:
                    fc = dist_cache.ensemble_from_payload(st, date, cached, CALIBRATION)
                else:
                    continue
            else:
                # Persist the RAW ensemble (dashboard's Forecast panel mirrors the
                # live endpoint, which showed the uncalibrated distribution).
                store.save_forecast_dist(con, st, date, "ensemble",
                                         dist_cache.ensemble_payload(fc))
                apply_calibration(fc, CALIBRATION)

        nc = None
        if _is_today(date, s["tz"]):
            try:
                nc = nowcast_mod.build_nowcast(st, date)
                store.save_forecast_dist(con, st, date, "nowcast",
                                         dist_cache.nowcast_payload(nc))
            except Exception as e:  # noqa: BLE001
                print(f"  ! nowcast failed for {st} {date}: {e}")

        scorers[(st, date)] = {"ensemble": fc, "nowcast": nc}
    return scorers


def cache_scorer(scorers: dict):
    """A `scorer_for(station, date)` for `generate_signals` that reuses the
    already-fetched forecasts. Uses the nowcast for same-day markets when
    NOWCAST is on, else the calibrated ensemble (mirrors `edge._build_scorer`)."""
    def scorer_for(station: str, date: str):
        entry = scorers.get((station, date))
        if not entry:
            return None
        s = STATIONS.get(station, {})
        if (NOWCAST and entry["nowcast"] is not None
                and _is_today(date, s.get("tz", "UTC"))):
            return entry["nowcast"]
        return entry["ensemble"]
    return scorer_for
