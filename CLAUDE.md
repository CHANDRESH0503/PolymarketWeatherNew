# PolymarketWeather

A trading bot for Polymarket's **daily high-temperature** markets, modeled on the
strategy of the trader [`@lactesting`](https://polymarket.com/@lactesting) and
designed to beat it on forecast accuracy.

---

## 1. What `@lactesting` actually does (data-driven analysis)

Pulled live from Polymarket's Data API (proxy wallet
`0x36f662fcbdc8f64aa1bbaa1f8897ca0e3bb7ae14`) — reproduce any time with
`python scripts/analyze_trader.py lactesting`:

| Metric | Value (5.5-day window) |
|---|---|
| Trades | 302 (~55/day) |
| Volume | ~$24,000 (median trade $50, max $3,328) |
| Open positions | 30, ~$2,100 value |
| Outcome bought | 184× **No**, 118× Yes |
| Median entry price | 0.67 |
| Markets | Daily high-temp for Asian cities: Taipei, Seoul, Guangzhou, Tokyo, Shanghai, Chengdu, Beijing, Chongqing, Busan, Wuhan (+ occasional London/Warsaw) |

**The strategy in one sentence:** trade short-dated daily "what's the high
temperature in city X" markets, where each whole-degree bucket (e.g. *"25°C"*,
*"27°C or higher"*) is a separate Yes/No contract, and exploit the fact that
retail prices these buckets worse than a real weather forecast would.

Key structural facts that make the edge possible:

- **Resolution is mechanical.** Each market resolves to the highest temperature
  recorded at one specific airport station (e.g. Seoul → Incheon/RKSI, Tokyo →
  Haneda/RJTT), sourced from Wunderground, **rounded to a whole degree Celsius**.
  No human judgment — so a good probabilistic forecast is directly tradable.
- **Buckets are a partition.** An event is ~11 mutually-exclusive buckets whose
  probabilities sum to 1. Most individual buckets are *unlikely*, so buying
  **No** is right most of the time — which is exactly `@lactesting`'s bias.
- **Edge decays to zero at resolution.** The big "winners" you see late in the
  day (buckets at 0.001/0.999) are already decided and untradeable. Real money
  is made entering **the day before**, which is what this bot targets.

> Note: at the snapshot above the trader was *down* ~$680 on open positions —
> evidence that naive entries still carry real variance. Beating them is about
> forecast quality + disciplined sizing + tradability filtering, all below.

---

## 2. How this bot works

```
discover markets ─▶ ensemble forecast ─▶ bucket probabilities ─▶ edge ─▶ Kelly ─▶ order
 (Gamma API)        (Open-Meteo, free)    (resolution-rule aware) (vs price) (sized)  (CLOB)
```

1. **`src/polymarket/gamma.py`** — discovers open temperature events via the
   Weather tag, parses each bucket into `(kind=exact|gte|lte, degree)`, and reads
   the resolution **station code** straight out of the market description.
2. **`src/forecast/openmeteo.py`** — pulls *every member* of the GFS, ICON and
   ECMWF ensembles for that station's coordinates and builds a distribution of
   the daily max temperature (not a point estimate).
3. **`src/forecast/model.py`** — converts that distribution into a probability
   for each bucket **using Polymarket's exact rule** (round-to-whole-degree):
   `P(25°C) = P(24.5 ≤ max < 25.5)`, etc. A `SIGMA_FLOOR` adds humility for
   model + station error.
4. **`src/strategy/edge.py`** — compares model probability to market price on
   both Yes and No, keeps the better side if `edge ≥ MIN_EDGE`, and filters out
   markets that are mid-resolution (`MIN_HOURS_TO_RESOLVE`, price band).
5. **`src/strategy/sizing.py`** — fractional-Kelly stake, capped per market.
6. **`src/polymarket/clob.py`** — order execution. **`DRY_RUN=1` by default**:
   it logs orders instead of sending them. Live trading needs wallet creds.

### Run it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # defaults are safe (DRY_RUN=1)

python -m src.bot             # one scan, prints signals, places nothing
python -m src.bot --loop 600  # rescan every 10 minutes
pytest -q                     # model sanity tests
```

Sample dry-run output (real, June 5 markets):

```
BUY No  @ 0.530 (model P(Yes)=0.152, edge=+0.318, $100.00)  ...Tokyo be 21°C on June 5?
BUY Yes @ 0.255 (model P(Yes)=0.535, edge=+0.280, $100.00)  ...Chongqing be 30°C or below
BUY No  @ 0.670 (model P(Yes)=0.152, edge=+0.178, $100.00)  ...Taipei be 30°C on June 5?
```

### Paper trading + dashboard (the week-long dry run)

Before risking money, run the strategy on a virtual $2,000 book that fills, marks
to live Polymarket prices, and settles on resolution — all persisted to SQLite
(`data/paper.db`).

```bash
./run_paper.sh            # trader daemon (ticks every 15 min) + dashboard
# open http://127.0.0.1:8000
```

Or run the pieces separately:

```bash
python -m src.paper.trader --loop 900   # paper fills + live marks + settlement
python -m src.server                    # dashboard API on :8000
```

The **dashboard** (an almanac/ledger-styled terminal — Fraunces + IBM Plex Mono,
hand-built SVG charts) shows live:

- **Summary plates** — equity, total/realized/unrealized P&L, ROI, cash, win
  rate, and **Brier score** (forecast calibration; lower = sharper).
- **Portfolio equity** curve vs the starting-cash baseline.
- **Open positions** ledger with live marks, model probability, edge, and P&L.
- **Forecast vs Market** — the signature view: for any city, the model's
  ensemble bucket probabilities bar-charted against the market-implied ones, with
  the ensemble mean ± σ. This is *literally a picture of the edge*.
- **Realized P&L by day** and a **trade blotter** (open/settled, outcome).

It polls every 20s and flags itself `live` / `stale` based on the last tick.
When the week is up, the same equity curve, win rate and Brier score tell you
whether the edge is real before you flip `DRY_RUN=0`.

### Going live (only when you're ready)

Live trading uses **`src/live/`**, which is the paper engine with one method
swapped: `LiveBroker(PaperBroker)` overrides only `_fill` to route through
`clob.place_order`, and `src.live.trader` runs the **same `tick()`** as the paper
daemon. So dedup, the cash/per-day/per-city caps, and equity sizing are identical
to the validated paper run — do **not** use the bare `src.bot` loop for real
money (it has none of those guards and re-buys every scan).

1. Fund a Polymarket account; put its proxy address + the controlling EOA
   private key in `.env`.
2. `python -m src.polymarket.clob --create-api-key` → paste the creds into `.env`.
3. Shadow first: `DRY_RUN=1 python -m src.live.trader --loop 900` simulates the
   live path into `data/live.db` and sends nothing.
4. Set `DRY_RUN=0` and run `python -m src.live.trader --loop 900`. It refuses to
   start without `PK`+`CLOB_API_KEY`, and assumes the wallet holds ≥ `BANKROLL`
   USDC. Point the dashboard at the live book with `PAPER_DB=data/live.db`.

> Fidelity caveat: with `DRY_RUN=0` the recorded fill is the marketable-limit
> estimate; reconciling the local ledger against on-chain fills/positions
> (`data_api.py`) is the next hardening step.

---

## 3. How to beat `@lactesting` — accuracy roadmap

The bot above already has *one* edge they may not: a calibrated multi-model
ensemble distribution mapped exactly to the resolution rule. To push accuracy
further, in rough order of bang-for-buck:

**Calibration & backtesting — ✅ built (`scripts/calibrate.py`)**

This is how we answer "does the model actually have edge" *before* risking money.

```bash
python scripts/calibrate.py --backtest --days 28          # score the model vs history
python scripts/calibrate.py --backtest --days 28 --write  # save per-station bias
python scripts/calibrate.py                               # live report (paper week)
```

The backtest replays the archived GFS/ICON/ECMWF forecasts that were actually
issued against ERA5 actuals, and reports multiclass Brier vs a uniform baseline,
RMSE, a reliability diagram, and per-station bias/sigma. The live trader logs
every forecast (`forecasts` table) and backfills the realized max, so the same
report runs on **real Polymarket resolutions** as the paper week accrues.

First-run findings (28-day backtest): model RMSE ≈ 1.0°C, Brier ≈ 0.75 vs 0.92
uniform — **real skill**. Measured per-station biases (Seoul −1.0°C cold, Beijing
/Tokyo +0.6°C warm) are now written to `config/calibration.yaml` and applied
automatically. The forecast is mildly *under-confident* (over-dispersed); the
`sigma_floor` is left at the default until the live week's resolutions tune it.

> Caveats: ERA5 ≈ but ≠ the Wunderground station obs that resolve markets
> (~0.5–1.5°C), and the backtest's 3-model spread ≠ the live 30-member ensemble
> spread. So the **bias** corrections transfer; the **sigma** is confirmed live.

**EMOS / NGR calibration — ✅ built (`scripts/calibrate.py --emos`)**

The rigorous successor to bias+sigma_floor. Fits `y | ens ~ N(a+b·mean, c+d·var)`
by minimising CRPS (`src/forecast/emos.py`), learning the mean *and* spread
jointly per station. First fit (40 days): **pooled CRPS −29%** (0.573→0.405; up
to −53% Seoul), with variance-scaling `d<1` — i.e. it sharpened the
over-dispersed ensemble, exactly the diagnosed problem. Coeffs are merged into
`config/calibration.yaml` and applied automatically (EMOS overrides bias).

```bash
python scripts/calibrate.py --emos --days 40 --write
```

> Reality check from the n=4,584 market backtest: even well-calibrated, our
> forecast is ~level with (slightly behind) the market price. EMOS mainly matters
> because the **LP daemon quotes around our fair value** — a sharper fair value
> means less adverse selection — not because it manufactures a directional edge.

**METAR resolution-source calibration — ✅ built (`--source metar`)**

We now calibrate against the *actual station observations* that resolve markets
(Iowa Environmental Mesonet ASOS archive, keyed by ICAO = our STATIONS key,
whole-°C like the resolution), not ERA5 reanalysis. `src/forecast/metar.py`
provides it; `calibrate.py --source metar` uses it for backtest + EMOS, and the
live paper engine backfills actuals from METAR (ERA5 fallback).

This mattered concretely: EMOS fit on **ERA5** *worsened* the market Brier
(0.069→0.078) because it sharpened the distribution confidently around the ERA5
value, which is ~1°C off the METAR resolution. Re-fitting EMOS on **METAR**
(`--emos --source metar --write`) aligns the sharpening with what actually
resolves the market.

```bash
python scripts/calibrate.py --emos --source metar --days 40 --write
```

**Tier 3 — intraday nowcasting (information/speed edge) — ✅ built (`src/forecast/nowcast.py`)**

The daily max that resolves a market is `M = max(observed-so-far, remaining-hours)`.
By early afternoon the observed station max is a **hard floor** on the high; as
the 2–4 PM peak passes, the remaining hours can no longer beat it and the
distribution **collapses** onto the floor. The nowcaster pulls the same IEM ASOS
station obs the resolver uses (`metar.station_obs_today`), keeps the ensemble only
for the *remaining* hours (`openmeteo.fetch_hourly_members`), and Monte-Carlos the
daily max with the floor clamped in — so a point mass piles up exactly where the
obs already are. Bucket probs come off the empirical samples with the same
round-half-up rule as `forecast.model`.

```bash
python scripts/nowcast_scan.py              # ENS vs NOW vs MKT for today's markets
python scripts/nowcast_scan.py --loop 300   # watch the "lock%" climb through the afternoon
NOWCAST=1 python -m src.bot                  # use the nowcast for same-day markets
```

The edge is real *only if the market is slow to reprice live obs* — `nowcast_scan`
exists to test exactly that (the `NOW-MKT` gap). Off by default (`NOWCAST=0`):
ensemble-only stays the safe baseline until the slowness is demonstrated.

**Correlation-aware Kelly — ✅ built + wired (`sizing.correlation_kelly`)**

Independent Kelly over-bets when one heat wave moves many cities together. The
log-normal simultaneous-Kelly solution `f* = Σ⁻¹μ` (per-dollar returns μ,
covariance Σ from an equicorrelation `rho`) shrinks the correlated legs. When
`CORR_KELLY=1`, `edge.generate_signals` re-sizes the whole book together
(`_apply_portfolio_sizing`) within the cash-buffer-adjusted bankroll.

**Tier 0 — resolution-source audit (the load-bearing check) — ✅ built**

Everything rests on one assumption: round(our METAR daily max) == the number
Polymarket resolves on. `src/analysis/resolution_audit.py` measures it on resolved
markets — reads the winning whole-degree from Polymarket vs our IEM ASOS max.
`scripts/resolution_audit.py` runs it, stores per-event rows + summary in the paper
DB, and prints a verdict; `/api/resolution_audit` + the dashboard's Resolution Audit
panel surface match-rate, ±1°C rate, mean |Δ°C|, the Δ histogram, and per-station.
Latest run: **n≈21, ~95% exact match, 100% within ±1°C, mean |Δ| ≈ 0.05°C** → the
source is faithful (only outlier is Seoul/RKSI, consistent with its known bias).

```bash
python scripts/resolution_audit.py --pages 25   # audit + store + verdict
```

**Execution realism — depth-aware paper fills — ✅ built**

Paper fills used to magically fill the whole size at the top-of-book quote; real
weather books are thin (~$0.11 depth seen in arb scans). Now `PaperBroker` walks
the live book (`clob.walk_asks`): VWAP fill price, depth-capped (partial) size,
and recorded `slippage` + `fill_ratio` per fill. Falls back to quoted-price fills
if the book is unavailable. Toggle with `PAPER_DEPTH` (default on).

**Risk — cash buffer + per-day capital cap — ✅ built**

`CASH_BUFFER` (default 0.10) keeps a fraction of bankroll permanently in reserve;
the broker never spends below `CASH_BUFFER × starting_cash`, and corr-Kelly sizes
to `BANKROLL × (1 − CASH_BUFFER)`. `MAX_DAY_FRACTION` (default 0.50) caps how much
capital any single *resolution day's* markets can hold — markets resolve next-day
so cash recycles daily, and without this one day deploys the whole bankroll and
starves the next day (which is already trading). Enforced in `engine.execute` via
`capped_budget(stake, cash, floor, day_deployed, day_cap)` =
`min(stake, cash−floor, day_cap−day_deployed)`. The dashboard's Capital Allocation
panel shows the deployment gauge vs reserve ceiling **and** per-resolution-day
deployment vs the day cap (red when over).

**Further forecast quality**
- **Whole-degree rounding bias** near .5 boundaries still to model explicitly.
- **Add more / better models.** Bring in ECMWF ENS proper and the Open-Meteo
  `best_match`.
- **Blend with climatology** as a prior when ensemble spread is thin, so no
  bucket gets an over-confident ~0 probability.

**Calibration & validation**
- **Backtest harness.** Replay historical forecasts vs resolved markets; track
  Brier score, log-loss, and a reliability diagram. Only trade buckets where the
  model is *proven* calibrated.
- **Tighten `SIGMA_FLOOR` empirically** instead of guessing it.

**Market / execution edge**
- **Enforce coherence.** Re-normalize the 11 bucket probabilities to sum to 1,
  then arbitrage internal inconsistencies in the order book (sum of Yes prices
  ≠ 1 is free money).
- **Liquidity & slippage model.** Read the real order book, size to available
  depth, use limit (maker) orders to earn the rebate `@lactesting` chases.
- **Capture LP rewards.** These markets run reward programs (visible in the tags);
  passive quoting around fair value earns yield on top of directional edge.

**Risk**
- Per-event and per-day exposure caps, correlation awareness (one heat wave moves
  many cities together), and a kill-switch on model-vs-realized drift.

---

## Project layout

```
src/
  config.py              env + station registry
  polymarket/
    gamma.py             market discovery + bucket/station parsing
    data_api.py          read-only positions/activity (self + other traders)
    clob.py              guarded order execution (DRY_RUN default)
  forecast/
    openmeteo.py         ensemble max-temp distribution
    model.py             distribution -> bucket probability + apply_calibration
    emos.py              EMOS/NGR fit (CRPS-minimised Gaussian calibration)
    metar.py             real station obs (IEM ASOS) — actuals + intraday floor
    nowcast.py           Tier 3 intraday nowcaster (observed floor + remaining hrs)
  analysis/
    resolution_audit.py  METAR-vs-Polymarket-resolution fidelity audit
  strategy/
    edge.py              edge detection + tradability + NOWCAST + corr-Kelly + drift guard
    drift.py             per-station drift kill-switch (bench a station whose rolling MAE blows out)
    sizing.py            fractional Kelly + correlation-aware (simultaneous) Kelly
    arbitrage.py         coherence / neg-risk basket arb (Σ YES vs 1)
    market_making.py     fair-value LP quotes + reward-maximising 2-sided sizing
    lp_rewards.py        CLOB v2 maker-reward scoring (S(v,s), Q_min, reward est)
  paper/
    store.py             SQLite schema/helpers (data/paper.db; PAPER_DB override)
    engine.py            PaperBroker: fills, live marks, settlement, equity, caps
    trader.py            paper-trading daemon (scan -> fill -> mark -> snapshot)
  live/
    engine.py            LiveBroker(PaperBroker): overrides _fill -> real CLOB order
    trader.py            live daemon: same tick() as paper, DRY_RUN-gated (data/live.db)
  notify.py              Telegram alerts (fires on settlement)
  bot.py                 minimal one-shot scan (NO caps/dedup — not for live money)
  server.py              dashboard API + static serving (Flask)
web/                     dashboard (index.html / styles.css / app.js)
                         live panels: equity (interactive), intraday nowcast +
                         collapse meter, capital allocation, EMOS calibration,
                         and a System Almanac strip of active strategies
scripts/backfill_fills.py  heal forecast metadata on legacy paper fills
scripts/analyze_trader.py  profile any wallet (used to study @lactesting)
scripts/arb_scan.py        scan events for coherence arbitrage (--loop to catch windows)
scripts/lp_quotes.py       LP reward quote sheet (fair-value-anchored maker quotes)
scripts/lp_bot.py          live LP daemon: reward-ranked 2-sided quotes under capital cap (LP_EXECUTE-gated)
scripts/lp_backtest.py     backtest LP reward yield + adverse-selection P&L on recorded trades
scripts/market_backtest.py forecast-vs-PRICE edge test (our Brier vs market's)
scripts/calibrate.py       backtest + live calibration report (learns bias/sigma)
scripts/nowcast_scan.py    Tier 3 nowcast probe: ENS vs NOW vs MKT, market-slowness
scripts/resolution_audit.py  METAR vs Polymarket resolution match (--pages, --loop)
scripts/go_no_go.py        one-screen GO/NO-GO verdict for the paper week (load-bearing checks + thresholds)
config/cities.yaml         station -> coordinates lookup
config/calibration.yaml    learned per-station bias (generated by calibrate.py)
run_paper.sh               launch trader daemon + dashboard together
tests/                     probability / sizing / calibration sanity checks
```

*Educational / research tool. Trade at your own risk; start in DRY_RUN.*
