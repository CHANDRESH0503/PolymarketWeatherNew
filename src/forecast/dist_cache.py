"""(De)serialize forecast distributions so the daemon can persist them and the
dashboard can rebuild bucket probabilities without re-calling Open-Meteo.

The daemon is the single upstream caller: each tick it fetches the ensemble (and,
for same-day markets, the intraday nowcast), stores a compact JSON payload via
`paper.store.save_forecast_dist`, and the dashboard reads it back here.

- Ensemble payload keeps the *raw* member daily-maxes (+ mean/std). The dashboard
  reconstructs a `MaxTempForecast` and computes bucket probs with the same
  `forecast.model` functions — optionally applying calibration, matching whatever
  the corresponding live endpoint used to do.
- Nowcast payload keeps the rounded-degree PMF (+ observed floor / collapse
  metrics). Since the market buckets are whole-°C, the PMF reproduces the exact /
  gte / lte probabilities exactly, without shipping thousands of MC samples.
"""
from __future__ import annotations

import numpy as np

from .openmeteo import MaxTempForecast
from .model import apply_calibration


def ensemble_payload(fc: MaxTempForecast) -> dict:
    """Serialize a (raw, pre-calibration) ensemble forecast."""
    return {
        "members": [float(x) for x in fc.members_max_c],
        "mean": fc.mean,
        "std": fc.std,
    }


def ensemble_from_payload(station: str, date: str, payload: dict,
                          calibration: dict | None = None) -> MaxTempForecast:
    """Rebuild a `MaxTempForecast` from a stored payload. Pass `calibration` to
    apply per-station EMOS/bias (leave None to keep the raw ensemble)."""
    members = np.asarray(payload["members"], dtype=float)
    fc = MaxTempForecast(station_code=station, date=date, members_max_c=members)
    if calibration is not None:
        apply_calibration(fc, calibration)
    return fc


def nowcast_payload(nc) -> dict:
    """Serialize a `nowcast.Nowcast`: rounded-degree PMF + collapse metrics."""
    rounded = np.floor(nc.samples_c + 0.5).astype(int)
    vals, counts = np.unique(rounded, return_counts=True)
    n = int(rounded.size) or 1
    pmf = {int(v): float(c) / n for v, c in zip(vals, counts)}
    return {
        "pmf": pmf,
        "observed_max": nc.observed_max_c,
        "latest_ob": nc.latest_ob,
        "remaining_hours": nc.n_remaining_hours,
        "floor_locked": nc.floor_locked,
        "mean": nc.mean,
        "std": nc.std,
    }


def nowcast_prob(payload: dict, bucket_kind: str, degree: int) -> float:
    """P(Yes) for a bucket from a stored nowcast PMF (keys are JSON strings)."""
    items = [(int(k), v) for k, v in payload["pmf"].items()]
    if bucket_kind == "exact":
        return float(sum(v for d, v in items if d == degree))
    if bucket_kind == "gte":
        return float(sum(v for d, v in items if d >= degree))
    if bucket_kind == "lte":
        return float(sum(v for d, v in items if d <= degree))
    raise ValueError(bucket_kind)
