"""Dashboard API + static server.

    python -m src.server            # http://127.0.0.1:8000

Read-only over the paper-trading SQLite, plus one live endpoint (/api/forecast)
that recomputes a city's ensemble distribution vs market prices on demand.
"""
from __future__ import annotations

import datetime as dt
import time
from collections import defaultdict
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from .config import (ROOT, STATIONS, BANKROLL, CALIBRATION, DRY_RUN, NOWCAST,
                     CORR_KELLY, CORR_KELLY_RHO, MIN_EDGE, KELLY_FRACTION,
                     MAX_STAKE_PER_MARKET, MIN_PRICE, MAX_PRICE,
                     MIN_HOURS_TO_RESOLVE, ARB_EXECUTE, LP_EXECUTE,
                     CASH_BUFFER, PAPER_DEPTH, MAX_DAY_FRACTION, MAX_CITY_FRACTION,
                     PEER_SIGNAL, PEER_WALLETS, NO_HARVEST, NO_HARVEST_MAX_P,
                     NO_HARVEST_STAKE, ARB_SCAN)
from .analysis.resolution_audit import summarize as summarize_audit
from .paper import store
from .polymarket.gamma import fetch_open_temperature_events, parse_event
from .forecast.openmeteo import MODELS
from .forecast.model import yes_probability
from .forecast import dist_cache

WEB = ROOT / "web"
app = Flask(__name__, static_folder=None)

def db():
    return store.connect()


@app.get("/api/summary")
def summary():
    con = db()
    start = store.get_meta(con, "starting_cash", BANKROLL)
    cash = store.get_meta(con, "cash", BANKROLL)
    eq = con.execute("SELECT * FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    # Realized/unrealized are computed straight from fills (not the equity
    # snapshot, which is throttled and so lags settlement): realized = Σ pnl of
    # settled lots, unrealized = Σ live pnl of open lots. This is the same
    # source of truth as wins/Brier below, so the plates can't disagree. Equity
    # is then start + realized + unrealized, so all four plates reconcile exactly.
    realized = con.execute(
        "SELECT COALESCE(SUM(pnl),0) s FROM fills WHERE status='settled'").fetchone()["s"]
    unrealized = con.execute(
        "SELECT COALESCE(SUM(pnl),0) s FROM fills WHERE status='open'").fetchone()["s"]
    equity = start + realized + unrealized
    settled = con.execute(
        "SELECT pnl, side, resolved_yes FROM fills WHERE status='settled'").fetchall()
    wins = sum(1 for s in settled if s["pnl"] > 0)
    n = len(settled)
    # Brier score: model P(Yes) at entry vs whether Yes actually resolved.
    brier = None
    rows = con.execute(
        "SELECT model_prob, resolved_yes FROM fills WHERE status='settled' "
        "AND resolved_yes IS NOT NULL").fetchall()
    if rows:
        brier = sum((r["model_prob"] - r["resolved_yes"]) ** 2 for r in rows) / len(rows)
    open_n = con.execute("SELECT COUNT(*) c FROM fills WHERE status='open'").fetchone()["c"]
    created = store.get_meta(con, "created_at", time.time())
    return jsonify({
        "starting_cash": start, "cash": cash, "equity": equity,
        "total_pnl": equity - start, "roi": (equity - start) / start if start else 0,
        "realized": realized, "unrealized": unrealized,
        "open_positions": open_n, "settled": n,
        "wins": wins, "win_rate": wins / n if n else None,
        "brier": brier, "last_update": eq["ts"] if eq else None,
        "running_since": created,
        "is_live": bool(eq) and (time.time() - eq["ts"] < 3600),
    })


@app.get("/api/equity")
def equity_series():
    con = db()
    rows = con.execute("SELECT ts, equity, realized, unrealized FROM equity "
                       "ORDER BY ts ASC").fetchall()
    out = [dict(r) for r in rows]
    # Append a live "now" point computed from fills (same source as /api/summary)
    # so the chart head always agrees with the summary plates. Persisted snapshots
    # are throttled, so without this the curve can lag a settlement and show a
    # stale realized/unrealized split. Replace a near-coincident persisted point
    # to avoid a tiny double-point at the head.
    start = store.get_meta(con, "starting_cash", BANKROLL)
    realized = con.execute(
        "SELECT COALESCE(SUM(pnl),0) s FROM fills WHERE status='settled'").fetchone()["s"]
    unrealized = con.execute(
        "SELECT COALESCE(SUM(pnl),0) s FROM fills WHERE status='open'").fetchone()["s"]
    live = {"ts": time.time(), "equity": start + realized + unrealized,
            "realized": realized, "unrealized": unrealized}
    if out and live["ts"] - out[-1]["ts"] <= 5:
        out[-1] = live
    else:
        out.append(live)
    return jsonify(out)


@app.get("/api/positions")
def positions():
    con = db()
    rows = con.execute(
        "SELECT * FROM fills WHERE status='open' ORDER BY ts DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/fills")
def fills():
    con = db()
    rows = con.execute(
        "SELECT * FROM fills ORDER BY ts DESC LIMIT 200").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/signals")
def signals():
    con = db()
    rows = con.execute(
        "SELECT * FROM signals ORDER BY edge DESC").fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/api/arb")
def arb():
    """Coherence-arbitrage opportunities from the last scan (Σ best-ask(YES) < 1)."""
    con = db()
    rows = con.execute(
        "SELECT * FROM arb_ops ORDER BY est_profit DESC").fetchall()
    last = con.execute("SELECT MAX(ts) t FROM arb_ops").fetchone()
    return jsonify({"updated": last["t"] if last else None,
                    "ops": [dict(r) for r in rows]})


@app.get("/api/daily")
def daily():
    """Realized PnL grouped by settlement day + end-of-day equity."""
    con = db()
    rows = con.execute(
        "SELECT ts, end_date, pnl FROM fills WHERE status='settled'").fetchall()
    def day_of(ts):
        return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
    by_day = defaultdict(float)
    for r in rows:
        # Attribute realized P&L to the day the market *resolved* (end_date),
        # not the day we entered the trade. Daily temp markets resolve next-day,
        # so grouping by entry ts smears a day's settlements onto the prior day.
        # Fall back to the entry day only for legacy rows missing end_date.
        day = (r["end_date"] or "")[:10] or day_of(r["ts"])
        by_day[day] += r["pnl"]
    eq = con.execute("SELECT ts, equity FROM equity ORDER BY ts ASC").fetchall()
    eq_by_day = {}
    for r in eq:
        eq_by_day[day_of(r["ts"])] = r["equity"]
    return jsonify({"realized": [{"day": d, "pnl": p} for d, p in sorted(by_day.items())],
                    "equity": [{"day": d, "equity": e} for d, e in sorted(eq_by_day.items())]})


@app.get("/api/forecast")
def forecast():
    """Ensemble distribution vs market-implied bucket probabilities for one event.

    Reads the distribution the daemon already fetched + persisted — the dashboard
    makes no Open-Meteo calls of its own (only fresh market prices via Gamma)."""
    slug = request.args.get("event", "")
    events = fetch_open_temperature_events()
    ev = next((e for e in events if e.get("slug") == slug), None)
    if ev is None:
        return jsonify({"error": "event not found", "available":
                        [e["slug"] for e in events[:60]]}), 404
    markets = parse_event(ev)
    station = next((m.station_code for m in markets if m.station_code in STATIONS), None)
    if not station:
        return jsonify({"error": "unknown station"}), 404
    date = markets[0].end_date[:10]
    payload = store.load_forecast_dist(db(), station, date, "ensemble")
    if payload is None:
        return jsonify({"error": "forecast warming up — the daemon hasn't cached "
                        "this city yet"}), 503
    fc = dist_cache.ensemble_from_payload(station, date, payload)
    buckets = []
    for m in sorted(markets, key=lambda x: x.threshold_c):
        p = yes_probability(fc, m.bucket_kind, m.threshold_c)
        buckets.append({"label": f"{m.threshold_c}°", "kind": m.bucket_kind,
                        "degree": m.threshold_c, "model": p, "market": m.yes_price})
    s = STATIONS[station]
    return jsonify({"event": slug, "city": s["city"], "station": station,
                    "date": date, "mean": payload["mean"], "std": payload["std"],
                    "members": payload["members"], "buckets": buckets,
                    "updated": payload["ts"]})


@app.get("/api/status")
def status():
    """What the system is configured to do right now — feature flags + knobs.
    Drives the dashboard's 'System Almanac' strip so it reflects the live build."""
    con = db()
    n_fc = con.execute("SELECT COUNT(*) c FROM forecasts").fetchone()["c"]
    n_emos = sum(1 for v in CALIBRATION.values() if v.get("emos"))
    return jsonify({
        "dry_run": DRY_RUN,
        "strategies": {
            "forecast_edge": True,
            "nowcast": NOWCAST,
            "corr_kelly": CORR_KELLY,
            "depth_fills": PAPER_DEPTH,
            "arb_execute": ARB_EXECUTE,
            "arb_scan": ARB_SCAN,
            "lp_execute": LP_EXECUTE,
            "peer_signal": PEER_SIGNAL,
            "no_harvest": NO_HARVEST,
        },
        "peer_wallets": PEER_WALLETS,
        "no_harvest_max_p": NO_HARVEST_MAX_P,
        "no_harvest_stake": NO_HARVEST_STAKE,
        "cash_buffer": CASH_BUFFER,
        "max_day_fraction": MAX_DAY_FRACTION,
        "knobs": {
            "min_edge": MIN_EDGE, "kelly_fraction": KELLY_FRACTION,
            "max_stake": MAX_STAKE_PER_MARKET, "bankroll": BANKROLL,
            "min_price": MIN_PRICE, "max_price": MAX_PRICE,
            "min_hours_to_resolve": MIN_HOURS_TO_RESOLVE,
            "corr_rho": CORR_KELLY_RHO,
        },
        "models": MODELS.split(","),
        "stations": len(STATIONS),
        "calibrated": len(CALIBRATION),
        "emos_fitted": n_emos,
        "forecasts_logged": n_fc,
    })


@app.get("/api/calibration")
def calibration():
    """Tier-2 EMOS/NGR + bias calibration per station (μ=a+b·mean, σ²=c+d·var)."""
    out = []
    for code, cal in sorted(CALIBRATION.items()):
        s = STATIONS.get(code, {})
        e = cal.get("emos") or {}
        out.append({
            "station": code, "city": s.get("city", code),
            "bias": cal.get("bias"), "sigma": cal.get("sigma"),
            "emos": bool(e),
            "a": e.get("a"), "b": e.get("b"), "c": e.get("c"), "d": e.get("d"),
        })
    return jsonify(out)


@app.get("/api/exposure")
def exposure():
    """Live breakdown of open capital: by city, by side (Yes/No longshot mix),
    and the edge/horizon profile — i.e. what the book currently looks like."""
    con = db()
    rows = con.execute(
        "SELECT city, side, cost, shares, mark_price, edge, model_prob, pnl, "
        "end_date, sleeve FROM fills WHERE status='open'").fetchall()
    by_city: dict[str, float] = defaultdict(float)
    by_day: dict[str, float] = defaultdict(float)
    by_side = {"Yes": {"n": 0, "cost": 0.0}, "No": {"n": 0, "cost": 0.0}}
    by_sleeve: dict[str, dict] = defaultdict(lambda: {"n": 0, "cost": 0.0, "pnl": 0.0})
    deployed = value = edge_sum = 0.0
    for r in rows:
        by_city[r["city"] or "—"] += r["cost"]
        by_day[(r["end_date"] or "")[:10] or "—"] += r["cost"]
        side = r["side"] if r["side"] in by_side else "No"
        by_side[side]["n"] += 1
        by_side[side]["cost"] += r["cost"]
        sl = by_sleeve[r["sleeve"] or "forecast"]
        sl["n"] += 1; sl["cost"] += r["cost"]; sl["pnl"] += r["pnl"] or 0.0
        deployed += r["cost"]
        value += r["shares"] * r["mark_price"]
        edge_sum += r["edge"] or 0.0
    n = len(rows)
    # Realized P&L per sleeve (settled), so the low-variance no-harvest grind can
    # be judged on its own track record vs the forecast lane.
    realized_sleeve = {r["sleeve"] or "forecast": {"n": r["n"], "pnl": r["pnl"]}
                       for r in con.execute(
                           "SELECT COALESCE(sleeve,'forecast') sleeve, COUNT(*) n, "
                           "COALESCE(SUM(pnl),0) pnl FROM fills WHERE status='settled' "
                           "GROUP BY COALESCE(sleeve,'forecast')").fetchall()}
    # slippage / fill-quality (depth-aware fills); legacy rows may be NULL
    sl = con.execute("SELECT slippage, fill_ratio FROM fills "
                     "WHERE slippage IS NOT NULL").fetchall()
    avg_slip = sum(r["slippage"] for r in sl) / len(sl) if sl else None
    avg_fill = sum(r["fill_ratio"] for r in sl) / len(sl) if sl else None
    start = store.get_meta(con, "starting_cash", BANKROLL)
    # Realized profit not yet promoted into the bankroll — the headroom the
    # manual "Add to Bankroll" control can move (total settled P&L minus what
    # auto-sweeps + manual injections already moved).
    realized_total = con.execute(
        "SELECT COALESCE(SUM(pnl),0) s FROM fills WHERE status='settled'").fetchone()["s"]
    promoted = con.execute(
        "SELECT COALESCE(SUM(swept),0) s FROM profit_sweeps").fetchone()["s"]
    available_to_add = round(max(0.0, realized_total - promoted), 2)
    return jsonify({
        "n": n, "deployed": round(deployed, 2), "value": round(value, 2),
        "available_to_add": available_to_add,
        "avg_edge": (edge_sum / n) if n else None,
        "by_city": [{"city": c, "cost": round(v, 2)}
                    for c, v in sorted(by_city.items(), key=lambda x: -x[1])],
        "by_side": by_side,
        "by_sleeve": {k: {"n": v["n"], "cost": round(v["cost"], 2),
                          "pnl": round(v["pnl"], 2)} for k, v in by_sleeve.items()},
        "realized_by_sleeve": {k: {"n": v["n"], "pnl": round(v["pnl"], 2)}
                               for k, v in realized_sleeve.items()},
        "by_day": [{"day": d, "cost": round(v, 2)}
                   for d, v in sorted(by_day.items())],
        "avg_slippage": avg_slip, "avg_fill_ratio": avg_fill,
        "bankroll": start, "cash_buffer": CASH_BUFFER,
        "investable": round(start * (1.0 - CASH_BUFFER), 2),
        "day_cap": round(start * MAX_DAY_FRACTION, 2),
        "max_day_fraction": MAX_DAY_FRACTION,
        "city_cap": round(start * MAX_CITY_FRACTION, 2),
        "max_city_fraction": MAX_CITY_FRACTION,
        "depth_fills": PAPER_DEPTH,
    })


@app.get("/api/resolution_audit")
def resolution_audit():
    """Tier-0 validation: does round(METAR daily max) match the actual Polymarket
    resolution? Reads the table populated by scripts/resolution_audit.py."""
    con = db()
    rows = [dict(r) for r in con.execute(
        "SELECT station, date, resolved_deg, metar_max, metar_deg, delta, matched, ts "
        "FROM resolution_audit").fetchall()]
    out = summarize_audit(rows)
    last = max((r["ts"] for r in rows if r.get("ts")), default=None)
    out["updated"] = last
    out["recent"] = sorted(rows, key=lambda r: r["date"], reverse=True)[:8]
    return jsonify(out)


@app.get("/api/nowcast")
def nowcast():
    """Tier-3 intraday nowcast for one event: observed station floor + remaining
    hours, with the collapse meter and ENS-vs-NOW-vs-MKT bucket probabilities.

    Served from the daemon's persisted distributions — no Open-Meteo calls here."""
    slug = request.args.get("event", "")
    events_ = fetch_open_temperature_events()
    ev = next((e for e in events_ if e.get("slug") == slug), None)
    if ev is None:
        return jsonify({"error": "event not found"}), 404
    markets = parse_event(ev)
    station = next((m.station_code for m in markets if m.station_code in STATIONS), None)
    if not station:
        return jsonify({"error": "unknown station"}), 404
    date = markets[0].end_date[:10]
    con = db()
    nc = store.load_forecast_dist(con, station, date, "nowcast")
    if nc is None:
        return jsonify({"error": "nowcast warming up — only same-day markets have "
                        "an intraday nowcast, and the daemon hasn't cached it yet"}), 503
    ens = store.load_forecast_dist(con, station, date, "ensemble")
    fc = (dist_cache.ensemble_from_payload(station, date, ens, CALIBRATION)
          if ens is not None else None)

    buckets = []
    for m in sorted(markets, key=lambda x: x.threshold_c):
        buckets.append({
            "label": f"{m.threshold_c}°", "kind": m.bucket_kind, "degree": m.threshold_c,
            "now": dist_cache.nowcast_prob(nc, m.bucket_kind, m.threshold_c),
            "ens": (yes_probability(fc, m.bucket_kind, m.threshold_c)
                    if fc is not None else None),
            "market": m.yes_price,
        })
    s = STATIONS[station]
    return jsonify({
        "event": slug, "city": s["city"], "station": station, "date": date,
        "observed_max": nc["observed_max"], "latest_ob": nc["latest_ob"],
        "remaining_hours": nc["remaining_hours"], "floor_locked": nc["floor_locked"],
        "mean": nc["mean"], "std": nc["std"], "buckets": buckets,
        "updated": nc["ts"],
    })


@app.get("/api/events")
def events():
    evs = fetch_open_temperature_events()
    out = []
    for e in evs:
        ms = parse_event(e)
        if any(m.station_code in STATIONS for m in ms):
            out.append({"slug": e["slug"], "title": e.get("title", "")})
    return jsonify(out)


@app.post("/api/bankroll/add")
def bankroll_add():
    """Manually promote a user-chosen amount of realized P&L into the bankroll
    (capital base), raising the reserve floor + per-day/per-city deploy caps."""
    from .paper.engine import PaperBroker
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400
    broker = PaperBroker()
    try:
        result = broker.add_to_bankroll(amount)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, **result})


@app.get("/")
def index():
    return send_from_directory(WEB, "index.html")


@app.get("/<path:p>")
def static_files(p):
    return send_from_directory(WEB, p)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8010, debug=False)
