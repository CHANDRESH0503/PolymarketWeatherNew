"""Scan open weather events for coherence / negative-risk arbitrage.

    python scripts/arb_scan.py                 # all open temperature events
    python scripts/arb_scan.py --min-edge 0.01 # lower the threshold

A non-forecast, model-free edge: it only reads live order books and checks
whether each event's mutually-exclusive buckets price coherently (Σ YES = 1).
Reports opportunities sorted by ROI. Nothing is traded — this is a detector.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.polymarket.gamma import fetch_open_temperature_events
from src.strategy.arbitrage import scan_event, execute_opportunity
from src.config import ARB_EXECUTE, ARB_MIN_PROFIT, ARB_MAX_CAPITAL, DRY_RUN
from src import notify


def scan_once(min_edge: float, execute: bool) -> None:
    events = fetch_open_temperature_events()
    opps = []
    for ev in events:
        try:
            opps += scan_event(ev, min_edge)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {ev.get('slug','')}: {e}")
    opps.sort(key=lambda o: o.roi_pct, reverse=True)
    stamp = time.strftime("%H:%M:%S")
    if not opps:
        print(f"[{stamp}] scanned {len(events)} events — no arb ≥ "
              f"{min_edge*100:.1f}% (spreads straddle 1.0)")
        return
    print(f"[{stamp}] {len(opps)} opportunity(ies):")
    total = 0.0
    for o in opps:
        print("   ", o)
        if o.kind == "underpriced":
            total += o.est_profit
        # auto-execute the long basket if enabled and it clears the profit floor
        if (execute and o.kind == "underpriced"
                and o.profit_per_basket >= ARB_MIN_PROFIT):
            res = execute_opportunity(o, ARB_MAX_CAPITAL)
            print(f"      EXECUTE → {res}")
            if not res.get("skipped"):
                notify.send(
                    f"🟡 *Arb basket* {'(DRY)' if DRY_RUN else 'LIVE'}\n"
                    f"_{o.event_slug}_\nΣask {o.price_sum:.3f} → edge "
                    f"{o.profit_per_basket*100:+.1f}% · est ${res.get('expected_profit',0):.2f}"
                    + ("\n⚠ partial fill — directional exposure!"
                       if res.get("partial_fill_risk") else ""))
    if total:
        print(f"    → executable buy-all-YES profit ≈ ${total:.2f} "
              f"(best-level depth; verify book + neg-risk convert)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=0.015,
                    help="minimum mispricing (basket sum vs 1.0) to report")
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between scans; 0 = once. Arb windows are "
                         "intermittent (~1h), so loop to catch them.")
    ap.add_argument("--execute", action="store_true",
                    help="auto-execute baskets (also needs ARB_EXECUTE=1; orders "
                         "still DRY_RUN unless DRY_RUN=0 + funded PK)")
    args = ap.parse_args()
    execute = args.execute and ARB_EXECUTE
    mode = ("EXECUTE " + ("(DRY_RUN sim)" if DRY_RUN else "LIVE ORDERS")
            if execute else "detect-only")
    print(f"Coherence-arb scanner — {mode} — min edge {args.min_edge*100:.1f}%, "
          f"act ≥ {ARB_MIN_PROFIT*100:.1f}%, cap ${ARB_MAX_CAPITAL:.0f}\n")
    if args.execute and not ARB_EXECUTE:
        print("  (--execute ignored: set ARB_EXECUTE=1 in .env to arm)\n")
    if args.loop <= 0:
        scan_once(args.min_edge, execute)
        return
    while True:
        try:
            scan_once(args.min_edge, execute)
        except Exception as e:  # noqa: BLE001
            print(f"scan error: {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()


if __name__ == "__main__":
    main()
