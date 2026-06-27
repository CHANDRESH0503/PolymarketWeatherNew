"""SQLite persistence for the paper-trading portfolio.

One file, stdlib only. Tables:
  meta      - key/value (starting cash, created_at)
  fills     - every paper position lot (open or settled)
  equity    - time series of portfolio value
  signals   - latest scan output (replaced each tick) for the dashboard
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

from ..config import ROOT, BANKROLL

# Default book DB. Overridable via PAPER_DB (e.g. point the dashboard at the live
# book with PAPER_DB=data/live.db). The live trader passes its path explicitly.
DB_PATH = Path(os.getenv("PAPER_DB") or ROOT / "data" / "paper.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS fills (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL,
    event_slug   TEXT,
    market_slug  TEXT,
    question     TEXT,
    city         TEXT,
    condition_id TEXT,
    token_id     TEXT,
    side         TEXT,          -- outcome bought: Yes / No
    entry_price  REAL,
    shares       REAL,
    cost         REAL,          -- usdc paid
    model_prob   REAL,          -- model P(Yes) at entry
    edge         REAL,
    end_date     TEXT,
    status       TEXT,          -- open / settled
    mark_price   REAL,          -- last mark (open) or resolved price (settled)
    exit_price   REAL,          -- resolved price of held token (settled)
    pnl          REAL,          -- realized pnl (settled) or live unrealized (open)
    resolved_yes INTEGER,       -- 1 if Yes outcome won, 0 if No, NULL if open
    station      TEXT,
    fc_date      TEXT,
    fc_mean      REAL,          -- calibrated ensemble mean max-temp at entry
    fc_std       REAL,
    quote_price  REAL,          -- top-of-book quote we sized against
    slippage     REAL,          -- realized avg fill price - quote (depth-aware fills)
    fill_ratio   REAL           -- filled cost / requested cost (1.0 = full fill)
);

-- Resolution-source audit: did round(METAR daily max) == the actual Polymarket
-- resolution? The whole edge rests on this matching; this table records it.
CREATE TABLE IF NOT EXISTS resolution_audit (
    station      TEXT,
    date         TEXT,
    resolved_deg INTEGER,       -- winning whole-degree per Polymarket
    metar_max    REAL,          -- our station daily max (IEM ASOS)
    metar_deg    INTEGER,       -- round(metar_max)
    delta        INTEGER,       -- metar_deg - resolved_deg
    matched      INTEGER,       -- 1 if metar_deg == resolved_deg
    ts           REAL,
    PRIMARY KEY (station, date)
);

-- One row per (station, date) we forecast, plus the realized max once known.
-- This is the dataset scripts/calibrate.py learns per-station bias/sigma from.
CREATE TABLE IF NOT EXISTS forecasts (
    station     TEXT,
    date        TEXT,
    ts_logged   REAL,
    mean        REAL,
    std         REAL,
    n_members   INTEGER,
    actual_max  REAL,           -- ERA5/Polymarket truth, filled in after the day
    actual_src  TEXT,
    PRIMARY KEY (station, date)
);

CREATE TABLE IF NOT EXISTS equity (
    ts          REAL PRIMARY KEY,
    cash        REAL,
    pos_value   REAL,
    realized    REAL,
    unrealized  REAL,
    equity      REAL
);

CREATE TABLE IF NOT EXISTS signals (
    ts          REAL,
    event_slug  TEXT,
    question    TEXT,
    city        TEXT,
    side        TEXT,
    price       REAL,
    model_prob  REAL,
    edge        REAL,
    stake       REAL,
    taken       INTEGER,
    sleeve      TEXT,          -- 'forecast' | 'no_harvest'
    peer        TEXT           -- smart-money stance: confirm / against / mixed / -
);

-- Coherence-arbitrage opportunities seen during a scan (Σ best-ask(YES) < 1),
-- replaced each tick. Surfaced on the dashboard; execution is ARB_EXECUTE-gated.
CREATE TABLE IF NOT EXISTS arb_ops (
    ts          REAL,
    event_slug  TEXT,
    kind        TEXT,
    n_buckets   INTEGER,
    price_sum   REAL,
    edge        REAL,          -- profit per $1 basket
    roi_pct     REAL,
    max_size    REAL,
    est_profit  REAL
);

-- Forecast distributions the daemon computed, so the dashboard can render the
-- Forecast/Nowcast panels WITHOUT calling Open-Meteo itself (the daemon is the
-- single source of upstream calls). `payload` is JSON: for kind='ensemble' the
-- raw member daily-maxes + mean/std; for kind='nowcast' the rounded-degree PMF
-- + observed floor / collapse metrics.
CREATE TABLE IF NOT EXISTS forecast_cache (
    station  TEXT,
    date     TEXT,
    kind     TEXT,          -- 'ensemble' | 'nowcast'
    ts       REAL,
    payload  TEXT,
    PRIMARY KEY (station, date, kind)
);

-- Profit-compounding ledger: one row per resolution day whose net realized P&L
-- was swept into the capital base. `swept` is the cumulative amount promoted to
-- starting_cash for that day (idempotent — re-ticks/late settlements only move
-- the increment). Auditable record of how capital grew beyond starting_cash.
CREATE TABLE IF NOT EXISTS profit_sweeps (
    day      TEXT PRIMARY KEY,   -- resolution day (YYYY-MM-DD, from end_date)
    day_pnl  REAL,               -- net realized P&L attributed to that day
    swept    REAL,               -- cumulative USDC moved into starting_cash
    ts       REAL
);
"""


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _migrate(con)
    _ensure_meta(con)
    return con


def _migrate(con: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created."""
    cols = {r["name"] for r in con.execute("PRAGMA table_info(fills)")}
    for col, decl in (("station", "TEXT"), ("fc_date", "TEXT"),
                      ("fc_mean", "REAL"), ("fc_std", "REAL"),
                      ("quote_price", "REAL"), ("slippage", "REAL"),
                      ("fill_ratio", "REAL"), ("sleeve", "TEXT"), ("peer", "TEXT")):
        if col not in cols:
            con.execute(f"ALTER TABLE fills ADD COLUMN {col} {decl}")
    sig_cols = {r["name"] for r in con.execute("PRAGMA table_info(signals)")}
    for col, decl in (("sleeve", "TEXT"), ("peer", "TEXT")):
        if col not in sig_cols:
            con.execute(f"ALTER TABLE signals ADD COLUMN {col} {decl}")
    con.commit()


def _ensure_meta(con: sqlite3.Connection) -> None:
    cur = con.execute("SELECT value FROM meta WHERE key='starting_cash'")
    if cur.fetchone() is None:
        con.execute("INSERT INTO meta VALUES ('starting_cash', ?)", (str(BANKROLL),))
        con.execute("INSERT INTO meta VALUES ('cash', ?)", (str(BANKROLL),))
        con.execute("INSERT INTO meta VALUES ('created_at', ?)", (str(time.time()),))
        con.commit()


def backfill_fill_metadata(con: sqlite3.Connection) -> int:
    """Self-heal fills written before forecast metadata was recorded (or by a
    stale deploy). Recoverable from data we still have:

      station  <- fills.city mapped through the station registry
      fc_date  <- end_date[:10]
      fc_mean/fc_std <- joined from the `forecasts` table on (station, date)

    Idempotent: only touches rows where the target column is NULL. Returns the
    number of fills updated."""
    from ..config import STATIONS
    city_to_code = {s["city"]: code for code, s in STATIONS.items()}

    updated = 0
    rows = con.execute(
        "SELECT id, city, end_date, station, fc_date FROM fills "
        "WHERE station IS NULL OR fc_date IS NULL").fetchall()
    for r in rows:
        station = r["station"] or city_to_code.get((r["city"] or "").strip())
        fc_date = r["fc_date"] or ((r["end_date"] or "")[:10] or None)
        if station == r["station"] and fc_date == r["fc_date"]:
            continue
        con.execute("UPDATE fills SET station=?, fc_date=? WHERE id=?",
                    (station, fc_date, r["id"]))
        updated += 1

    # Fill forecast mean/std from the logged forecasts dataset where available.
    con.execute(
        """UPDATE fills SET
             fc_mean = COALESCE(fc_mean, (SELECT mean FROM forecasts f
                        WHERE f.station=fills.station AND f.date=fills.fc_date)),
             fc_std  = COALESCE(fc_std,  (SELECT std  FROM forecasts f
                        WHERE f.station=fills.station AND f.date=fills.fc_date))
           WHERE (fc_mean IS NULL OR fc_std IS NULL)
             AND station IS NOT NULL AND fc_date IS NOT NULL""")
    con.commit()
    return updated


def save_audit(con: sqlite3.Connection, rows: list[dict]) -> int:
    """Upsert resolution-audit rows (one per resolved station/date)."""
    for r in rows:
        con.execute(
            """INSERT INTO resolution_audit
               (station,date,resolved_deg,metar_max,metar_deg,delta,matched,ts)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(station,date) DO UPDATE SET
                 resolved_deg=excluded.resolved_deg, metar_max=excluded.metar_max,
                 metar_deg=excluded.metar_deg, delta=excluded.delta,
                 matched=excluded.matched, ts=excluded.ts""",
            (r["station"], r["date"], r["resolved_deg"], r["metar_max"],
             r["metar_deg"], r["delta"], r["matched"], time.time()))
    con.commit()
    return len(rows)


def station_error_metrics(con: sqlite3.Connection,
                          lookback_days: int) -> dict[str, tuple[float, int]]:
    """{station: (rolling MAE °C, n resolved days)} over the last `lookback_days`,
    from logged forecasts vs their backfilled actual max. Feeds the drift guard."""
    import datetime as _dt
    since = (_dt.date.today() - _dt.timedelta(days=lookback_days)).isoformat()
    rows = con.execute(
        """SELECT station, AVG(ABS(actual_max - mean)) AS mae, COUNT(*) AS n
             FROM forecasts
            WHERE actual_max IS NOT NULL AND date >= ?
            GROUP BY station""", (since,)).fetchall()
    return {r["station"]: (float(r["mae"]), int(r["n"])) for r in rows}


def get_meta(con: sqlite3.Connection, key: str, default: float = 0.0) -> float:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return float(row["value"]) if row else default


def set_meta(con: sqlite3.Connection, key: str, value: float) -> None:
    con.execute("INSERT INTO meta VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=?",
                (key, str(value), str(value)))


def record_profit_sweep(con: sqlite3.Connection, day: str, day_pnl: float,
                        swept: float) -> None:
    """Upsert the cumulative amount swept into capital for a resolution day.
    Does not commit (the caller commits atomically with the starting_cash bump)."""
    con.execute(
        """INSERT INTO profit_sweeps (day, day_pnl, swept, ts) VALUES (?,?,?,?)
           ON CONFLICT(day) DO UPDATE SET day_pnl=excluded.day_pnl,
             swept=excluded.swept, ts=excluded.ts""",
        (day, day_pnl, swept, time.time()))


def save_forecast_dist(con: sqlite3.Connection, station: str, date: str,
                       kind: str, payload: dict, ts: float | None = None) -> None:
    """Persist a forecast distribution (JSON) for the dashboard to read."""
    con.execute(
        """INSERT INTO forecast_cache (station, date, kind, ts, payload)
           VALUES (?,?,?,?,?)
           ON CONFLICT(station, date, kind) DO UPDATE SET
             ts=excluded.ts, payload=excluded.payload""",
        (station, date, kind, ts if ts is not None else time.time(),
         json.dumps(payload)))
    con.commit()


def record_arb_ops(con: sqlite3.Connection, ops: list) -> None:
    """Replace the arb-opportunity snapshot with this scan's results."""
    con.execute("DELETE FROM arb_ops")
    now = time.time()
    for o in ops:
        con.execute(
            "INSERT INTO arb_ops VALUES (?,?,?,?,?,?,?,?,?)",
            (now, o.event_slug, o.kind, o.n_buckets, round(o.price_sum, 4),
             round(o.profit_per_basket, 4), round(o.roi_pct, 2),
             round(o.max_size, 2), round(o.est_profit, 2)))
    con.commit()


def load_forecast_dist(con: sqlite3.Connection, station: str, date: str,
                       kind: str) -> dict | None:
    """Read back a persisted distribution; returns the payload with its `ts`,
    or None if the daemon hasn't cached this (station, date, kind) yet."""
    row = con.execute(
        "SELECT ts, payload FROM forecast_cache WHERE station=? AND date=? AND kind=?",
        (station, date, kind)).fetchone()
    if not row:
        return None
    return {"ts": row["ts"], **json.loads(row["payload"])}
