# Competitor wallet analysis — Polymarket daily-temperature traders

Peer traders worth learning from, profiled the same way this project reverse-
engineered `@lactesting`. **Everything here is public on-chain data**, not X
scraping: candidates came from Polymarket's own **weather leaderboard**
(`GET https://data-api.polymarket.com/v1/leaderboard?category=WEATHER&orderBy=PNL`),
and each strategy below is computed from the wallet's real `activity`/`positions`
via `src/polymarket/data_api.py`. X handles are the ones the trader themselves
linked on their Polymarket profile.

> Snapshot: 2026-06-05. PnL/volume are leaderboard all-time; the strategy
> metrics (side bias, entry price, cities, timing) are from each wallet's most
> recent ~500 temperature trades. Re-run any row with
> `python scripts/analyze_trader.py <wallet>`.

## At a glance

| Wallet (name) | X | All-time PnL / Vol | Edge style | Entry px | Side | Enters | Cities |
|---|---|---|---|---|---|---|---|
| `0x594edb…1c11` (ColdMath) | — | $141k / $11.4M | **same-day ≥0.90 convergence harvest** (capital-weighted) + cheap-tail dust | bimodal: ≥0.90 in size, <0.05 dust | all BUY (Yes+No) | **same-day (69%)** | **NYC** + global |
| `0x6a8d17…50f9` (onlylucknobrain) | — | $22.5k / $1.7M | diversified **day-ahead** forecast, tiny tickets | ~0.22 (live buckets) | Yes-heavy, some SELL | **+1 to +2 days** | **39 cities, global** |
| `0xd8f8c1…0f11` (automatedAItradingbot) | — | $65k / $2.6M | cheap-Yes longshots, **our exact Asian set** | ~0.02 | Yes-heavy | ~14h before | Taipei, Shanghai, Seoul, Moscow |
| `0x1f6679…4c8d` (ShyGuy1) | @Mask4che | $65k / $5.3M | cheap-Yes Asian longshots (top recent PnL) | ~0.15 | Yes-heavy | ~16h before | Seoul, Shenzhen, HK, NYC |
| `0x15ceff…d5fa` (HondaCivic) | @0xMarchyel | $59k / $7.4M | **No-favorite harvesting** (sell tails) | ~1.00 | No-heavy | ~5h before | Moscow, Chicago, NYC, Toronto |
| `0x6011655c…b31e` (HighTempTation) | — | $57k / $1.5M | No-favorite harvesting, **wide city set** | ~1.00 | No-heavy (lots of SELL) | mixed | Taipei, Cape Town, Wellington, Jeddah |
| `0x0f37cb…1410` (Hans323) | @Hans323 | $81k / $7.2M | buy near-certain favorites, low risk | ~0.96 | No-heavy | late | NYC, London, Paris, Shenzhen |

---

## The wallets

### 1. ColdMath — `0x594edb9112f526fa6a80b8f858a6379c8a2c1c11`
- **Leaderboard:** $141k PnL on **$11.4M** volume (the highest-turnover weather bot on the board). 30d +$3.4k, 7d +$112 — the edge is thin per-unit, earned on enormous turnover.
- **Observed (2026-06-08 refresh, last 437 trades):** **all BUY**, 100% temperature markets, **69% entered same-day** (0% beyond +1 day). Entry prices are **bimodal**: 283 at **<0.05** and 131 at **≥0.95**, almost nothing in between. **Top city NYC (283)** + ~20 global cities. ~500 open positions, **−$8.8k unrealized**.
- **Capital tells the real story** (where the $ go, not the trade count):
  | Entry-price band | trades | capital deployed |
  |---|---|---|
  | **≥ 0.90** | 131 | **$34,715** (avg $265, max $3,888) |
  | 0.5–0.9 | 15 | $3,170 |
  | 0.05–0.5 | 8 | $60 |
  | < 0.05 | 283 | $101 (dust) |
- **Read (corrected):** *not* a cheap-tail scatter — that's negligible capital (free lottery dust). The money is a **same-day convergence harvest**: wait until the afternoon peak passes and the daily high is essentially *locked*, then buy the near-certain bucket (≥0.90) **in size** for the last 1–10¢ of convergence, repeatedly, on the deepest book (NYC). This is exactly the zone our own `CLAUDE.md` called "untradeable" — ColdMath proves the market is **slow to reprice locked obs**, which is the thesis behind our Tier-3 nowcaster.
- **Helps us:** the live proof to **turn on the nowcast** (`NOWCAST=1`) for same-day markets and harvest near-locked favorites — gated by `resolution_audit` so we only buy buckets that are *actually* locked. Runs on negative open inventory, so copy the signal, **not** the bleed (we size off live equity instead).

### 2. automatedAItradingbot — `0xd8f8c13644ea84d62e1ec88c5d1215e436eb0f11`
- **Leaderboard:** $65k PnL / $2.6M vol.
- **Observed:** ~10 temp trades/day, median entry **0.02**, **Yes-heavy (205 Yes / 26 No)**, cities are **Taipei, Shanghai, Seoul, Moscow, London** — essentially **our STATIONS universe**. Enters ~13.6h before. 49 open positions, ~$2.2k value, **+$74 unrealized (in the green)**.
- **Read:** the closest peer to us — a disciplined bot buying **cheap Yes longshots** on the same Asian daily-high markets, modest size, currently profitable.
- **Helps us:** direct **head-to-head benchmark**. Same markets, same horizon. Track this wallet's entries vs our model's bucket probs: where it buys a 0.02 Yes that our ensemble says is ~0, we're likely right; where it buys one our model also likes, that's a calibration confirmation. Good candidate for an automated "are we agreeing with the smart money?" check.

### 3. ShyGuy1 — `0x1f66796b45581868376365aef54b51eb84184c8d`  ·  X: @Mask4che
- **Leaderboard:** $65k all-time / $5.3M vol, and **#1 weather trader this week and month** — the hottest recent hand.
- **Observed:** ~92 temp trades/day, median entry **0.15**, **Yes-heavy (400/63)**, cities **Seoul (188), Shenzhen (114), NYC, Hong Kong** — heavy overlap with us. Enters ~15.6h before. But: **475 open positions, only $4.2k value, ~-$23.9k unrealized**.
- **Read:** aggressive **cheap-Yes longshot** accumulation in Asian cities. Top *realized* recent PnL but a large *open* drawdown — i.e. high variance: realizes winners fast, carries a long tail of losers.
- **Helps us:** (a) confirms Seoul/Shenzhen/HK as the productive Asian books right now. (b) A **cautionary sizing case** — exactly the over-betting our correlation-aware Kelly + per-day cap are meant to prevent. Their -$24k open mark is what independent longshot stacking looks like when a heat pattern goes against you.

### 4. HondaCivic — `0x15ceffed7bf820cd2d90f90ea24ae9909f5cd5fa`  ·  X: @0xMarchyel
- **Leaderboard:** $59k PnL / **$7.4M** vol.
- **Observed:** ~205 temp trades/day, median entry **1.00 (0.90–1.00)**, overwhelmingly **No (398) / Yes (13)**, **Western** cities (Moscow, Chicago, NYC, Toronto, Houston, Buenos Aires). Enters **late — median ~5h before, p10 ~0.1h**. Currently **-$5.6k unrealized**.
- **Read:** the scaled-up version of `@lactesting`'s bias — **buy No on unlikely buckets at ~0.97–1.00 near resolution**, collecting the last few cents of premium on near-certain outcomes. High volume, thin margins, fat-tail risk.
- **Helps us:** defines the **No-favorite harvesting** lane and its failure mode. We can do this *better* by entering on a calibrated forecast the day before (more edge, less crowding) instead of scalping pennies at T-1h. Their current red mark is the argument for our `MIN_HOURS_TO_RESOLVE` filter and tail-aware sizing.

### 5. HighTempTation — `0x6011655c4afb76f36dd1b08a137a1ba73466b31e`
- **Leaderboard:** $57k PnL / $1.5M vol.
- **Observed:** ~100 temp trades/day, median entry **1.00**, **No-heavy (476/24)** with a lot of **SELL (349)**, across a **wide, less-crowded city set**: Taipei, Cape Town, Miami, Wellington, Jeddah, Karachi, Warsaw, Tel Aviv.
- **Read:** No-favorite harvesting like HondaCivic, but spread across **newer/exotic markets** rather than the deep majors — chasing softer pricing where fewer bots compete.
- **Helps us:** a **city-expansion map**. Cape Town, Wellington, Jeddah, Karachi, Tel Aviv are markets our station registry may not cover yet; if the resolution source is auditable (`resolution_audit.py`), these are less-contested books to extend into.

### 6. Hans323 — `0x0f37cb80dee49d55b5f6d9e595d52591d6371410`  ·  X: @Hans323
- **Leaderboard:** $81k PnL / $7.2M vol.
- **Observed:** ~162 temp trades/day, median entry **0.96**, **No-heavy (334/107)**, mix of BUY/SELL, cities **NYC (354), London, Paris, Shenzhen**. Small median ticket (~$17).
- **Read:** a steady **buy-near-certain favorites** grinder on the deep books — low edge per trade, high count, low variance. The "boring but green" archetype.
- **Helps us:** the low-variance counterweight to ShyGuy1's longshot stacking. Useful as a model for a **safe sub-allocation**: a slice of bankroll on high-confidence near-1.0 buckets our forecast strongly agrees with, to stabilize the equity curve.

### 7. onlylucknobrain — `0x6a8d1709bfb718d8555d315a983c4816278350f9`  (added 2026-06-08)
- **Leaderboard:** $22.5k PnL / $1.7M vol. 30d +$4.0k — strong recent form on modest size.
- **Observed (last 471 trades):** 100% temperature, **100% day-ahead** (66% at +1 day, 34% at +2 days; **never same-day**). Mid-range entries — 185 at 0.05–0.2, 141 at 0.2–0.5, 90 at 0.5–0.8 (i.e. the *live, uncertain* buckets, not the tails). **Tiny tickets: median $2, max $46.** **39 cities, global** (Toronto, Milan, Panama, Singapore, Madrid, Tel Aviv, SF…). Yes-heavy (349/122), occasional SELL. −$5.1k open.
- **Read:** essentially **our strategy, done broader and smaller** — pure day-ahead forecast betting on underpriced live buckets, with radical diversification (hundreds of tiny bets across 39 cities) to crush single-market variance. No nowcast, no tail-harvesting.
- **Helps us:** the template for **breadth + small size**. Validates expanding `config/cities.yaml` globally and capping per-trade size hard (our `MAX_STAKE_FRACTION`) — diversification, not conviction, smooths the curve. Directly counters the single-bet blowup pattern we hit (one oversized No that tanked realized P&L).

---

## What we should actually take from this

1. **Expand the city universe toward liquidity.** Every top bot is heavy in **NYC + London** (ColdMath, Hans323) — far deeper books than our Asian-centric set. Add them (and audit their resolution stations) so corr-Kelly has real depth to size into.
2. **Two distinct, profitable lanes exist** — *cheap-Yes longshots day-before* (automatedAItradingbot, ShyGuy1) and *No-favorite harvesting* (HondaCivic, HighTempTation, Hans323). We currently lean on the first; a calibrated, day-before version of the second is a cleaner edge than their near-resolution penny-scalping.
3. **Variance discipline is the differentiator.** ShyGuy1 (-$24k open) and HondaCivic (-$5.6k open) are top earners *carrying large drawdowns* from independent longshot stacking — precisely what our correlation-aware Kelly + cash buffer + per-day cap are designed to avoid. Sizing, not forecast, is where they're beatable.
4. **`automatedAItradingbot` is our benchmark.** Same markets, same ~14h horizon, currently green. Wire a periodic compare of its entries against our model probabilities as a live calibration signal.
5. **`HighTempTation` is a scouting list** for less-contested markets (Cape Town, Wellington, Jeddah, Karachi, Tel Aviv).

## Excluded (looked promising, weren't useful)
- **gopfan2** (`0xf2f6…5817`) and **aenews2** (`0x44c1…ebc1`) — top of the all-time weather PnL board, but **zero temperature trades in their last 500 activities**; their weather profit is stale and they've rotated into other categories. Not a current weather strategy to copy.
- **WeatherTraderBot** (`0xacc8…7d08`) — despite the name, **~$167 total volume**, effectively a dormant test wallet.

## Reproduce
```bash
# leaderboard of weather traders (names + X handles)
curl -s "https://data-api.polymarket.com/v1/leaderboard?category=WEATHER&orderBy=PNL&timePeriod=ALL&limit=25"
# deep-dive any wallet
python scripts/analyze_trader.py 0x594edb9112f526fa6a80b8f858a6379c8a2c1c11
```
