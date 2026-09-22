# PASS 3 — line-by-line verification against the original brief

PASS 1 built the platform, PASS 2 audited it mechanically. PASS 3 re-read the brief
line by line and checked every claim the running system makes — against the code, the
database and, wherever the brief demands real public data, against independent public
sources. Where a check contradicted a claim, the claim (or the data) was corrected and
the correction is recorded below and in the research log (`data/site/research.json`).

Everything in this document was executed on the committed state; nothing is projected.

## Method

1. Re-read each brief line and map it to a concrete artifact, a quality check or an
   audit check.
2. For every league/season fact the engine depends on (season length, opening nights,
   regular-season end dates, cancellations), fetch an independent public source and pin
   the evidence in `data/seed/crosscheck/checks_20260922_pass3.json` (SHA-256 pinned in
   `data/seed/MANIFEST.json`).
3. For every historical result the engine could settle a bet on, look for physical
   impossibilities (0-0 finals, above-league season lengths) and resolve each one to a
   documented real-world event — never to a guess.
4. Re-derive every published number (bankroll curves, drawdowns, hit rates, standings)
   from the ledger in economic time and fail the run on any drift.
5. Reproduce the production pipeline offline (`scripts/sim_nightly_offline.py`) because
   the Actions nightly job was failing at its Audit gate and its logs are not readable
   from the sandbox.

## Findings and corrections

### 1. NHL 2026-27 is an 84-game season — the "anomaly" was correct data (R-011 → resolved)

The imported 2026-27 schedule held 1344 regular-season games (84 per team) and the
completeness check flagged it against an assumed 82-game season. Independent sources
(AP News' 2026-27 schedule report, TSN, the Wikipedia 2026-27 NHL season article) confirm
the new CBA made it an 84-game season — the first since 1993-94 — running 2026-09-29 to
2027-04-10, with a shortened four-game preseason (2026-09-19..26). Nothing was trimmed.
`config.SEASON_LENGTH` now carries a per-season override, so the check measures against
the verified length instead of a hardcoded one.

### 2. Three "missing games" are real cancellations, not import gaps (R-020)

* **NFL 2022 — 271 games.** Week 17's BUF@CIN was suspended on 2023-01-02 after Damar
  Hamlin's cardiac arrest, cancelled on 2023-01-05 and declared a no contest; Buffalo and
  Cincinnati finished on 16 games.
* **MLB 2024 — 2429 games.** The 2024-09-29 HOU@CLE finale was rained out after a 3h05m
  delay and never made up because it could not affect qualification; Cleveland and Houston
  finished on 161.
* **NHL 2026-27 — 1344 games** (see finding 1).

`engine.EXPECTED_HISTORY` now records these as `known_short` with the verification, and
`partial_seasons`/`in_progress` windows are declared, so the completeness check reports
unexplained deviations only. Nothing was padded to make a number look tidy.

### 3. NBA `game_type` was hardcoded REG — exhibitions polluted the history (R-019)

The inherited NBA log has no game-type column and every row was imported as a regular
season game, so per-team counts reached 108-110 and preseason exhibitions entered the Elo
trail (and could have entered the forward book as soon as a line posted). Boundaries are
now derived from verified opening nights and last full slates (2023-10-24, 2024-10-22,
2025-10-21, 2026-10-20; last full 30-team slates 2024-04-14, 2025-04-13, 2026-04-12 —
nba.com key dates, ESPN/FOX/CBS/AP coverage) with the citation stored per row in
`games.verify_note`/`games.extra_json`. Result: **exactly 82 regular-season games per team
for 2023-24, 2024-25 and 2025-26** (1230 games per season), and the forward book admits
NBA REG/POST rows only.

### 4. Twelve NBA "finals" were postponed games recorded as 0-0 (R-023)

A 0-0 final is impossible in basketball. All twelve rows were traced to documented
postponements:

| fixture | date | verified reason |
| --- | --- | --- |
| GSW@UTA, DAL@GSW | 2024-01-17, 2024-01-19 | after the death of Warriors assistant Dejan Milojevic (NBA.com, Mercury News, Deseret News) |
| CHA@LAL, SAS@LAL, CHA@LAC | 2025-01-09, 2025-01-11 | Los Angeles wildfires (NBA Communications, LA Times) |
| MIA@CHI | 2026-01-08 | moisture on the court, United Center (NBA Communications) |
| GSW@MIN | 2026-01-24 | safety-and-security grounds, Minneapolis (Forbes, NBA.com) |
| DEN@MEM, DAL@MIL | 2026-01-25 | January 2026 North American winter storm (NBA.com, Forbes) |

The log itself corroborates each one: the makeups are present as separate rows with real
scores (e.g. 2024-02-15 GSW 140 UTA 137 and 2024-04-02 DAL 100 GSW 104). Those rows are
now `status='postponed'` with NULL scores, `verified=1` and the citation in the row.

Two further 0-0 rows (HOU@ATL 2025-01-11, MIL@NOP 2025-01-22, plus the 2024-10-11 NOP@ORL
preseason game) could **not** be matched to a published postponement notice in this pass.
They are recorded the same way with `verified=0` and an explicit "reason NOT independently
verified" note: flagged, never guessed. Every case files an issue, `settle_leg` voids a leg
on a postponed game (stake refunded — standard book rule) instead of leaving it pending,
and `_invalid_stats` reports any 0-0 final that reaches the table again.

### 5. The NBA Cup championship is excluded from the 82-game record (R-024)

Every NBA Cup game counts toward the regular season **except** the final, so the two
finalists legitimately play 83 (ESPN's Cup explainer, Sportico, Yahoo Sports). The log
agrees: the championship rows (2023-12-09 IND@LAL, 2024-12-17 MIL@OKC, 2025-12-16 SAS@NYK)
are exactly the extra games. `ingest.NBA_CUP_FINALS` flags those rows with the citation and
the completeness check subtracts them instead of reporting a false over-length season. The
games are real, played and rated — they simply are not part of the 82.

### 6. A live snapshot could be recorded as a result

The NHL snapshot's DET@CBJ preseason fixture (2026-09-21) was captured with `state=LIVE`
and 0-0; the importer inferred "final" from the presence of scores. Ingest now trusts the
upstream state first: LIVE stays live (never a result, never rated, never bettable — the
forward book admits `scheduled` only), PRE/FUT stay scheduled, and a FINAL with 0-0 becomes
a postponed row with an issue. `missing-scores-past-games` was widened to report past-dated
games still live or scheduled with no result.

### 7. Equity curves were plotted in insertion order, not economic time (R-022)

A replayed backtest writes every stake before it settles, so the stored `balance_after`
column walked through an impossible trough: S-MLB-01 showed a 129.1% drawdown and several
books showed negative balances. `books.strategy_books` now re-sums the ledger in
`(entry_ts, entry_id)` order, reports `min_balance` and `ledger_drift` next to
`max_drawdown`, and `settle_all` stamps each ledger row with the economic entry time. The
new `bankroll-integrity` quality check and the `bankroll-economic-integrity` audit check
independently re-walk every book and fail on a negative balance, a drawdown above 100%, or
any drift between the recomputed endpoint and `bankroll.current_amount`. Because the walk
sums amounts in economic order, re-sorting rows cannot make it pass.

### 8. Look-ahead guards (price time and form time)

The brief forbids look-ahead in backtests. Two new checks make the rule mechanical:

* `price-observed-after-decision` — every priced leg must use a price that existed when the
  ticket was decided; a backtest leg may only price off a historical close (`close_flag=1`)
  and never a live snapshot, and an unpriced leg must declare its MODEL/UNPRICED grade in
  the row rather than silently borrowing a market number. The recorded
  `features.price_observed_utc` must match the referenced price row.
* `form-snapshot-leakage` — a leg that reads team form may not use a snapshot stamped after
  the decision.

Verified state: 13,168 priced legs, 0 live-snapshot legs after a decision, 0 backtest legs
priced off a live snapshot, 0 form stamps after the decision.

### 9. Why the nightly job failed, and the fix (R-021)

The Actions nightly run failed at its Audit gate on 2026-09-22 and its exports were not
published. Actions logs are not retrievable from the sandbox, so the pipeline was
reproduced offline (`scripts/sim_nightly_offline.py`, which now drives the *real*
collector functions). Root cause: `_guarded_upsert_game` matched an existing league row on
an exact `game_date`, while ESPN reports a UTC date. A Monday-night kickoff
(2026-09-22T00:15Z) is filed by ESPN under 2026-09-22 but kept by the league log under the
local gameday 2026-09-21, so the event never matched, a duplicate `NFL:espn:401872947` row
was inserted, and the real result never reached the row the forward book had bet on.

The lookup now tolerates ±1 day, keeps the league's own gameday, records why the dates
differ (`verify_note`), and still refuses to overwrite a recorded final. The same pass made
`_price` refuse to stack duplicates (identical stamp, or identical value inside one UTC
night) and log a `conflicting-price-same-stamp` issue when two different values arrive under
one stamp. Both behaviours are covered by tests; the offline simulation now runs the fixed
path end to end and the duplicate-games check passes.

The workflow itself now commits evidence on failure (logs, `audit_report.json`,
`data/site/quality.json`) and uploads them as artifacts, so a red run is diagnosable from
the repository instead of requiring log access, and the audit prints the first failing
checks with detail instead of only a count.

## Verification results (this commit)

```
make test     -> 60/60 unit tests
make uismoke  -> 15/15 routes render
lint          -> workflow YAML lint (a malformed workflow file silently stops the
                 scheduled job, so the audit lints every file in .github/workflows)
make audit    -> 63/63 checks
sim           -> 63/63 via scripts/sim_nightly_offline.py (real collector path)
seed          -> 29,056 games, 60,504 prices, 7,014 parlays, 20,570 legs,
                 8,267 ledger rows, 28 users, 24 research entries, 28 verifications
```

## Residual limitations (honest list)

* Three 0-0 NBA rows remain unverified: recorded as not played with `verified=0` and an
  explicit note. The nightly ESPN pull will settle them once it sees the real games
  (which it can now match across the UTC rollover).
* MODEL-grade legs are estimates at assumed fair prices (-110 / proxy lines). They are
  labeled per leg, and UNPRICED tickets never stake or claim a payout.
* MLB results history is partial (2023-2025 plus Apr-Jul 2015); the gap is reported by
  `missing-historical-periods` on every build and never padded (R-013).
* Player props are not simulated (no free verified historical feed), and no strategy
  conditions on injury data (R-014, R-016).
* Forward tickets use a whole-date cutoff when a game has no recorded start time (R-015).
* Multi-sport legs assume independence; no same-game parlays (R-017).
