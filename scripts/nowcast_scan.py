"""Tier 3 — intraday nowcast scanner / market-slowness probe.

For every open *same-day* temperature market in our stations, compare three
numbers side by side:

  ENS  : P(Yes) from the plain multi-model ensemble (no live obs)
  NOW  : P(Yes) from the nowcast (observed station floor + remaining-hours only)
  MKT  : the current market YES price

The nowcast sharpens as the afternoon peak passes (watch the "lock" column climb
toward 100% — that's mass pinned to the already-observed max). The whole point of
Tier 3 is the NOW-vs-MKT gap: if the market is slow to reprice the live station
obs the resolver uses, that lag shows up here as a persistent edge. If NOW≈MKT,
the market is already fast and there's no speed alpha — which is the honest null
we expect until proven otherwise.

    python scripts/nowcast_scan.py                 # all today markets, our stations
    python scripts/nowcast_scan.py --station RKSI   # one station
    python scripts/nowcast_scan.py --min-edge 0.10  # only show NOW-MKT gaps >= 10c
    python scripts/nowcast_scan.py --loop 300       # repeat every 5 min

Nothing is traded — this is a detector.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import CALIBRATION, STATIONS
from src.forecast import nowcast as nc_mod
from src.forecast.model import apply_calibration, yes_probability
from src.forecast.openmeteo import fetch_max_temp_distribution
from src.polymarket.gamma import fetch_open_temperature_events, parse_event
from src.strategy.edge import _is_today


def scan_once(only_station: str | None, min_edge: float) -> None:
    events = fetch_open_temperature_events()
    markets = [m for ev in events for m in parse_event(ev)]

    # Group today's markets for our stations by (station, date).
    groups: dict[tuple[str, str], list] = {}
    for m in markets:
        st = m.station_code
        if not st or st not in STATIONS:
            continue
        if only_station and st != only_station:
            continue
        date = m.end_date[:10]
        if not _is_today(date, STATIONS[st]["tz"]):
            continue
        groups.setdefault((st, date), []).append(m)

    stamp = time.strftime("%H:%M:%S")
    if not groups:
        print(f"[{stamp}] no open same-day markets for "
              f"{only_station or 'our stations'} right now.")
        return

    for (st, date), ms in sorted(groups.items()):
        s = STATIONS[st]
        try:
            nc = nc_mod.build_nowcast(st, date)
        except Exception as e:  # noqa: BLE001
            print(f"[{stamp}] {st} {date}: nowcast failed: {e}")
            continue
        try:
            fc = apply_calibration(
                fetch_max_temp_distribution(s["lat"], s["lon"], date, s["tz"], st),
                CALIBRATION)
        except Exception:  # noqa: BLE001
            fc = None

        floor = f"{nc.observed_max_c:.0f}°C" if nc.observed_max_c is not None else "—"
        print(f"\n[{stamp}] {s['city']} ({st}) {date}  "
              f"obs-max={floor}  latest={nc.latest_ob or '—'}  "
              f"rem-hrs={nc.n_remaining_hours}  lock={nc.floor_locked*100:.0f}%  "
              f"nowcast μ={nc.mean:.1f}±{nc.std:.1f}")
        print(f"    {'bucket':<10} {'ENS':>6} {'NOW':>6} {'MKT':>6} {'NOW-MKT':>8}")
        for m in sorted(ms, key=lambda x: (x.threshold_c, x.bucket_kind)):
            label = {"exact": f"{m.threshold_c}°C",
                     "gte": f">={m.threshold_c}°C",
                     "lte": f"<={m.threshold_c}°C"}.get(m.bucket_kind, str(m.threshold_c))
            now_p = nc_mod.yes_probability(nc, m.bucket_kind, m.threshold_c)
            ens_p = (yes_probability(fc, m.bucket_kind, m.threshold_c)
                     if fc is not None else float("nan"))
            gap = now_p - m.yes_price
            flag = "  <<" if abs(gap) >= min_edge else ""
            print(f"    {label:<10} {ens_p:>6.2f} {now_p:>6.2f} "
                  f"{m.yes_price:>6.2f} {gap:>+8.2f}{flag}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--station", help="restrict to one ICAO station code")
    ap.add_argument("--min-edge", type=float, default=0.07,
                    help="flag NOW-vs-MKT gaps at/above this (default 0.07)")
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between scans; 0 = run once")
    args = ap.parse_args()

    if args.loop <= 0:
        scan_once(args.station, args.min_edge)
        return
    while True:
        try:
            scan_once(args.station, args.min_edge)
        except Exception as e:  # noqa: BLE001
            print(f"scan error: {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
