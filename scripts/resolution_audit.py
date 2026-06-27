"""Audit whether our METAR daily-max matches the actual Polymarket resolution.

Runs on resolved markets, stores per-event results in the paper DB (so the
dashboard can read them), and prints a summary. This is the load-bearing check:
if the match rate is low or |Δ°C| is large, the resolution source is wrong and
no downstream edge is real.

    python scripts/resolution_audit.py            # ~6 pages of closed events
    python scripts/resolution_audit.py --pages 20 # bigger sample
    python scripts/resolution_audit.py --loop 21600   # refresh every 6h
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.analysis.resolution_audit import audit_events, summarize
from src.paper import store
from scripts.market_backtest import fetch_closed_events


def run_once(pages: int, limit: int) -> None:
    events = fetch_closed_events(pages, limit)
    print(f"closed temperature events: {len(events)}  — auditing METAR vs resolution…",
          flush=True)
    rows = audit_events(events)
    con = store.connect()
    store.save_audit(con, rows)
    s = summarize(rows)
    if not s.get("n"):
        print("no exact-winner events with METAR coverage yet.")
        return
    print(f"\n── Resolution-source audit (n={s['n']}) ──")
    print(f"  exact match       : {s['matched']}/{s['n']} = {s['match_rate']*100:.1f}%")
    print(f"  within ±1°C       : {s['within1_rate']*100:.1f}%")
    print(f"  mean |Δ°C|        : {s['mean_abs_delta']:.2f}")
    print("  Δ (METAR − resolved) histogram:")
    for h in s["hist"]:
        print(f"    {h['delta']:+d}°C  {'█'*h['count']} {h['count']}")
    print("  per station (match% · mean Δ):")
    for p in s["per_station"]:
        print(f"    {p['station']} {p['city']:<11} n={p['n']:<3} "
              f"{p['match_rate']*100:4.0f}%   {p['mean_delta']:+.2f}")
    verdict = ("FAITHFUL ✓ — METAR tracks the resolution source"
               if s["match_rate"] >= 0.8 else
               "SUSPECT ✗ — source mismatch; fix before trusting any edge"
               if s["match_rate"] < 0.6 else
               "MARGINAL — verify window/rounding/station")
    print(f"  → {verdict}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=6, help="x100 closed events to scan")
    ap.add_argument("--limit", type=int, default=0, help="cap # events")
    ap.add_argument("--loop", type=int, default=0, help="seconds between runs; 0 = once")
    args = ap.parse_args()
    if args.loop <= 0:
        run_once(args.pages, args.limit)
        return
    while True:
        try:
            run_once(args.pages, args.limit)
        except Exception as e:  # noqa: BLE001
            print(f"audit error: {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
