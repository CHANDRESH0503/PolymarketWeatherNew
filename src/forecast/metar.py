"""Real station observations (METAR/ASOS) — the actual resolution source.

Polymarket resolves on the highest temperature recorded at a specific airport
station (Wunderground, whole °C). ERA5 reanalysis (used as a stopgap) differs
from that by ~0.5-1.5°C. This module pulls the *same class of observation* that
resolves the market, from the Iowa Environmental Mesonet (IEM) ASOS archive —
free, global, and keyed by ICAO code (which is exactly our STATIONS key).

METAR temperatures are reported in whole °C, matching the market's rounding.
"""
from __future__ import annotations

import datetime as dt
import time

import requests

IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# IEM intermittently returns an empty body (header only, or nothing at all) for a
# query that succeeds on retry — observed ~1-in-3 under the audit's concurrency.
# Treating that transient empty as "no observations" silently drops resolved
# stations from the audit (Seoul kept losing the dice roll → never landed) and
# starves the live actuals-backfill + nowcast floor, which share this function.
# So we retry with backoff: an empty body for a PAST date that should have obs is
# retried; a genuinely empty result (future/unobserved date) just costs the
# retries then returns []. Connection reuse via a shared Session.
_RETRIES = 4
_BACKOFF = 1.5          # seconds, multiplied each attempt (1.5, 3.0, 6.0, …)
_session = requests.Session()


def _fetch_rows(icao: str, start: str, end: str, tz: str) -> list[tuple[str, float]]:
    """Raw (local_timestamp, tmpc) observations over [start, end] from IEM ASOS.

    Timestamps are 'YYYY-MM-DD HH:MM' in the station's local tz; only rows with a
    parseable temperature are returned. Retries transient empty/failed responses
    (see module note) so a flaky fetch can't masquerade as a station having no
    observations."""
    d2 = dt.date.fromisoformat(end) + dt.timedelta(days=1)   # IEM end is exclusive-ish
    d1 = dt.date.fromisoformat(start)
    params = {
        "station": icao, "data": "tmpc", "tz": tz,
        "format": "onlycomma", "latlon": "no", "missing": "empty",
        "year1": d1.year, "month1": d1.month, "day1": d1.day,
        "year2": d2.year, "month2": d2.month, "day2": d2.day,
    }
    for attempt in range(_RETRIES):
        try:
            r = _session.get(IEM_URL, params=params, timeout=40)
            r.raise_for_status()
            lines = r.text.splitlines()
        except Exception:  # noqa: BLE001
            lines = []
        rows: list[tuple[str, float]] = []
        for line in lines[1:]:                  # skip header
            parts = line.split(",")
            if len(parts) < 3:
                continue
            ts = parts[1]                       # 'YYYY-MM-DD HH:MM' (local)
            try:
                rows.append((ts, float(parts[2])))
            except ValueError:
                continue
        if rows:
            return rows
        # Empty/failed: retry with backoff (the data is usually there next time).
        if attempt < _RETRIES - 1:
            time.sleep(_BACKOFF * (attempt + 1))
    return []


def station_daily_max(icao: str, start: str, end: str, tz: str) -> dict[str, float]:
    """Map local-date -> max observed temperature (°C) over [start, end]."""
    out: dict[str, float] = {}
    for ts, t in _fetch_rows(icao, start, end, tz):
        day = ts[:10]
        if start <= day <= end:
            out[day] = max(out.get(day, -1e9), t)
    return out


def fetch_station_daily_max(icao: str, date: str, tz: str) -> float | None:
    """Actual recorded daily max for one station/date (resolution-aligned)."""
    return station_daily_max(icao, date, date, tz).get(date)


def station_obs_today(icao: str, date: str, tz: str
                      ) -> tuple[float | None, str | None, int]:
    """Intraday snapshot of `date` *so far*: (running_max_c, latest_ob_ts, n_obs).

    This is the Tier-3 nowcasting input — the same station observations that will
    ultimately resolve the market, read live. The running max is a HARD floor on
    the day's high: the daily max can only equal or exceed what's already been
    observed. Returns (None, None, 0) if no obs exist yet for the date.
    """
    day_rows = [(ts, t) for ts, t in _fetch_rows(icao, date, date, tz)
                if ts[:10] == date]
    if not day_rows:
        return None, None, 0
    running_max = max(t for _, t in day_rows)
    latest = max(ts for ts, _ in day_rows)
    return running_max, latest, len(day_rows)
