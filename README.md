# ParlaySports

**A simulated multi-sport parlay research + paper-trading competition across NFL, MLB, NHL and NBA.**
**No real money. No paid APIs. No fabricated odds, scores or sources.** Every datapoint carries
source + URL + timestamp; anything unverifiable is flagged, never guessed.

Live site: `https://buffedlizard55-lab.github.io/ParlaySports/` (GitHub Pages, refreshed daily).

Coverage plan for the four leagues was seeded from
[MasterSite](https://buffedlizard55-lab.github.io/MasterSite/), then every fact used here was
independently verified against the primary sources below (see `Data Sources` and `Data Quality`
on the site, and `data/seed/crosscheck/checks_20260922.json`).

## How it works

* **28 versioned strategies** (6 per sport + 4 multi-sport), each with a named manager,
  a written hypothesis, selection/construction rules, required data, min edge, stake and
  documented limitations. Rules live in `parlaysports/strategies.py` (`CATALOG`).
* **Two sealed books.** `backtest` = history replayed chronologically with closing prices
  only — multi-sport strategies backtest through the v1.1 cross-sport overlap engine
  (R-017): one decision clock per slate, restricted to slates where ≥2 per-sport
  backtest windows hold verified finals.
  `forward` = paper tickets on upcoming slates. Books are accounted separately and never merged.
* **Pricing grades on every leg/ticket.** `VERIFIED` (direct market quote) →
  `REFERENCE` (aggregated line, book unnamed) → `MODEL` (estimated price, payout approximate) →
  `MIXED` (blend) → `UNPRICED` (no odds: tracked for hit-rate only at **$0 stake**).
* **Immutable settlement.** Parlays settle append-only from final scores
  (push/void legs reduce the ticket; all-push refunds). Settled tickets are never edited.
* **Hash-chained ledger + $10,000 standard bankroll** per strategy per book.
* **Quality system that flags instead of guessing**: missing scores/odds, duplicates,
  conflicting results, invalid stats, timestamp order, stale snapshots, settlement
  recomputation, ledger integrity, parlay math, backtest-leakage guards and missing
  historical periods run on every build (`Data Quality` page). Legitimate MLB
  doubleheaders are hand-verified before being exempted (see the verifications table).

## Quickstart (stdlib only — no installs)

```bash
make seed    # build DB from pinned seeds, backtest, forward, settle, export
make test    # 39 unit tests
make audit   # 54 PASS-2 checks (must be 54/54)
make uismoke # all 15 site routes render against the real exports
make serve   # serve this repo root on 0.0.0.0:8000
```

`make nightly` runs the same collect → forward → settle → audit → export pipeline the
GitHub Action runs daily (needs network for live APIs).

## Strategy catalog (v1)

| ID | Manager | Name | Sport | Style | Markets | Stake |
|---|---|---|---|---|---|---|
| `S-NFL-01` | GridironGuru | Elo edge favorites | NFL | ratings-vs-market | ML | $10 |
| `S-NFL-02` | DivisionalDog | Divisional home dogs | NFL | situational | SPREAD | $10 |
| `S-NFL-03` | Totalician | Efficiency totals | NFL | totals | TOTAL | $10 |
| `S-NFL-04` | RestEdge | Rest mismatch | NFL | situational | SPREAD | $10 |
| `S-NFL-05` | WindChill | Wind chill under | NFL | weather totals | TOTAL | $10 |
| `S-NFL-06` | ShortChalkFade | Short road chalk fade | NFL | contrarian | SPREAD | $10 |
| `S-MLB-01` | Pythagoras | Pythagorean log5 | MLB | ratings-vs-market | ML | $10 |
| `S-MLB-02` | StreakSmarter | Form streaks | MLB | form | ML | $10 |
| `S-MLB-03` | RegressionRandy | xW-Luck fade | MLB | regression | ML | $10 |
| `S-MLB-04` | HomeCookin | Home/road splits | MLB | situational | ML | $10 |
| `S-MLB-05` | CoorsTotal | Park-aware totals | MLB | totals | TOTAL | $10 |
| `S-MLB-06` | ScoreboardWatcher | September motivation | MLB | situational | ML | $10 |
| `S-NHL-01` | PuckElo | Puck Elo edge | NHL | ratings-vs-market | ML | $10 |
| `S-NHL-02` | HomeIceQ | Home ice + rest | NHL | situational | ML | $10 |
| `S-NHL-03` | CreaseCrash | Goals-model totals | NHL | totals | TOTAL | $10 |
| `S-NHL-04` | BackToBacker | B2B fade | NHL | situational | ML | $10 |
| `S-NHL-05` | PucklinePete | Puck-line value | NHL | spread mapping | SPREAD | $10 |
| `S-NHL-06` | MuddyWater | Road dog process | NHL | contrarian | ML | $10 |
| `S-NBA-01` | EloAndOrder | Elo vs close | NBA | ratings-vs-market | ML,SPREAD | $10 |
| `S-NBA-02` | LoadManager | Schedule-spot fade | NBA | situational | SPREAD | $10 |
| `S-NBA-03` | PaceCase | Pace totals | NBA | totals | TOTAL | $10 |
| `S-NBA-04` | HomeCourt | Elite home cover | NBA | situational | SPREAD | $10 |
| `S-NBA-05` | MeanReversion | ATS streak fade | NBA | contrarian | SPREAD | $10 |
| `S-NBA-06` | RoadDawg | Rested road dogs | NBA | contrarian | SPREAD | $10 |
| `S-MULTI-01` | FourSportFavor | Cross-sport favorites | MULTI | multi-sport | ML | $10 |
| `S-MULTI-02` | CrossSportEdge | Best edges any sport | MULTI | multi-sport | ML,SPREAD,TOTAL | $10 |
| `S-MULTI-03` | TotalChaos | Cross-sport totals | MULTI | multi-sport | TOTAL | $10 |
| `S-MULTI-04` | LotteryTicket | Longshot lottery | MULTI | multi-sport | ML,SPREAD,TOTAL | $25 |

## Data sources (all free, all public)

| ID | Source | Status |
|---|---|---|
| `SRC_NFLVERSE_GAMES` | nflverse nfldata: schedules, scores, lines, weather, QBs | VERIFIED_PRIMARY |
| `SRC_MLB_STATSAPI` | MLB Stats API (official): schedule, scores, standings, probables | VERIFIED_PRIMARY |
| `SRC_NHL_API` | NHL official API + partner odds feed | VERIFIED_PRIMARY |
| `SRC_ESPN_SCOREBOARD` | ESPN scoreboard API, all four leagues (+DK odds blocks) | VERIFIED_PRIMARY |
| `SRC_KALSHI` | Kalshi public market API (GAME/SPREAD/TOTAL series) | VERIFIED_PRIMARY |
| `SRC_SBR_NBA` | SportsbookReview NBA odds archive (via NBAComp) | SINGLE_SOURCE |
| `SRC_SIBLING_MLB_RESULTS` | MLB results via sibling tooling (official StatsAPI): full 2023–25, Apr–Jul 2015 | VERIFIED_SECONDARY |
| `SRC_SIBLING_NBACOMP` | NBAComp games 2024–27 + forward lines | VERIFIED_SECONDARY |
| `SRC_SIBLING_NHLCOMP` | NHLComp games 2024–27 + Kalshi closes + DK snapshots | VERIFIED_SECONDARY |
| `SRC_SIBLING_VACSCHED` | VacationSchedule verified 2026 MLB/NFL fixtures | VERIFIED_SECONDARY |
| `SRC_MODEL` | Internal models (Elo, Pythag/log5, totals) — labeled, never facts | MODEL |

Pinned seed inputs + sha256 manifest: `data/seed/` (+ `MANIFEST.json`).
Independent cross-checks: `data/seed/crosscheck/checks_20260922.json`.

## Repo layout

```
index.html / styles.css / app.js   static site (hash-routed, dependency-free)
data/site/*.json                   committed exports (the persisted record)
data/site/history_<SID>.json       complete per-strategy ticket history
data/seed/                         pinned inputs + MANIFEST.json
parlaysports/                      engine: config, store, ingest, ratings,
                                   strategies, parlay, engine, books, quality,
                                   export, sources, util
scripts/seed.py                    cold-start pipeline (PASS 1 build path)
scripts/nightly.py                 live pipeline for GitHub Actions
scripts/audit.py                   PASS 2 mechanical audit (54 checks)
tests/test_platform.py             39 unit tests (stdlib unittest)
scripts/ui_smoke.mjs               headless route render test (node)
```

## Limits (read before trusting any number)

* MODEL-grade payouts are estimates (assumed -110/proxy lines), clearly labeled.
* Backtests assume closing-line availability and ignore line movement, limits and fees.
* Multi-sport legs assume independence (no correlation model; no same-game parlays in v1).
* Kalshi prices exclude exchange fees (noted wherever used).
* S-NHL-05 has no verified puck-line history yet (forward-armed only); S-MLB-06 is
  forward-only by design. MULTI backtests cover only cross-sport overlap slates
  (NFL∩NBA Oct–Dec 2014–22, NFL∩MLB Sep 2023–Sep 2025, NFL∩NHL Oct 2025–Jan 2026 —
  measured, never padded; R-017), and the lottery book keeps its documented
  max-1-ticket-per-ISO-week cap in both books (R-018).
* **MLB results coverage is partial**: the pinned results file holds full 2023–2025
  seasons, Apr–Jul 2015 only, and **nothing for 2016–2022** (R-013). MLB backtests run
  over 2023–2025 only; the `missing-historical-periods` quality check flags the gap on
  every build and nothing is ever padded. NBA backtests use Oct–Dec slices only
  (SBR archive). 2026 MLB game logs are still to be backfilled by the nightly job (R-012).
* Player props / game props are **not** simulated: no free verified historical
  player-prop feed exists (R-014). Injury/availability data is recorded where the feeds
  provide it (nflverse QBs, MLB probables) but no strategy conditions on it yet (R-016).
* Forward tickets use a conservative whole-date cutoff when a game has no recorded
  start time (R-015): better to miss a ticket than to bet a started game.

## Verification

* `tests/test_platform.py`: 39/39 (odds math, settlement incl. push-reduction, guards,
  ledger tamper-evidence, Elo point-in-time + rollover, catalog completeness + version
  immutability, score-correction logging, cover-map push exclusion, history-gap flags,
  cross-sport overlap engine incl. close-only pricing and single-sport-date refusal,
  per-sport leg caps, lottery ISO-week caps in both books, settle-summary counting).
* `scripts/audit.py`: 54/54 (coverage, books separation, settlement + payout,
  ledger↔bankroll consistency, UNPRICED-$0, sources on every row, per-strategy history
  files, upcoming/completed purity, leaderboard columns, users, quality registry, docs,
  MULTI-backtest overlap integrity: tickets exist, every ticket spans ≥2 sports and
  every slate is a real ≥2-sport window overlap).
* `scripts/ui_smoke.mjs`: all 15 site routes render headlessly against the real exports
  (`make uismoke`, also gated in the nightly workflow).
* `data/audit_report.json` is regenerated on every run.
