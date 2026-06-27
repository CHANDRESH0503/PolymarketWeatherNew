"""Backfill forecast metadata (station / fc_date / fc_mean / fc_std) onto paper
fills that were written before that data was recorded (or by a stale deploy).

Safe + idempotent — only fills NULL columns from data we still have (the city
registry + the logged `forecasts` table). Run it on the host after deploying:

    python scripts/backfill_fills.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.paper import store


def main() -> None:
    con = store.connect()
    before = con.execute(
        "SELECT COUNT(*) n FROM fills WHERE station IS NULL OR fc_mean IS NULL"
    ).fetchone()["n"]
    healed = store.backfill_fill_metadata(con)
    have_station = con.execute(
        "SELECT COUNT(*) n FROM fills WHERE station IS NOT NULL").fetchone()["n"]
    have_mean = con.execute(
        "SELECT COUNT(*) n FROM fills WHERE fc_mean IS NOT NULL").fetchone()["n"]
    total = con.execute("SELECT COUNT(*) n FROM fills").fetchone()["n"]
    print(f"fills total={total}  incomplete before={before}  rows touched={healed}")
    print(f"now: station set on {have_station}/{total}, fc_mean set on {have_mean}/{total}")
    miss = con.execute(
        "SELECT id, city, end_date FROM fills WHERE fc_mean IS NULL").fetchall()
    if miss:
        print(f"still missing fc_mean (no matching forecasts row): "
              f"{[m['id'] for m in miss]}")


if __name__ == "__main__":
    main()
