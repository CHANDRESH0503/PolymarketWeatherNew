"""THE edge test: would our model have beaten the *market price* on resolved
weather markets?

Unlike scripts/calibrate.py (which scores the forecast vs actual temperatures),
this replays our model against the prices that actually existed and the real
resolutions, then compares OUR Brier score to the MARKET's Brier. If ours isn't
lower, we have no edge — no matter how good the forecast looks in isolation.

    python scripts/market_backtest.py
    python scripts/market_backtest.py --pages 12 --edge 0.07 --decision-hour 18

Pipeline per resolved bucket market:
  1. Gamma (closed events)      -> bucket, station, clobTokenIds, resolved outcome
  2. decision time T            =  ~18:00 UTC the day before resolution
  3. CLOB prices-history        -> market price at T (what we'd have paid)
  4. historical-forecast model  -> our P(Yes) from the forecast available by ~T
  5. edge = model - price; trade if >= threshold; P&L per $1 = outcome - price

Caveats (read these):
  * Lead-time: we use the historical-forecast for the target date, which is
    ~the final short-range forecast — marginally better than a true 1-day-ahead.
  * prices-history points on thin buckets can be stale; we gate trades to a
    tradable price band, but illiquid fills are still optimistic.
  * Coverage grows as more markets resolve; page back with --pages for a bigger
    sample.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import STATIONS, GAMMA_API, CLOB_API, CALIBRATION
from src.forecast.openmeteo import MaxTempForecast
from src.forecast.model import yes_probability, apply_calibration
from src.polymarket.gamma import parse_event, _as_list
from scripts.calibrate import model_daily_maxes


# ----------------------------- data fetch ------------------------------------
def fetch_closed_events(pages: int, limit: int = 0) -> list[dict]:
    out = []
    for off in range(0, pages * 100, 100):
        r = requests.get(f"{GAMMA_API}/events", params={
            "closed": "true", "tag_id": 84, "limit": 100, "offset": off,
            "order": "endDate", "ascending": "false"}, timeout=30)
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out += [e for e in batch if "highest-temperature-in" in e.get("slug", "")]
        if limit and len(out) >= limit:
            return out[:limit]
    return out[:limit] if limit else out


def price_at(token_id: str, ts: int) -> float | None:
    """Last traded/quoted price at or before unix `ts` for a CLOB token."""
    try:
        r = requests.get(f"{CLOB_API}/prices-history", params={
            "market": token_id, "startTs": ts - 3 * 86400, "endTs": ts,
            "fidelity": 60}, timeout=20)
        r.raise_for_status()
        hist = r.json().get("history", [])
    except Exception:  # noqa: BLE001
        return None
    pts = [p for p in hist if p["t"] <= ts]
    return float(pts[-1]["p"]) if pts else None


def forecast_for(code: str, date: str, cache: dict) -> MaxTempForecast | None:
    key = (code, date)
    if key in cache:
        return cache[key]
    s = STATIONS[code]
    try:
        members = model_daily_maxes(s, date, date).get(date)
    except Exception:  # noqa: BLE001
        members = None
    if members is None or len(members) < 2:
        cache[key] = None
        return None
    fc = apply_calibration(MaxTempForecast(code, date, members), CALIBRATION)
    cache[key] = fc
    return fc


# ----------------------------- replay ----------------------------------------
def resolved_outcomes(ev: dict) -> dict[str, int]:
    """condition_id -> 1 if the Yes outcome resolved true, else 0."""
    out = {}
    for m in ev.get("markets", []):
        op = _as_list(m.get("outcomePrices"))
        outs = _as_list(m.get("outcomes"))
        if not op:
            continue
        yi = outs.index("Yes") if "Yes" in outs else 0
        out[m.get("conditionId")] = 1 if float(op[yi]) > 0.5 else 0
    return out


def decision_ts(date: str, hour: int) -> int:
    d = dt.date.fromisoformat(date) - dt.timedelta(days=1)
    return int(dt.datetime(d.year, d.month, d.day, hour, tzinfo=dt.timezone.utc).timestamp())


def run(pages: int, edge_thr: float, hour: int,
        min_price: float, max_price: float, limit: int, workers: int = 16,
        eval_after: str = "") -> None:
    events = fetch_closed_events(pages, limit)
    print(f"Closed temperature events fetched: {len(events)}", flush=True)

    # 1) collect candidate bucket-markets (cheap, no network)
    cand = []   # (tm, date, outcome)
    for ev in events:
        resolved = resolved_outcomes(ev)
        for tm in parse_event(ev):
            d = tm.end_date[:10]
            if eval_after and d < eval_after:        # out-of-sample window only
                continue
            if tm.station_code in STATIONS and tm.condition_id in resolved:
                cand.append((tm, d, resolved[tm.condition_id]))
    note = f" (eval markets on/after {eval_after})" if eval_after else ""
    print(f"Candidate bucket-markets (our cities): {len(cand)}{note}", flush=True)

    # 2) forecasts: one per unique (station, date), fetched concurrently
    keys = sorted({(tm.station_code, d) for tm, d, _ in cand})
    fc_cache: dict = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for (k, fc) in ex.map(lambda kv: (kv, forecast_for(kv[0], kv[1], {})), keys):
            fc_cache[k] = fc
    print(f"Forecasts fetched: {sum(1 for v in fc_cache.values() if v)}/{len(keys)}",
          flush=True)

    # 3) prices: one per token at decision time, fetched concurrently
    toks = [(tm.yes_token_id, decision_ts(d, hour)) for tm, d, _ in cand]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        prices = list(ex.map(lambda t: price_at(t[0], t[1]), toks))
    print(f"Prices fetched: {sum(1 for p in prices if p is not None)}/{len(toks)}",
          flush=True)

    # 4) assemble scores
    model_p, market_p, outcomes, trades = [], [], [], []
    skipped_price = skipped_fc = 0
    for (tm, d, outcome), p_yes_mkt in zip(cand, prices):
        fc = fc_cache.get((tm.station_code, d))
        if fc is None:
            skipped_fc += 1; continue
        if p_yes_mkt is None:
            skipped_price += 1; continue
        p_yes_model = yes_probability(fc, tm.bucket_kind, tm.threshold_c)
        model_p.append(p_yes_model); market_p.append(p_yes_mkt); outcomes.append(outcome)
        yes_edge = p_yes_model - p_yes_mkt
        no_edge = p_yes_mkt - p_yes_model
        if yes_edge >= no_edge and yes_edge >= edge_thr and min_price <= p_yes_mkt <= max_price:
            trades.append(("Yes", p_yes_mkt, outcome, outcome - p_yes_mkt, yes_edge))
        elif no_edge > yes_edge and no_edge >= edge_thr and min_price <= (1 - p_yes_mkt) <= max_price:
            trades.append(("No", 1 - p_yes_mkt, outcome, p_yes_mkt - outcome, no_edge))

    report(model_p, market_p, outcomes, trades, skipped_price, skipped_fc)


def report(model_p, market_p, outcomes, trades, skipped_price, skipped_fc) -> None:
    n = len(outcomes)
    print(f"Evaluated bucket-markets: {n}  "
          f"(skipped: {skipped_price} no-price, {skipped_fc} no-forecast)\n")
    if not n:
        print("No samples — page back further with --pages, or wait for more "
              "markets to resolve.")
        return

    mp = np.array(model_p); kp = np.array(market_p); o = np.array(outcomes)
    model_brier = float(np.mean((mp - o) ** 2))
    market_brier = float(np.mean((kp - o) ** 2))
    print("── Calibration vs the market (lower Brier = sharper) ──")
    print(f"  OUR model Brier : {model_brier:.4f}")
    print(f"  MARKET   Brier : {market_brier:.4f}")
    diff = market_brier - model_brier
    verdict = ("model SHARPER than market ✓ (edge plausible)" if diff > 0.003
               else "model WORSE than/equal to market ✗ (no demonstrated edge)"
               if diff < -0.003 else "≈ tie (no clear edge)")
    print(f"  → {verdict}  (Δ {diff:+.4f})\n")

    print("── Hypothetical trading (edge-thresholded, $1 notional/trade) ──")
    if not trades:
        print("  No trades cleared the edge threshold / price band.")
        return
    pnl = np.array([t[3] for t in trades])
    staked = np.array([t[1] for t in trades])
    wins = int(np.sum(pnl > 0))
    print(f"  trades         : {len(trades)}")
    print(f"  hit rate       : {wins}/{len(trades)} = {wins/len(trades):.0%}")
    print(f"  total P&L      : {pnl.sum():+.2f}  on  {staked.sum():.2f} staked")
    print(f"  ROI            : {pnl.sum()/staked.sum()*100:+.1f}%")
    print(f"  avg edge taken : {np.mean([t[4] for t in trades])*100:.1f}%")
    yes_n = sum(1 for t in trades if t[0] == "Yes")
    print(f"  side split     : {yes_n} Yes / {len(trades)-yes_n} No")
    print("\n  ⚠ small samples are noise — treat ROI as directional, not a return "
          "estimate, until you have hundreds of trades.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=8, help="x100 closed events to scan")
    ap.add_argument("--edge", type=float, default=0.07)
    ap.add_argument("--decision-hour", type=int, default=18, help="UTC hour day-before")
    ap.add_argument("--min-price", type=float, default=0.03)
    ap.add_argument("--max-price", type=float, default=0.97)
    ap.add_argument("--limit", type=int, default=0, help="cap # temperature events")
    ap.add_argument("--eval-after", default="", help="only evaluate markets "
                    "resolving on/after YYYY-MM-DD (out-of-sample test)")
    args = ap.parse_args()
    run(args.pages, args.edge, args.decision_hour, args.min_price, args.max_price,
        args.limit, eval_after=args.eval_after)


if __name__ == "__main__":
    main()
