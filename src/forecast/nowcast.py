"""Tier 3 — intraday nowcasting from live observations.

The daily maximum temperature that resolves a market is

    M = max( observed-so-far , remaining-hours )

By early afternoon the *observed* part is already a hard floor: the day's high
can only equal or exceed what the station has already reported. As the
mid-afternoon peak (≈2–4 PM local) passes, the remaining hours cool off and can
no longer beat the floor, so the predictive distribution **collapses** onto the
observed max. The edge — if any — is pulling the same station obs the resolver
uses faster than the market reprices them.

Implementation: we keep the ensemble for the *remaining* hours only, draw Monte
Carlo samples of that remaining-hours max (member spread + a small residual),
clip each sample up to the observed floor, and read bucket probabilities off the
empirical sample distribution. Using samples (not a single Normal) is what lets
us represent the point mass that piles up exactly on the floor.

This is intentionally a sibling of forecast.model: same whole-°C rounding
convention (round-half-up via floor(x+0.5)), so its bucket probs are directly
comparable to the ensemble-only ones.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import numpy as np

from ..config import (CALIBRATION, NOWCAST_RESID_SIGMA, NOWCAST_SAMPLES,
                      STATIONS)
from .metar import station_obs_today
from .openmeteo import fetch_hourly_members


@dataclass
class Nowcast:
    station_code: str
    date: str
    samples_c: np.ndarray            # MC samples of the daily max (°C)
    observed_max_c: float | None     # running station max so far (the floor), None if no obs
    n_remaining_hours: int           # forecast hours still ahead of `now`
    latest_ob: str | None = None     # timestamp of the latest observation used

    @property
    def mean(self) -> float:
        return float(self.samples_c.mean())

    @property
    def std(self) -> float:
        return float(self.samples_c.std(ddof=1)) if self.samples_c.size > 1 else 0.0

    @property
    def floor_locked(self) -> float:
        """Fraction of probability already pinned to the observed floor — a
        'collapse meter' that rises toward 1.0 as the afternoon peak passes and
        the remaining hours can no longer beat what's been observed."""
        if self.observed_max_c is None:
            return 0.0
        return float((_rounded(self.samples_c) <= round(self.observed_max_c)).mean())


def _rounded(samples: np.ndarray) -> np.ndarray:
    # Round-half-up to whole °C, matching the market resolution + forecast.model.
    return np.floor(samples + 0.5)


def prob_exact(nc: Nowcast, degree: int) -> float:
    return float((_rounded(nc.samples_c) == degree).mean())


def prob_gte(nc: Nowcast, degree: int) -> float:
    return float((_rounded(nc.samples_c) >= degree).mean())


def prob_lte(nc: Nowcast, degree: int) -> float:
    return float((_rounded(nc.samples_c) <= degree).mean())


def yes_probability(nc: Nowcast, bucket_kind: str, degree: int) -> float:
    if bucket_kind == "exact":
        return prob_exact(nc, degree)
    if bucket_kind == "gte":
        return prob_gte(nc, degree)
    if bucket_kind == "lte":
        return prob_lte(nc, degree)
    raise ValueError(bucket_kind)


def nowcast_from_parts(station_code: str, date: str, times: list[str],
                       members: dict[str, np.ndarray],
                       observed_max_c: float | None, latest_ob: str | None,
                       *, tz: str, bias_c: float = 0.0,
                       resid_sigma: float = NOWCAST_RESID_SIGMA,
                       n_samples: int = NOWCAST_SAMPLES,
                       now: dt.datetime | None = None,
                       rng: np.random.Generator | None = None) -> Nowcast:
    """Assemble a Nowcast from already-fetched inputs (pure, easy to test).

    `times`/`members` are the hourly ensemble for `date` (local tz); only hours
    strictly after `now` are treated as still-forecast. `observed_max_c` is the
    running station floor. `bias_c` is the per-station calibration offset
    (subtracted from the forecast, same sign convention as MaxTempForecast)."""
    tzinfo = ZoneInfo(tz)
    now = now or dt.datetime.now(tzinfo)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tzinfo)
    rng = rng or np.random.default_rng()

    parsed = [dt.datetime.fromisoformat(t).replace(tzinfo=tzinfo) for t in times]
    remaining_idx = [i for i, t in enumerate(parsed) if t > now]

    # Per-member max over the remaining hours, bias-corrected to the station.
    rem_member_max: list[float] = []
    for arr in members.values():
        if not remaining_idx:
            break
        vals = arr[remaining_idx]
        vals = vals[~np.isnan(vals)]
        if vals.size:
            rem_member_max.append(float(vals.max()) - bias_c)
    rem = np.array(rem_member_max, dtype=float)

    floor = observed_max_c if observed_max_c is not None else -np.inf

    if rem.size:
        per = max(1, n_samples // rem.size)
        draws = rng.normal(rem[:, None], max(resid_sigma, 1e-6), size=(rem.size, per)).ravel()
        samples = np.maximum(draws, floor)
    elif observed_max_c is not None:
        # No hours left to forecast — the day is decided; collapse onto the floor.
        samples = np.full(n_samples, float(observed_max_c))
    else:
        raise RuntimeError(f"nowcast for {station_code} {date} has neither forecast nor obs")

    return Nowcast(station_code, date, samples, observed_max_c,
                   len(remaining_idx), latest_ob)


def build_nowcast(station_code: str, date: str, *,
                  calibration: dict | None = None,
                  resid_sigma: float = NOWCAST_RESID_SIGMA,
                  n_samples: int = NOWCAST_SAMPLES,
                  now: dt.datetime | None = None,
                  rng: np.random.Generator | None = None) -> Nowcast:
    """Fetch live inputs (ensemble hourly + station obs so far) and build a
    Nowcast for one station/date. `station_code` must be in STATIONS."""
    s = STATIONS[station_code]
    cal = (CALIBRATION if calibration is None else calibration).get(station_code, {})
    bias_c = float(cal.get("bias", 0.0))

    times, members = fetch_hourly_members(s["lat"], s["lon"], date, s["tz"])
    obs_max, latest, _ = station_obs_today(station_code, date, s["tz"])

    return nowcast_from_parts(
        station_code, date, times, members, obs_max, latest,
        tz=s["tz"], bias_c=bias_c, resid_sigma=resid_sigma,
        n_samples=n_samples, now=now, rng=rng)
