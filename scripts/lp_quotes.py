"""Liquidity-provider quote sheet (Tier-1 edge #2).

Prints fair-value-anchored two-sided quotes for weather events, flagging which
buckets are reward-eligible and where to lean to avoid adverse selection.

    python scripts/lp_quotes.py                              # held/our cities
    python scripts/lp_quotes.py --event highest-temperature-in-busan-on-june-5-2026
    python scripts/lp_quotes.py --half-spread 0.015
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import STATIONS
from src.polymarket.gamma import fetch_open_temperature_events
from src.strategy.market_making import quote_event


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--event", default="", help="single event slug")
    ap.add_argument("--half-spread", type=float, default=0.02)
    ap.add_argument("--limit", type=int, default=4, help="events to show")
    args = ap.parse_args()

    events = fetch_open_temperature_events()
    if args.event:
        events = [e for e in events if e["slug"] == args.event]
    else:
        events = [e for e in events
                  if any(m.get("clobTokenIds") for m in e.get("markets", []))][:args.limit]

    for ev in events:
        quotes = quote_event(ev, args.half_spread)
        if not quotes:
            continue
        elig = sum(1 for q in quotes if q.reward_eligible)
        print(f"\n§ {ev['slug']}   ({elig}/{len(quotes)} buckets reward-eligible)")
        print(f"  {'bkt':>6}  {'fair':>5}  {'mid':>5}  {'spr':>5}  quote      flags")
        for q in quotes:
            print(q)
    print("\nLegend: ✓rwd = within reward band (earns LP rewards);  "
          "[bid-only]/[ask-only] = our fair disagrees with mid beyond the band, "
          "so quote one side to avoid adverse selection.")
    print("Execution stays guarded (clob.py, DRY_RUN). This is the quote sheet.")


if __name__ == "__main__":
    main()
