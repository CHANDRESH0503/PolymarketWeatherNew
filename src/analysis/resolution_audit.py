"""Resolution-source fidelity audit.

The entire strategy rests on one assumption: the station daily-max we compute
from IEM ASOS METAR, rounded to whole °C, equals the number Polymarket actually
resolves on (Wunderground station obs). If those disagree by even ~1°C, every
modeled probability is aimed at the wrong target and all "edge" is noise.

This module measures that directly on RESOLVED markets: for each closed event it
reads the winning whole-degree from Polymarket, fetches our METAR daily max for
the same station/date, and compares. No waiting for our own trades to settle —
it runs on the public resolution history right now.
"""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor

from ..config import STATIONS
from ..forecast.metar import fetch_station_daily_max
from ..polymarket.gamma import parse_event, _as_list


def _round_half_up(x: float) -> int:
    return int(math.floor(x + 0.5))


def winning_degree(ev: dict) -> tuple[str, str, int] | None:
    """(station, date, resolved_degree) from the EXACT bucket that resolved Yes.

    We use exact-degree winners only — they pin the resolution to a single whole
    degree, giving a clean apples-to-apples comparison. Tail winners ('X or
    higher/below') don't, so they're skipped here."""
    markets = parse_event(ev)
    station = next((m.station_code for m in markets if m.station_code in STATIONS), None)
    if not station:
        return None
    resolved: dict[str, int] = {}
    for m in ev.get("markets", []):
        op = _as_list(m.get("outcomePrices"))
        outs = _as_list(m.get("outcomes"))
        if op:
            yi = outs.index("Yes") if "Yes" in outs else 0
            resolved[m.get("conditionId")] = 1 if float(op[yi]) > 0.5 else 0
    for tm in markets:
        if tm.bucket_kind == "exact" and resolved.get(tm.condition_id) == 1:
            return station, tm.end_date[:10], tm.threshold_c
    return None


def audit_event(ev: dict) -> dict | None:
    """One audit row, or None if no exact winner / no METAR for it."""
    win = winning_degree(ev)
    if not win:
        return None
    station, date, resolved_deg = win
    tz = STATIONS[station]["tz"]
    metar_max = fetch_station_daily_max(station, date, tz)
    if metar_max is None:
        return None
    metar_deg = _round_half_up(metar_max)
    return {
        "station": station, "date": date, "resolved_deg": resolved_deg,
        "metar_max": round(metar_max, 1), "metar_deg": metar_deg,
        "delta": metar_deg - resolved_deg, "matched": int(metar_deg == resolved_deg),
    }


def audit_events(events: list[dict], workers: int = 6) -> list[dict]:
    with ThreadPoolExecutor(max_workers=workers) as ex:
        rows = list(ex.map(audit_event, events))
    return [r for r in rows if r]


def summarize(rows: list[dict]) -> dict:
    """Aggregate match rate, mean |Δ°C|, the Δ histogram, and a per-station table."""
    n = len(rows)
    if not n:
        return {"n": 0}
    matched = sum(r["matched"] for r in rows)
    within1 = sum(1 for r in rows if abs(r["delta"]) <= 1)
    mean_abs = sum(abs(r["delta"]) for r in rows) / n
    hist: dict[int, int] = {}
    per: dict[str, dict] = {}
    for r in rows:
        hist[r["delta"]] = hist.get(r["delta"], 0) + 1
        p = per.setdefault(r["station"], {"n": 0, "matched": 0, "sum_delta": 0})
        p["n"] += 1; p["matched"] += r["matched"]; p["sum_delta"] += r["delta"]
    per_station = [{
        "station": st, "city": STATIONS.get(st, {}).get("city", st),
        "n": p["n"], "match_rate": p["matched"] / p["n"],
        "mean_delta": p["sum_delta"] / p["n"],
    } for st, p in sorted(per.items())]
    return {
        "n": n, "matched": matched, "match_rate": matched / n,
        "within1_rate": within1 / n, "mean_abs_delta": mean_abs,
        "hist": [{"delta": d, "count": c} for d, c in sorted(hist.items())],
        "per_station": per_station,
    }
