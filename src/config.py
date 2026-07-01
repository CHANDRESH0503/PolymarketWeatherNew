"""Central configuration: env vars + station registry."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _clean(key: str, default: str) -> str:
    """Return env var value with inline shell comments stripped."""
    return os.getenv(key, default).split("#")[0].strip()


def _f(key: str, default: float) -> float:
    return float(_clean(key, str(default)))


def _b(key: str, default: str) -> bool:
    return _clean(key, default) == "1"


# Strategy knobs
MIN_EDGE = _f("MIN_EDGE", 0.07)
KELLY_FRACTION = _f("KELLY_FRACTION", 0.25)
MAX_STAKE_PER_MARKET = _f("MAX_STAKE_PER_MARKET", 5)
# Floor on a single ticket, in USDC. A sized stake below this is dropped (not
# rounded up) — it filters marginal, low-edge bets that aren't worth a position.
MIN_STAKE_PER_MARKET = _f("MIN_STAKE_PER_MARKET", 1.5)
# Per-market stake cap as a fraction of *live equity*. The effective ceiling is
# min(MAX_STAKE_PER_MARKET, MAX_STAKE_FRACTION × equity), so a single bet can't
# blow up the book — and the cap rises as capital compounds and falls in a
# drawdown. Set 0 to disable and fall back to the flat MAX_STAKE_PER_MARKET.
MAX_STAKE_FRACTION = _f("MAX_STAKE_FRACTION", 0.05)
BANKROLL = _f("BANKROLL", 100)
DRY_RUN = _b("DRY_RUN", "1")

# Fraction of bankroll always kept in cash (never deployed). A reserve buffer so
# the book can't drift to 100%-invested with no dry powder.
CASH_BUFFER = _f("CASH_BUFFER", 0.10)
# Max fraction of bankroll committed to any single resolution-day's markets, so
# one day can't eat the whole book and starve the next day (which is already
# trading). Positions recycle daily, so this keeps dry powder for each new date.
MAX_DAY_FRACTION = _f("MAX_DAY_FRACTION", 0.5)
# Max fraction of bankroll committed to any single CITY's open positions, across
# all resolution days. One heat wave moves every bucket of a city together, so
# without this the book concentrates in one station (the paper week ran ~70% in
# Seoul) — a single bad station read or source mismatch then dominates P&L. Caps
# correlated single-name risk on top of the per-day cap.
MAX_CITY_FRACTION = _f("MAX_CITY_FRACTION", 0.25)
# Profit compounding. The capital base (starting_cash) — which raises the reserve
# floor and the per-day/per-city exposure caps above, so a winning book deploys
# more — can be grown from realized profit. This is now MANUAL: use the
# dashboard's "Add to Bankroll" control to move a chosen amount of realized P&L
# into the base (PaperBroker.add_to_bankroll). The legacy automatic per-day sweep
# is OFF by default; flip PROFIT_SWEEP=1 to re-enable it.
PROFIT_SWEEP = _b("PROFIT_SWEEP", "0")
PROFIT_SWEEP_MIN = _f("PROFIT_SWEEP_MIN", 10)
PROFIT_SWEEP_FRACTION = _f("PROFIT_SWEEP_FRACTION", 0.5)
# Paper fills walk the live order book (depth + slippage) instead of magically
# filling the whole size at the quoted price. Falls back to quoted-price fills if
# the book is unavailable.
PAPER_DEPTH = _b("PAPER_DEPTH", "1")
# Minimum spacing (seconds) between equity-curve points. The trader may tick more
# often than this (to fill/mark/settle promptly), but a new equity time-series
# point is only written once per interval — keeps the dashboard chart from
# updating every short tick. Default 600s = every 10 minutes.
EQUITY_SNAPSHOT_INTERVAL = _f("EQUITY_SNAPSHOT_INTERVAL", 600)

# Tradability filters. Markets within a few hours of resolution are effectively
# decided (prices pinned to 0.001/0.999, no liquidity), so forecast "edge" there
# is fake. Only trade a sane price band and a minimum horizon.
MIN_PRICE = _f("MIN_PRICE", 0.03)
MAX_PRICE = _f("MAX_PRICE", 0.97)
MIN_HOURS_TO_RESOLVE = _f("MIN_HOURS_TO_RESOLVE", 8)

# Absolute floor on the predictive max-temp sigma (°C) — a low safety against a
# pathologically collapsed distribution, applied to EVERY station. Keep it LOW:
# a 28-day backtest showed raising it to 1.5 over-widened the 9 well-calibrated
# stations (whose realized residual σ is only 0.44–1.06) and DEGRADED multiclass
# Brier 0.7424 → 0.7550. Per-station humility for the few overconfident stations
# (e.g. RCSS/Taipei) is the targeted `sigma_min:` key in config/calibration.yaml,
# not this global knob.
MIN_PREDICTIVE_SIGMA = _f("MIN_PREDICTIVE_SIGMA", 0.5)

# Auto-execution gates. Each is an EXTRA opt-in on top of DRY_RUN+PK: even with
# DRY_RUN=0 and a funded wallet, the arb / LP bots only send orders when their
# own flag is set. Default off.
ARB_EXECUTE = _b("ARB_EXECUTE", "0")
ARB_MIN_PROFIT = _f("ARB_MIN_PROFIT", 0.02)        # min basket edge to act on
ARB_MAX_CAPITAL = _f("ARB_MAX_CAPITAL", 200)       # max USDC deployed per basket
LP_EXECUTE = _b("LP_EXECUTE", "0")
LP_SIZE = _f("LP_SIZE", 50)                        # shares per maker quote (per side)
LP_EVENTS = _clean("LP_EVENTS", "")
LP_MAX_CAPITAL = _f("LP_MAX_CAPITAL", 50)          # max USDC tied up in resting quotes
LP_COMPETITION = _f("LP_COMPETITION", 0.0)         # assumed summed Q_min of OTHER makers
                                                   # (only affects the reward ESTIMATE)

# Tier 3 — intraday nowcasting. When on, same-day markets are scored from a
# nowcast that folds the running observed station max (the resolution source) in
# as a hard floor and only forecasts the remaining hours, sharpening as the
# afternoon peak passes. Off by default (it's the unproven "is the market slow?"
# alpha hunt; ensemble-only is the safe baseline).
NOWCAST = _b("NOWCAST", "0")
NOWCAST_RESID_SIGMA = _f("NOWCAST_RESID_SIGMA", 0.6)   # °C residual on remaining-hours max
NOWCAST_SAMPLES = int(_f("NOWCAST_SAMPLES", 4000))     # MC samples for the daily-max dist
CORR_KELLY = _b("CORR_KELLY", "0")
CORR_KELLY_RHO = _f("CORR_KELLY_RHO", 0.3)             # assumed cross-bet outcome correlation

# Per-station drift kill-switch. When a station's recent forecast keeps missing
# (rolling MAE over the last DRIFT_LOOKBACK_DAYS ≥ DRIFT_MAX_MAE °C, with at least
# DRIFT_MIN_SAMPLES resolved days), the model is demonstrably wrong there — e.g.
# RCSS/Taipei ran 3–4σ hot in a heat regime σ-widening alone didn't catch. We
# SKIP all trades on a drifting station until its error recovers (the lookback
# window self-heals as good days accrue). Capital protection > a few missed
# trades, so it's on by default. Needs the forecasts table (paper store) backfilled.
DRIFT_GUARD = _b("DRIFT_GUARD", "1")
DRIFT_LOOKBACK_DAYS = int(_f("DRIFT_LOOKBACK_DAYS", 7))
DRIFT_MAX_MAE = _f("DRIFT_MAX_MAE", 2.5)               # °C rolling MAE that benches a station
# Resolved days required before the guard can trip. =1 reacts to the FIRST big
# miss (a single 2.5°+ error is ~3σ — a strong signal; the lookback self-heals a
# fluke). A temporal backtest on the paper run showed =2 reacts a day too late —
# it benched Taipei only AFTER both ruinous losses, then sat out the recovering
# winning days (net worse). =1 caught the second loss. Capital protection wins.
DRIFT_MIN_SAMPLES = int(_f("DRIFT_MIN_SAMPLES", 1))

# Smart-money agreement signal. Cross-check our signals against proven weather
# traders' live entries (matched by Polymarket token id). Advisory by default —
# it tags each signal confirm/against and nudges size — never a blind copy.
PEER_SIGNAL = _b("PEER_SIGNAL", "0")
# Default peer: automatedAItradingbot (0xd8f8…0f11) — trades our exact Asian
# universe on our ~14h horizon and is currently profitable (see WALLETS.md).
PEER_WALLETS = [w.strip() for w in _clean(
    "PEER_WALLETS", "0xd8f8c13644ea84d62e1ec88c5d1215e436eb0f11").split(",") if w.strip()]
PEER_LOOKBACK_HOURS = _f("PEER_LOOKBACK_HOURS", 36)    # how far back to read peer trades

# No-favorite harvesting sleeve (low-variance lane, modeled on HondaCivic but
# entered the day before, not at T-1h). Take No on buckets our CALIBRATED model
# says are near-impossible (P(Yes) <= NO_HARVEST_MAX_P) yet the No price still has
# room — small, capped tickets. Deliberately re-includes the cheap-Yes buckets the
# normal MIN_PRICE band excludes, gated on model confidence instead.
NO_HARVEST = _b("NO_HARVEST", "0")
NO_HARVEST_MAX_P = _f("NO_HARVEST_MAX_P", 0.03)        # max model P(Yes) to call it a "favorite No"
NO_HARVEST_STAKE = _f("NO_HARVEST_STAKE", 25)          # hard cap per sleeve ticket (USDC)

# Resolution-audit TRUST GATE for the capital-builder harvest sleeves. A station
# is only trusted to harvest on if round(our METAR daily max) has matched the
# Polymarket resolution on >= AUDIT_MIN_MATCH of >= AUDIT_MIN_SAMPLES audited
# resolutions (table populated by scripts/resolution_audit.py). The load-bearing
# safety check: without it a "locked" favorite could be one our source disagrees
# with the resolver on. (Seoul/RKSI was once feared a ~50% match; that was a flaky
# IEM fetch dropping its rows, not a real mismatch — with metar.py's retry it now
# audits 6/6 = 100%, like every other station.) 0/0 ⇒ gate off (trust all).
AUDIT_MIN_MATCH = _f("AUDIT_MIN_MATCH", 0.8)
AUDIT_MIN_SAMPLES = int(_f("AUDIT_MIN_SAMPLES", 5))

# Tier 3 capital-builder: same-day CONVERGENCE HARVEST (the ColdMath edge, risk-
# gated). Once the day's high has locked in (nowcast collapse-meter floor_locked
# >= MIN_LOCK), buy the near-certain bucket (model P >= MIN_P on whichever side is
# the favorite) for the last few cents of convergence — small capped tickets, on
# audit-trusted stations only. Deliberately bypasses MIN_HOURS_TO_RESOLVE (it is
# the late same-day window the horizon filter excludes). Requires NOWCAST=1 (the
# nowcast scorer is what folds live obs in for same-day markets).
NOWCAST_HARVEST = _b("NOWCAST_HARVEST", "0")
NOWCAST_HARVEST_MIN_P = _f("NOWCAST_HARVEST_MIN_P", 0.92)      # min nowcast P on the favored side
NOWCAST_HARVEST_MIN_LOCK = _f("NOWCAST_HARVEST_MIN_LOCK", 0.85)  # min collapse-meter (floor_locked)
NOWCAST_HARVEST_MIN_EDGE = _f("NOWCAST_HARVEST_MIN_EDGE", 0.02)  # min edge vs price (room to harvest)
NOWCAST_HARVEST_STAKE = _f("NOWCAST_HARVEST_STAKE", 25)         # hard cap per harvest ticket (USDC)

# Coherence arbitrage surfacing in the paper loop: scan events for Σ best-ask(YES)
# < 1 baskets and record them for the dashboard. Real execution stays gated by
# ARB_EXECUTE (live only); this just flags the opportunities.
ARB_SCAN = _b("ARB_SCAN", "1")

# Forecast freshness: the daemon re-fetches a station's ensemble from Open-Meteo
# only when the cached copy is older than this. The underlying models update
# ~every 6h, so re-pulling every 15-min tick is wasteful — a 90-min TTL keeps the
# daemon's API footprint well under the free-tier caps while staying current.
# (The intraday nowcast still refreshes every tick — it must fold in new obs.)
FORECAST_TTL = _f("FORECAST_TTL", 5400)   # seconds (default 90 min)

# Wallet / CLOB
PK = os.getenv("PK", "")
POLY_PROXY_ADDRESS = os.getenv("POLY_PROXY_ADDRESS", "")
# py-clob-client signature type for the funder wallet:
#   0 = EOA, 1 = email/Magic proxy, 2 = legacy Polymarket proxy,
#   3 = POLY_1271 (EIP-1271 smart-contract wallet — the new pUSD/v2 account type).
# Polymarket's new accounts are EIP-7702-delegated smart wallets holding pUSD as
# collateral; those authenticate with signature_type=3 and the funder set to the
# PROFILE address (where the pUSD balance lives), not the deposit address.
SIGNATURE_TYPE = int(_clean("SIGNATURE_TYPE", "2"))
CLOB_API_KEY = os.getenv("CLOB_API_KEY", "")
CLOB_API_SECRET = os.getenv("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE = os.getenv("CLOB_API_PASSPHRASE", "")

# Open-Meteo API key (optional). Blank = free tier (shared daily/min rate limits,
# which a concurrent backtest can exhaust -> 429). Set it to use the higher-limit
# customer endpoints. The forecast layer auto-routes to customer-*.open-meteo.com
# and appends apikey when this is non-empty.
OPENMETEO_API_KEY = _clean("OPENMETEO_API_KEY", "")

# Telegram notifications
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Endpoints
GAMMA_API = "https://gamma-api.polymarket.com"
DATA_API = "https://data-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def load_stations() -> dict:
    with open(ROOT / "config" / "cities.yaml") as fh:
        return yaml.safe_load(fh)["stations"]


def load_calibration() -> dict:
    """Per-station {bias, sigma} learned by scripts/calibrate.py. Empty if not
    yet calibrated."""
    path = ROOT / "config" / "calibration.yaml"
    if not path.exists():
        return {}
    with open(path) as fh:
        return yaml.safe_load(fh) or {}


STATIONS = load_stations()
CALIBRATION = load_calibration()
