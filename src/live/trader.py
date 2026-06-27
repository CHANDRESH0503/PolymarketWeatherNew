"""Live trading daemon — the paper engine, wired to real CLOB execution.

It runs the SAME `tick()` as the paper trader (discover -> signal -> fill -> mark
-> settle -> snapshot); the only difference is the broker is a LiveBroker, so the
fill step sends real orders. This is deliberate: identical decision logic to the
validated paper run, one swapped method.

    python -m src.live.trader              # one tick
    python -m src.live.trader --loop 900   # every 15 min (matches the paper run)

DRY_RUN is the master switch:
  * DRY_RUN=1 -> SHADOW mode: simulates fills into data/live.db, sends nothing.
                Use this to dry-run the live path safely before funding.
  * DRY_RUN=0 -> sends REAL orders. Refuses to start without PK + CLOB creds.
"""
from __future__ import annotations

import argparse
import sys
import time

from ..config import DRY_RUN, PK, CLOB_API_KEY, BANKROLL
from .. import notify
from ..paper.trader import tick
from .engine import LiveBroker


def _preflight() -> None:
    """Loud, explicit confirmation of the mode we're about to run in."""
    if DRY_RUN:
        print("=" * 64)
        print(" LIVE TRADER — SHADOW MODE (DRY_RUN=1)")
        print(" Simulating fills into data/live.db. NO real orders are sent.")
        print(" Set DRY_RUN=0 (with a funded wallet + creds) to trade for real.")
        print("=" * 64)
        return
    missing = [name for name, val in (("PK", PK), ("CLOB_API_KEY", CLOB_API_KEY))
               if not val]
    if missing:
        print(f"REFUSING to run live: missing {', '.join(missing)} in .env.")
        print("Fund the wallet, set PK, derive CLOB creds "
              "(python -m src.polymarket.clob --create-api-key), then retry.")
        sys.exit(1)
    print("!" * 64)
    print(" LIVE TRADING ENABLED (DRY_RUN=0) — REAL MONEY ORDERS WILL BE SENT.")
    print(f" Bankroll assumed = ${BANKROLL:.0f}. Ensure the wallet holds >= that")
    print(" in USDC. Same caps/sizing as the paper run. Ctrl-C to abort now.")
    print("!" * 64)
    time.sleep(5)      # a breath to abort before the first real order


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between ticks; 0 = run once")
    args = ap.parse_args()

    _preflight()
    broker = LiveBroker()

    if args.loop <= 0:
        tick(broker)
        return

    consecutive_errors = 0
    while True:
        try:
            tick(broker)
            if consecutive_errors:
                notify.notify_tick_recovered(consecutive_errors)
            consecutive_errors = 0
        except Exception as e:  # noqa: BLE001
            consecutive_errors += 1
            print(f"tick error #{consecutive_errors}: {e}")
            notify.notify_tick_error(e, consecutive_errors)
            broker.snapshot()
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
