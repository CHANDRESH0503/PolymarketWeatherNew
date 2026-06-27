"""Main bot loop.

  1. Discover open daily high-temperature markets on Polymarket.
  2. For each station/date, pull an ensemble max-temp forecast.
  3. Convert the forecast into bucket probabilities matching the resolution rule.
  4. Compare to market prices, rank by edge, size with fractional Kelly.
  5. Place orders (DRY_RUN by default — logs instead of sending).

Run once:   python -m src.bot
Loop:       python -m src.bot --loop 600    # every 10 min
"""
from __future__ import annotations

import argparse
import os
import time

from .config import MIN_EDGE, DRY_RUN
from .polymarket.gamma import fetch_open_temperature_events, parse_event
from .polymarket.clob import place_order
from .strategy.edge import generate_signals
from .strategy import drift


def run_once() -> None:
    # Safety: this minimal loop has NO position dedup and NONE of the paper
    # engine's cash/per-day/per-city caps — it re-places every signal each scan.
    # It's a forecasting demo, not a live trader. Real money must go through
    # src.live.trader (LiveBroker = the paper engine + real fills).
    if not DRY_RUN and os.getenv("ALLOW_UNSAFE_BOT") != "1":
        raise SystemExit(
            "src.bot is unsafe for live money (no caps/dedup; re-buys each scan).\n"
            "Use:  python -m src.live.trader --loop 900\n"
            "(or set ALLOW_UNSAFE_BOT=1 to override, not recommended).")
    print(f"\n=== scan @ {time.strftime('%Y-%m-%d %H:%M:%S')} "
          f"(DRY_RUN={DRY_RUN}, MIN_EDGE={MIN_EDGE}) ===")
    events = fetch_open_temperature_events()
    markets = [m for ev in events for m in parse_event(ev)]
    print(f"discovered {len(events)} temperature events / {len(markets)} bucket markets")

    blocked = drift.current_blocked()      # bench drifting stations (capital protection)
    if blocked:
        print(f"drift guard benched: {', '.join(sorted(blocked))}")
    signals = generate_signals(markets, blocked_stations=blocked)
    print(f"\n{len(signals)} actionable signal(s):")
    for s in signals:
        print(" ", s)

    for s in signals:
        place_order(s.token_id, "BUY", s.price, s.stake)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between scans; 0 = run once")
    args = ap.parse_args()

    if args.loop <= 0:
        run_once()
        return
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            print(f"scan error: {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
