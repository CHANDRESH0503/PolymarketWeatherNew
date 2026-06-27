"""One-screen GO / NO-GO verdict for the paper-trading week.

Turns "is the edge real yet?" into a checklist with thresholds instead of a gut
feel. It reads the *live paper DB* (data/paper.db) — your own forecasts, fills
and resolutions — and scores the five things that actually decide whether to
flip DRY_RUN=0:

  0. Resolution-source fidelity  — does round(our METAR max) == the Polymarket
     resolution? (the load-bearing assumption; from the resolution_audit table)
  1. Edge vs the MARKET          — our Brier vs the market-implied Brier on the
     buckets we actually traded (beating a uniform baseline is not enough)
  2. Calibration / reliability   — when we say 70%, does it happen ~70%?
  3. Equity & drawdown           — are we positive, and survivable at real size?
  4. Execution realism           — fills actually filling, slippage not eating it

Each check returns PASS / MARGINAL / FAIL / INSUFFICIENT (too few samples to
judge). The overall gate is conservative: GO only if the load-bearing checks
PASS, nothing FAILs, and the sample is big enough to mean something.

    python scripts/go_no_go.py                 # verdict from the paper DB
    python scripts/go_no_go.py --refresh-audit 8   # run a fresh resolution audit first
    python scripts/go_no_go.py --days 14       # restrict to the last N days of fills
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.analysis.resolution_audit import summarize as summarize_audit
from src.paper import store

# Minimum independent-ish samples before a statistical check means anything.
# These markets are highly correlated (one heat wave moves many cities), so the
# effective sample is far below the raw count — hence deliberately cautious gates.
MIN_AUDIT_N = 10
MIN_TRADE_N = 40

PASS, MARGINAL, FAIL, INSUF = "PASS", "MARGINAL", "FAIL", "INSUFFICIENT"
_ICON = {PASS: "✓", MARGINAL: "~", FAIL: "✗", INSUF: "·"}


class Check:
    def __init__(self, name: str, status: str, headline: str, detail: list[str] | None = None):
        self.name = name
        self.status = status
        self.headline = headline
        self.detail = detail or []

    def print(self) -> None:
        print(f"  [{_ICON[self.status]}] {self.name:<26} {self.status:<12} {self.headline}")
        for d in self.detail:
            print(f"        {d}")


# ----------------------------- the checks ------------------------------------
def check_resolution(con) -> Check:
    rows = [dict(r) for r in con.execute(
        "SELECT station, date, resolved_deg, metar_max, metar_deg, delta, matched "
        "FROM resolution_audit").fetchall()]
    s = summarize_audit(rows)
    n = s.get("n", 0)
    if n < MIN_AUDIT_N:
        return Check("0. Resolution fidelity", INSUF,
                     f"only n={n} audited (need ≥{MIN_AUDIT_N})",
                     ["run: python scripts/resolution_audit.py --pages 15"])
    rate = s["match_rate"]
    status = PASS if rate >= 0.90 else MARGINAL if rate >= 0.60 else FAIL
    detail = [f"exact {s['matched']}/{n} = {rate*100:.0f}% · "
              f"within ±1°C {s['within1_rate']*100:.0f}% · mean |Δ| {s['mean_abs_delta']:.2f}°C"]
    weak = [p for p in s["per_station"] if p["n"] >= 3 and p["match_rate"] < 0.7]
    if weak:
        detail.append("weak stations: " + ", ".join(
            f"{p['city']} {p['match_rate']*100:.0f}% (Δ{p['mean_delta']:+.1f})" for p in weak))
    return Check("0. Resolution fidelity", status,
                 f"{rate*100:.0f}% match on n={n}", detail)


def _market_pyes(f: dict) -> float | None:
    """Market-implied P(Yes) for a fill: the top-of-book quote we sized against,
    flipped to the Yes axis (a No fill at price p implies P(Yes)=1−p)."""
    price = f["quote_price"] if f["quote_price"] is not None else f["entry_price"]
    if price is None:
        return None
    return price if f["side"] == "Yes" else 1.0 - price


def check_edge_vs_market(con, since: float) -> Check:
    fills = [dict(r) for r in con.execute(
        "SELECT model_prob, quote_price, entry_price, side, resolved_yes FROM fills "
        "WHERE status='settled' AND resolved_yes IS NOT NULL AND ts>=?", (since,)).fetchall()]
    pairs = [(f["model_prob"], _market_pyes(f), f["resolved_yes"]) for f in fills]
    pairs = [(m, k, o) for m, k, o in pairs if m is not None and k is not None]
    n = len(pairs)
    if n < MIN_TRADE_N:
        return Check("1. Edge vs market", INSUF,
                     f"only n={n} settled trades (need ≥{MIN_TRADE_N})",
                     ["keep paper trading; markets resolve next-day"])
    m = np.array([p[0] for p in pairs]); k = np.array([p[1] for p in pairs])
    o = np.array([p[2] for p in pairs])
    our = float(np.mean((m - o) ** 2)); mkt = float(np.mean((k - o) ** 2))
    diff = mkt - our   # positive => we are sharper than the market
    status = PASS if diff > 0.003 else FAIL if diff < -0.003 else MARGINAL
    head = ("sharper than market" if status == PASS else
            "no edge over market" if status == FAIL else "≈ tie with market")
    return Check("1. Edge vs market", status, f"{head} (Δ {diff:+.4f})",
                 [f"our Brier {our:.4f}  vs  market Brier {mkt:.4f}  on n={n} traded buckets"])


def check_calibration(con, since: float) -> Check:
    rows = [dict(r) for r in con.execute(
        "SELECT model_prob, resolved_yes FROM fills "
        "WHERE status='settled' AND resolved_yes IS NOT NULL AND ts>=?", (since,)).fetchall()]
    n = len(rows)
    if n < MIN_TRADE_N:
        return Check("2. Calibration", INSUF, f"only n={n} (need ≥{MIN_TRADE_N})")
    p = np.array([r["model_prob"] for r in rows]); o = np.array([r["resolved_yes"] for r in rows])
    brier = float(np.mean((p - o) ** 2))
    uniform = 0.25   # Brier of always-0.5 on a binary outcome
    # ECE: weighted gap between predicted prob and observed frequency per decile.
    edges = np.linspace(0, 1, 6)
    ece = 0.0; table = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (p >= lo) & (p < hi if hi < 1 else p <= hi)
        if not m.any():
            continue
        pred, freq, c = p[m].mean(), o[m].mean(), int(m.sum())
        ece += c / n * abs(pred - freq)
        table.append(f"{lo:.1f}-{hi:.1f}: pred {pred:.2f} actual {freq:.2f} (n={c})")
    status = (FAIL if brier >= uniform else
              PASS if (brier < uniform and ece < 0.10) else MARGINAL)
    head = f"Brier {brier:.4f} vs {uniform:.2f} uniform · ECE {ece:.3f}"
    return Check("2. Calibration", status, head, table)


def check_equity(con) -> Check:
    eq = [dict(r) for r in con.execute(
        "SELECT ts, equity FROM equity ORDER BY ts ASC").fetchall()]
    start = store.get_meta(con, "starting_cash", 0.0)
    realized = con.execute(
        "SELECT COALESCE(SUM(pnl),0) s FROM fills WHERE status='settled'").fetchone()["s"]
    unreal = con.execute(
        "SELECT COALESCE(SUM(pnl),0) s FROM fills WHERE status='open'").fetchone()["s"]
    equity = start + realized + unreal
    if not eq or start <= 0:
        return Check("3. Equity & drawdown", INSUF, "no equity history yet")
    vals = np.array([r["equity"] for r in eq])
    peak = np.maximum.accumulate(vals)
    dd = (peak - vals)
    max_dd = float(dd.max()); max_dd_pct = float((dd / peak).max()) * 100
    total_pnl = equity - start
    # Equity is the noisiest signal — it is context only and never drives the
    # gate (see overall()), so a small early drawdown can't trigger a NO-GO.
    status = PASS if total_pnl > 0 else MARGINAL
    head = f"P&L {total_pnl:+.2f} ({total_pnl/start*100:+.1f}%) · max DD {max_dd:.2f} ({max_dd_pct:.1f}%)"
    return Check("3. Equity & drawdown", status, head,
                 [f"realized {realized:+.2f} · unrealized {unreal:+.2f} · equity {equity:.2f}",
                  "note: P&L is the noisy downstream signal — trust checks 0–2 more"])


def check_execution(con, since: float) -> Check:
    rows = [dict(r) for r in con.execute(
        "SELECT slippage, fill_ratio FROM fills WHERE slippage IS NOT NULL AND ts>=?",
        (since,)).fetchall()]
    n = len(rows)
    if n < MIN_TRADE_N:
        return Check("4. Execution realism", INSUF, f"only n={n} depth-aware fills")
    slip = np.array([r["slippage"] for r in rows]); fr = np.array([r["fill_ratio"] for r in rows])
    avg_slip = float(slip.mean()); avg_fill = float(fr.mean())
    # Good fills: we get most of the size, and slippage isn't eating the edge.
    status = (PASS if avg_fill >= 0.90 and avg_slip <= 0.01 else
              FAIL if avg_fill < 0.60 or avg_slip > 0.03 else MARGINAL)
    return Check("4. Execution realism", status,
                 f"avg fill {avg_fill*100:.0f}% · avg slippage {avg_slip*100:+.1f}¢",
                 [f"thin books mean partial fills + slippage shrink real-money size (n={n})"])


def check_logging(con) -> Check:
    """Integrity, not strategy: can calibrate.py actually re-fit on this week?"""
    settled = con.execute("SELECT COUNT(*) c FROM fills WHERE status='settled'").fetchone()["c"]
    with_fc = con.execute(
        "SELECT COUNT(*) c FROM fills WHERE status='settled' AND fc_mean IS NOT NULL").fetchone()["c"]
    fc_total = con.execute("SELECT COUNT(*) c FROM forecasts").fetchone()["c"]
    fc_actual = con.execute(
        "SELECT COUNT(*) c FROM forecasts WHERE actual_max IS NOT NULL").fetchone()["c"]
    if settled == 0:
        return Check("✓ Logging integrity", INSUF, "no settled fills yet")
    fc_rate = with_fc / settled
    status = PASS if fc_rate >= 0.95 else MARGINAL if fc_rate >= 0.7 else FAIL
    return Check("✓ Logging integrity", status,
                 f"{with_fc}/{settled} fills have forecast metadata",
                 [f"forecasts logged: {fc_total} · with backfilled actuals: {fc_actual}",
                  "needed so calibrate.py --emos can re-fit on the live week"])


# ------------------------------ verdict --------------------------------------
def overall(checks: list[Check]) -> tuple[str, list[str]]:
    """The gate is driven ONLY by the load-bearing checks (fidelity, edge,
    calibration, execution). Equity and logging are shown for context but never
    decide GO/NO-GO — equity is too noisy early, and a logging gap is an
    integrity warning, not a strategy verdict."""
    reasons = []
    fidelity = next(c for c in checks if c.name.startswith("0"))
    edge = next(c for c in checks if c.name.startswith("1"))
    calib = next(c for c in checks if c.name.startswith("2"))
    execu = next(c for c in checks if c.name.startswith("4"))
    logging = next(c for c in checks if "Logging" in c.name)

    # A logging gap doesn't change the strategy verdict, but flag it loudly:
    # without forecast metadata you can't recalibrate on the live week.
    if logging.status == FAIL:
        reasons.append("⚠ logging integrity FAILED — fills are missing forecast "
                       "metadata; calibrate.py can't re-fit on this week. Investigate.")

    # Hard disqualifiers: something is actually broken/unfaithful.
    broken = [c for c in (fidelity, calib, execu) if c.status == FAIL]
    if broken:
        for c in broken:
            reasons.append(f"{c.name} FAILED — {c.headline}")
        return "NO-GO", reasons

    if any(c.status == INSUF for c in (fidelity, edge, calib)):
        reasons.append("not enough resolved data yet on a load-bearing check — "
                       "keep the paper week running, then re-run this report")
        return "NOT YET", reasons
    if fidelity.status != PASS:
        reasons.append("resolution fidelity must be PASS before any edge is trustworthy")
        return "NOT YET", reasons
    if edge.status == FAIL:
        reasons.append("no demonstrated edge over the market price — if calibration is "
                       "solid, lean into LP/maker quoting (scripts/lp_quotes.py) rather "
                       "than going live directional")
        return "NOT YET", reasons
    if edge.status != PASS:
        reasons.append("edge over market is only a tie — needs to be clearly sharper; "
                       "keep collecting samples")
        return "NOT YET", reasons
    if calib.status == MARGINAL:
        reasons.append("calibration only marginal — tighten with "
                       "`calibrate.py --emos --source metar --write`, then re-check")
        return "BORDERLINE", reasons
    reasons.append("load-bearing checks pass with adequate sample — ramp in live at "
                   "TINY size first (DRY_RUN=0, small BANKROLL/MAX_STAKE), reconcile "
                   "real fills vs paper, then scale")
    return "GO", reasons


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=0,
                    help="only score fills from the last N days (0 = all)")
    ap.add_argument("--refresh-audit", type=int, default=0, metavar="PAGES",
                    help="run a fresh resolution audit (x100 closed events) first")
    args = ap.parse_args()

    con = store.connect()
    since = time.time() - args.days * 86400 if args.days else 0.0

    if args.refresh_audit:
        from src.analysis.resolution_audit import audit_events
        from scripts.market_backtest import fetch_closed_events
        print(f"refreshing resolution audit ({args.refresh_audit} pages)…", flush=True)
        rows = audit_events(fetch_closed_events(args.refresh_audit, 0))
        store.save_audit(con, rows)

    checks = [
        check_resolution(con),
        check_edge_vs_market(con, since),
        check_calibration(con, since),
        check_equity(con),
        check_execution(con, since),
        check_logging(con),
    ]

    window = f"last {args.days}d" if args.days else "all paper history"
    print(f"\n══ GO / NO-GO — paper-trading verdict ({window}) ══\n")
    for c in checks:
        c.print()

    verdict, reasons = overall(checks)
    print(f"\n──────────────────────────────────────────────")
    print(f"  VERDICT: {verdict}")
    for r in reasons:
        print(f"    • {r}")
    print(f"──────────────────────────────────────────────\n")


if __name__ == "__main__":
    main()
