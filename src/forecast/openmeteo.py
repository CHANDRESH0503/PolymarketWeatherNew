"""Fetch ensemble temperature forecasts from Open-Meteo (free, no API key).

The key to an edge in these markets is a *probability distribution* over the
daily maximum temperature, not a single number. We get that by pulling every
member of multiple ensemble models (GFS, ICON, ECMWF) and treating the spread
of member daily-maxes as the predictive distribution.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
import requests

from ..config import OPENMETEO_API_KEY

# Free-tier hosts by default; with an API key, the higher-limit customer hosts.
if OPENMETEO_API_KEY:
    ENSEMBLE_URL = "https://customer-ensemble-api.open-meteo.com/v1/ensemble"
    ARCHIVE_URL = "https://customer-archive-api.open-meteo.com/v1/archive"
else:
    ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
    ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
# Models with hourly temperature ensemble members available on Open-Meteo.
MODELS = "gfs_seamless,icon_seamless,ecmwf_ifs025"

# Open-Meteo's free tier rate-limits (HTTP 429) per minute/hour/day. A handful
# of concurrent dashboard requests + the scan daemon can trip the per-minute
# limit, so retry transient 429/5xx with backoff (honouring Retry-After) rather
# than failing the whole forecast.
_RETRY_STATUS = {429, 500, 502, 503, 504}


def _get(url: str, params: dict, *, timeout: int = 30,
         retries: int = 4, backoff: float = 2.0) -> requests.Response:
    if OPENMETEO_API_KEY:
        params = {**params, "apikey": OPENMETEO_API_KEY}
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code in _RETRY_STATUS and attempt < retries - 1:
                wait = float(r.headers.get("Retry-After") or backoff * (2 ** attempt))
                time.sleep(min(wait, 30.0))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:  # network blips / raise_for_status
            last_exc = e
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
                continue
            raise
    if last_exc:  # pragma: no cover - defensive
        raise last_exc
    raise RuntimeError(f"request to {url} failed without an exception")

# Default extra uncertainty (°C) added in quadrature to ensemble spread.
# Overridden per-station once calibration data exists.
DEFAULT_SIGMA_FLOOR = 1.2


@dataclass
class MaxTempForecast:
    station_code: str
    date: str
    members_max_c: np.ndarray   # one daily-max per ensemble member
    bias_c: float = 0.0         # learned calibration offset (subtracted from mean)
    sigma_floor: float = DEFAULT_SIGMA_FLOOR
    sigma_min: float = 0.0      # per-station HARD floor on final σ (targeted humility
                                # for overconfident/drifting stations; 0 = none)
    emos: tuple | None = None   # (a,b,c,d): μ=a+b·mean, σ²=c+d·var (overrides above)

    @property
    def mean(self) -> float:
        return float(self.members_max_c.mean())

    @property
    def std(self) -> float:
        return float(self.members_max_c.std(ddof=1))


def fetch_hourly_members(lat: float, lon: float, date: str, tz: str,
                         models: str = MODELS) -> tuple[list[str], dict[str, np.ndarray]]:
    """Raw per-member hourly 2m-temperature series for `date` (local tz).

    Returns (times, members) where `times` are local 'YYYY-MM-DDTHH:MM' strings
    and `members` maps each member key to a float array (NaN for missing hours),
    aligned to `times`. This is the building block for both the full-day max
    distribution and the Tier-3 intraday nowcaster (which needs to split the day
    into observed-so-far vs remaining hours).
    """
    r = _get(
        ENSEMBLE_URL,
        {
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m",
            "models": models,
            "timezone": tz,
            "start_date": date, "end_date": date,
        },
    )
    hourly = r.json()["hourly"]
    times = list(hourly["time"])
    # Every member series is keyed temperature_2m, temperature_2m_member01, ...
    member_keys = [k for k in hourly if k.startswith("temperature_2m")]
    members = {
        k: np.array([np.nan if v is None else v for v in hourly[k]], dtype=float)
        for k in member_keys
    }
    return times, members


def fetch_max_temp_distribution(lat: float, lon: float, date: str,
                                tz: str, station_code: str = "",
                                models: str = MODELS) -> MaxTempForecast:
    """Return the distribution of the daily maximum 2m temperature for `date`.

    We request hourly temps for all ensemble members, then take, per member,
    the max over the local-day's hours.
    """
    _, members = fetch_hourly_members(lat, lon, date, tz, models)
    maxes = []
    for arr in members.values():
        a = arr[~np.isnan(arr)]
        if a.size:
            maxes.append(a.max())
    members_max = np.array(maxes, dtype=float)
    if members_max.size == 0:
        raise RuntimeError(f"No ensemble data returned for {station_code} {date}")
    return MaxTempForecast(station_code=station_code, date=date, members_max_c=members_max)


def fetch_actual_max(lat: float, lon: float, date: str, tz: str) -> float | None:
    """Actual recorded daily max (ERA5 reanalysis archive).

    NOTE: this is a *proxy* for the resolution source (Wunderground station obs);
    they can differ ~0.5-1.5°C. For live trades the authoritative truth is the
    Polymarket resolution itself; ERA5 is used for historical backtesting where
    we have no trade.
    """
    try:
        r = _get(
            ARCHIVE_URL,
            {"latitude": lat, "longitude": lon,
             "daily": "temperature_2m_max", "timezone": tz,
             "start_date": date, "end_date": date},
        )
        vals = r.json().get("daily", {}).get("temperature_2m_max", [])
        return float(vals[0]) if vals and vals[0] is not None else None
    except Exception:  # noqa: BLE001
        return None
