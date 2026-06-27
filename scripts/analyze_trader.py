"""Profile any Polymarket trader's strategy. Used to reverse-engineer @lactesting.

    python scripts/analyze_trader.py lactesting
    python scripts/analyze_trader.py 0x36f662fcbdc8f64aa1bbaa1f8897ca0e3bb7ae14
"""
from __future__ import annotations

import collections
import datetime as dt
import re
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.polymarket.data_api import get_positions, get_activity, resolve_username


def main(who: str) -> None:
    wallet = who if who.startswith("0x") else resolve_username(who)
    if not wallet:
        print(f"could not resolve {who}")
        return
    print(f"Trader: {who}  ->  {wallet}\n")

    pos = get_positions(wallet)
    cur = sum(p.get("currentValue", 0) for p in pos)
    pnl = sum(p.get("cashPnl", 0) for p in pos)
    print(f"Open positions: {len(pos)}  | value ${cur:,.2f}  | unrealized PnL ${pnl:,.2f}")

    acts = get_activity(wallet)
    trades = [a for a in acts if a.get("type") == "TRADE"]
    if not trades:
        print("no trades")
        return

    ts = [a["timestamp"] for a in trades]
    span = (max(ts) - min(ts)) / 86400 or 1
    sizes = [a.get("usdcSize", 0) for a in trades]
    prices = [a.get("price", 0) for a in trades]
    side = collections.Counter(a.get("side") for a in trades)
    outcome = collections.Counter(a.get("outcome") for a in trades)
    cities = collections.Counter()
    for a in trades:
        m = re.search(r"in (\w+)", a.get("title", ""))
        if m:
            cities[m.group(1)] += 1

    print(f"\nTrades: {len(trades)} over {span:.1f} days "
          f"({len(trades)/span:.0f}/day)")
    print(f"Volume: ${sum(sizes):,.0f}  | median ${st.median(sizes):.0f}  "
          f"| max ${max(sizes):,.0f}")
    print(f"Entry price: median {st.median(prices):.2f}  "
          f"[{min(prices):.2f}-{max(prices):.2f}]")
    print(f"Side: {dict(side)}   Outcome bought: {dict(outcome)}")
    print(f"Top cities: {dict(cities.most_common(8))}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "lactesting")
