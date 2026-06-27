"""Measure whether the forecast model actually has edge, and learn per-station
calibration (bias + sigma) to improve it.

Two modes:

  # Historical backtest (works today, no trades needed): replays the ensemble
  # model against ERA5 actuals over the last N days and scores it.
  python scripts/calibrate.py --backtest --days 21
  python scripts/calibrate.py --backtest --days 21 --write   # save calibration.yaml

  # Live report: reads the paper DB's logged forecasts + settled trades.
  python scripts/calibrate.py

Caveats on the backtest: it uses the ensemble *reforecast* for past dates (a
lower bound on true multi-day lead-time error) and ERA5 reanalysis as the
"actual" (a ~0.5-1.5°C proxy for the Wunderground station obs that actually
resolve markets). Treat it as a sanity check; the live report — which uses real
Polymarket resolutions — is the gold standard that accrues over the paper week.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import requests
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import STATIONS, ROOT
from src.forecast.openmeteo import (MaxTempForecast, ENSEMBLE_URL, ARCHIVE_URL,
                                    MODELS, DEFAULT_SIGMA_FLOOR)
from src.forecast.model import prob_exact
from src.forecast.emos import fit_emos, baseline_crps
from src.paper import store


# ----------------------------- data fetch (ranged) ---------------------------
HIST_FORECAST_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"


def model_daily_maxes(s: dict, start: str, end: str) -> dict[str, np.ndarray]:
    """Per-day mini-ensemble of daily-max temps from the *archived forecasts that
    were actually issued* (GFS/ICON/ECMWF). The ensemble-member API has no
    historical values, so we use cross-model spread as the ensemble for backtests.
    """
    r = requests.get(HIST_FORECAST_URL, params={
        "latitude": s["lat"], "longitude": s["lon"], "daily": "temperature_2m_max",
        "models": MODELS, "timezone": s["tz"], "start_date": start, "end_date": end},
        timeout=60)
    r.raise_for_status()
    d = r.json().get("daily", {})
    times = d.get("time", [])
    model_keys = [k for k in d if k.startswith("temperature_2m_max")]
    out = {}
    for i, date in enumerate(times):
        members = [d[k][i] for k in model_keys if d[k][i] is not None]
        if len(members) >= 2:
            out[date] = np.array(members, dtype=float)
    return out


def archive_actuals(s: dict, start: str, end: str) -> dict[str, float]:
    r = requests.get(ARCHIVE_URL, params={
        "latitude": s["lat"], "longitude": s["lon"], "daily": "temperature_2m_max",
        "timezone": s["tz"], "start_date": start, "end_date": end}, timeout=60)
    r.raise_for_status()
    d = r.json().get("daily", {})
    return {t: v for t, v in zip(d.get("time", []), d.get("temperature_2m_max", []))
            if v is not None}


def actuals(code: str, s: dict, start: str, end: str, source: str) -> dict[str, float]:
    """Daily-max actuals from the chosen source.
    'metar' = real station obs (resolution-aligned, IEM ASOS); falls back to ERA5
    per station if IEM returns nothing."""
    if source == "metar":
        from src.forecast.metar import station_daily_max
        got = station_daily_max(code, start, end, s["tz"])
        if got:
            return got
    return archive_actuals(s, start, end)


# ----------------------------- scoring ---------------------------------------
def brier_multiclass(fc: MaxTempForecast, actual: float, span: int = 6) -> tuple[float, list]:
    truth = round(actual)
    s, pairs = 0.0, []
    for d in range(truth - span, truth + span + 1):
        p = prob_exact(fc, d)
        o = 1.0 if d == truth else 0.0
        s += (p - o) ** 2
        pairs.append((p, o))
    return s, pairs


def reliability(pairs: list, bins: int = 10) -> list:
    buckets = [[] for _ in range(bins)]
    for p, o in pairs:
        buckets[min(bins - 1, int(p * bins))].append((p, o))
    out = []
    for i, b in enumerate(buckets):
        if b:
            out.append((i / bins, np.mean([p for p, _ in b]),
                        np.mean([o for _, o in b]), len(b)))
    return out


# ----------------------------- backtest --------------------------------------
def run_backtest(days: int, write: bool, source: str = "era5") -> None:
    end = dt.date.today() - dt.timedelta(days=6)      # ERA5 archive lags a few days
    start = end - dt.timedelta(days=days)
    print(f"Backtest window: {start} … {end}  ({days} days)\n")

    per_station_resid: dict[str, list] = defaultdict(list)
    per_station_ensvar: dict[str, list] = defaultdict(list)
    all_pairs, briers = [], []
    n = 0

    for code, s in STATIONS.items():
        try:
            ens = model_daily_maxes(s, start.isoformat(), end.isoformat())
            act = actuals(code, s, start.isoformat(), end.isoformat(), source)
        except Exception as e:  # noqa: BLE001
            print(f"  ! {code}: fetch failed ({e})")
            continue
        for date, members in ens.items():
            if date not in act:
                continue
            actual = act[date]
            fc = MaxTempForecast(code, date, members)   # uncalibrated
            resid = fc.mean - actual                    # >0 ⇒ model runs hot
            per_station_resid[code].append(resid)
            per_station_ensvar[code].append(fc.std ** 2)
            b, pairs = brier_multiclass(fc, actual)
            briers.append(b)
            all_pairs += pairs
            n += 1

    if not n:
        print("No samples — archive/ensemble unavailable for this window.")
        return

    print(f"Samples (station-days): {n}\n")
    resid_all = np.array([r for v in per_station_resid.values() for r in v])
    print(f"Forecast bias  (mean ensemble-mean − actual): {resid_all.mean():+.2f} °C")
    print(f"Forecast RMSE: {np.sqrt((resid_all ** 2).mean()):.2f} °C")
    print(f"Mean multiclass Brier (current model): {np.mean(briers):.4f}"
          f"   [uniform baseline ≈ {uniform_brier():.4f}]\n")

    print("Per-station calibration:")
    print(f"  {'stn':6}{'city':12}{'n':>4}{'bias':>8}{'resid σ':>9}{'ens σ':>8}{'→ sigma_floor':>15}")
    cal = {}
    for code in per_station_resid:
        r = np.array(per_station_resid[code])
        bias = float(r.mean())
        resid_sd = float(r.std(ddof=1)) if len(r) > 1 else float(abs(r).mean())
        ens_sd = float(np.sqrt(np.mean(per_station_ensvar[code])))
        floor = float(np.sqrt(max(0.25, resid_sd ** 2 - ens_sd ** 2)))
        cal[code] = {"bias": round(bias, 2), "sigma": round(floor, 2)}
        print(f"  {code:6}{STATIONS[code]['city'][:11]:12}{len(r):>4}"
              f"{bias:>+8.2f}{resid_sd:>9.2f}{ens_sd:>8.2f}{floor:>15.2f}")

    print("\nReliability (predicted → observed frequency):")
    for lo, mp, mo, c in reliability(all_pairs):
        bar = "█" * int(mo * 40)
        print(f"  p≈{mp:.2f}  obs={mo:.2f}  n={c:<5} {bar}")

    if write:
        # Write only the per-station BIAS — it transfers from the 3-model backtest
        # to the live 30-member ensemble. The sigma_floor depends on the live
        # ensemble's own spread, so leave it at the default until the live report
        # (real Polymarket resolutions) can tune it over the paper week.
        bias_only = {code: {"bias": v["bias"]} for code, v in cal.items()}
        path = ROOT / "config" / "calibration.yaml"
        header = ("# Learned per-station bias (°C), subtracted from the ensemble "
                  "mean.\n# Generated by scripts/calibrate.py --backtest --write.\n"
                  "# sigma intentionally omitted -> live model uses DEFAULT_SIGMA_FLOOR\n"
                  "# until the live report tunes it from real resolutions.\n")
        with open(path, "w") as fh:
            fh.write(header)
            yaml.safe_dump(bias_only, fh, sort_keys=True)
        print(f"\n✓ wrote per-station bias → {path}  (live bot applies it automatically)")
    else:
        print("\n(re-run with --write to save per-station bias to config/calibration.yaml)")


def uniform_brier(span: int = 6) -> float:
    k = 2 * span + 1
    p = 1 / k
    return (k - 1) * (p ** 2) + (1 - p) ** 2


# ----------------------------- EMOS / NGR fit --------------------------------
def run_emos(days: int, write: bool, source: str = "era5",
             end_str: str = "") -> None:
    """Fit per-station EMOS coefficients (a,b,c,d) by minimising CRPS and, with
    --write, merge them into config/calibration.yaml (preferred over bias/sigma).
    `end_str` lets you fit on an older window for out-of-sample validation."""
    end = dt.date.fromisoformat(end_str) if end_str else dt.date.today() - dt.timedelta(days=6)
    start = end - dt.timedelta(days=days)
    print(f"EMOS fit window: {start} … {end}  ({days} days)\n")
    print(f"  {'stn':6}{'city':12}{'n':>4}{'a':>7}{'b':>6}{'c':>7}{'d':>7}"
          f"{'CRPS₀':>8}{'CRPS*':>8}{'gain':>7}")

    cal_out: dict = {}
    tot_before = tot_after = 0.0
    n_all = 0
    for code, s in STATIONS.items():
        try:
            ens = model_daily_maxes(s, start.isoformat(), end.isoformat())
            act = actuals(code, s, start.isoformat(), end.isoformat(), source)
        except Exception:  # noqa: BLE001
            continue
        means, varis, ys = [], [], []
        for date, members in ens.items():
            if date in act and len(members) >= 2:
                fc = MaxTempForecast(code, date, members)
                means.append(fc.mean); varis.append(fc.std ** 2); ys.append(act[date])
        if len(ys) < 8:
            continue
        means = np.array(means); varis = np.array(varis); ys = np.array(ys)
        a, b, c, d, crps_star = fit_emos(means, varis, ys)
        crps0 = baseline_crps(means, varis, ys, DEFAULT_SIGMA_FLOOR)
        gain = (crps0 - crps_star) / crps0 * 100
        cal_out[code] = {"emos": {"a": round(a, 3), "b": round(b, 3),
                                  "c": round(c, 3), "d": round(d, 3)}}
        tot_before += crps0 * len(ys); tot_after += crps_star * len(ys); n_all += len(ys)
        print(f"  {code:6}{s['city'][:11]:12}{len(ys):>4}{a:>7.2f}{b:>6.2f}"
              f"{c:>7.2f}{d:>7.2f}{crps0:>8.3f}{crps_star:>8.3f}{gain:>6.0f}%")

    if n_all:
        print(f"\nPooled mean CRPS:  before {tot_before/n_all:.3f}  →  "
              f"after {tot_after/n_all:.3f}  "
              f"({(tot_before-tot_after)/tot_before*100:.0f}% better)")
    if write and cal_out:
        path = ROOT / "config" / "calibration.yaml"
        existing = {}
        if path.exists():
            existing = yaml.safe_load(path.read_text()) or {}
        for code, v in cal_out.items():        # merge emos into any existing bias
            existing.setdefault(code, {}).update(v)
        path.write_text(
            "# Per-station calibration. emos:{a,b,c,d} (preferred) from\n"
            "# scripts/calibrate.py --emos --write : μ=a+b·mean, σ²=c+d·var.\n"
            + yaml.safe_dump(existing, sort_keys=True))
        print(f"\n✓ merged EMOS coeffs into {path}  (live model applies them)")
    elif cal_out:
        print("\n(re-run with --write to save EMOS coeffs)")


# ----------------------------- live report -----------------------------------
def run_live_report() -> None:
    con = store.connect()
    fcs = con.execute(
        "SELECT * FROM forecasts WHERE actual_max IS NOT NULL").fetchall()
    print("=== Live calibration (paper DB) ===\n")
    if fcs:
        resid = np.array([f["mean"] - f["actual_max"] for f in fcs])
        print(f"Forecasts with known actuals: {len(fcs)}")
        print(f"  bias {resid.mean():+.2f} °C · RMSE {np.sqrt((resid**2).mean()):.2f} °C")
    else:
        print("No forecast actuals yet (backfills once a traded day resolves).")

    settled = con.execute(
        "SELECT model_prob, resolved_yes, pnl FROM fills "
        "WHERE status='settled' AND resolved_yes IS NOT NULL").fetchall()
    print(f"\nSettled trades: {len(settled)}")
    if settled:
        brier = np.mean([(s["model_prob"] - s["resolved_yes"]) ** 2 for s in settled])
        wins = sum(1 for s in settled if s["pnl"] > 0)
        print(f"  Brier (P(Yes) vs outcome): {brier:.4f}")
        print(f"  win rate: {wins}/{len(settled)} = {wins/len(settled):.0%}")
        print(f"  realized PnL: {sum(s['pnl'] for s in settled):+.2f} USDC")
    else:
        print("  (markets resolve next day — check back after settlements)")
    print("\nFor a model check now, run:  python scripts/calibrate.py --backtest")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--emos", action="store_true",
                    help="fit per-station EMOS/NGR coefficients (min CRPS)")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--source", choices=["era5", "metar"], default="era5",
                    help="actuals source: 'metar' = real station obs "
                         "(resolution-aligned, IEM ASOS); 'era5' = reanalysis")
    ap.add_argument("--end", default="", help="fit window end date YYYY-MM-DD "
                    "(for out-of-sample validation); default = today-6")
    args = ap.parse_args()
    if args.emos:
        run_emos(args.days, args.write, args.source, args.end)
    elif args.backtest:
        run_backtest(args.days, args.write, args.source)
    else:
        run_live_report()


if __name__ == "__main__":
    main()
