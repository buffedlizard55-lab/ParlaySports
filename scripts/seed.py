"""One-time seed: snapshots -> database -> ratings -> backtests -> export.

Deterministic given data/seed/**. Verifies the seed MANIFEST (sha256) before
importing so a corrupted snapshot can never silently poison the build.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from parlaysports import books, engine, export, ingest, quality, ratings, sources, store
from parlaysports.config import DB_PATH, SEED_DIR
from parlaysports.strategies import CATALOG_BY_ID, MULTI_SOURCES, install_catalog
from parlaysports.util import utcnow_iso

RETRIEVED_SEED = "2026-09-22T01:30:00Z"


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_manifest() -> dict:
    man_path = SEED_DIR / "MANIFEST.json"
    man = json.loads(man_path.read_text())
    problems = []
    for f in man["files"]:
        p = SEED_DIR / f["path"]
        if not p.exists():
            problems.append(f"missing {f['path']}")
            continue
        if sha256_file(p) != f["sha256"]:
            problems.append(f"hash mismatch {f['path']}")
    if problems:
        raise SystemExit(f"SEED MANIFEST FAILED:\n  - " + "\n  - ".join(problems))
    man_sha = sha256_file(man_path)
    return {"files": len(man["files"]), "manifest_sha256": man_sha}


def compute_sigma(con: sqlite3.Connection, sport: str, seasons: list[str]) -> float:
    import statistics
    rows = con.execute(
        f"""SELECT away_score + home_score AS t FROM games WHERE sport=?
            AND status='final' AND season IN ({','.join('?' for _ in seasons)})""",
        (sport, *seasons)).fetchall()
    vals = [r["t"] for r in rows]
    if len(vals) < 30:
        raise SystemExit(f"insufficient totals data for {sport} sigma ({len(vals)})")
    return round(statistics.pstdev(vals), 3)


def compute_park_factors(con: sqlite3.Connection) -> dict[str, float]:
    """Home-team park proxy from 2015-2025: runs in home games / runs in road games."""
    out: dict[str, float] = {}
    teams = con.execute(
        "SELECT DISTINCT home_team AS t FROM games WHERE sport='MLB'").fetchall()
    for r in teams:
        t = r["t"]
        home = con.execute(
            """SELECT AVG(away_score + home_score) AS a, COUNT(*) AS n FROM games
               WHERE sport='MLB' AND home_team=? AND status='final'""", (t,)).fetchone()
        road = con.execute(
            """SELECT AVG(away_score + home_score) AS a, COUNT(*) AS n FROM games
               WHERE sport='MLB' AND away_team=? AND status='final'""", (t,)).fetchone()
        if home and road and (home["n"] or 0) >= 100 and (road["n"] or 0) >= 100 \
                and (road["a"] or 0) > 0:
            pf = round(float(home["a"]) / float(road["a"]), 3)
            out[t] = min(max(pf, 0.85), 1.15)  # regress extremes (documented)
            store.set_meta(con, f"PARK_{t}", str(out[t]))
    return out


RESEARCH_SEED = [
    {"research_id": "R-001", "sport": "NFL",
     "title": "nflverse lines match the DraftKings feed exactly (MNF sample)",
     "hypothesis": "nflverse consensus lines equal book lines.",
     "method": "Hand-compared 2026_02_NYG_LA row vs ESPN scoreboard DK odds block 2026-09-22.",
     "data_window": "2026-09-21 NYG@LA",
     "result": "MATCH on spread (-6.5), total (47.5), ML (-298/+240) and split odds. nflverse adopted as VERIFIED_PRIMARY for NFL prices.",
     "status": "tested", "ref_strategy": "S-NFL-01"},
    {"research_id": "R-002", "sport": "NFL",
     "title": "Kalshi KXNFLGAME corroborates Week 3 moneylines within cents",
     "hypothesis": "Exchange winner prices track book MLs.",
     "method": "Compared 4 Week-3 games (PHI@CHI, LA@DEN, BAL@DAL, LV@NO) Kalshi yes_ask vs nflverse ML, 2026-09-22.",
     "data_window": "2026-09-27/28",
     "result": "All within ~3-20 cents of implied prob (e.g. PHI 0.64 vs -180). Kalshi adopted as live cross-check feed.",
     "status": "tested", "ref_strategy": "S-NFL-01"},
    {"research_id": "R-003", "sport": "MLB",
     "title": "No free historical MLB odds feed found; model-pricing adopted",
     "hypothesis": "A free historical MLB odds archive exists.",
     "method": "Surveyed sibling repos (MLBComp, MLB-PBP, StatcastMLB), Retrosheet (scores only), SBR (no free archive).",
     "data_window": "2015-2026",
     "result": "REJECTED for v1: MLB legs are MODEL-grade (log5 fair prices) until the nightly collector accumulates Kalshi/ESPN-DK quotes.",
     "status": "tested", "ref_strategy": "S-MLB-01"},
    {"research_id": "R-004", "sport": "MLB",
     "title": "2026 standings snapshot verifies playoff races for motivation signals",
     "hypothesis": "Race/eliminated status is knowable pre-game for the final week.",
     "method": "Pulled official StatsAPI standings 2026-09-22 (all 30 clubs, W/L, streaks, splits, xW).",
     "data_window": "2026 season through 2026-09-21",
     "result": "Confirmed: TB/NYY/BOS/MIL/LAD/ATL clinched; TEX/CLE lead weak divisions; 12 teams eliminated. S-MLB-06 armed.",
     "status": "tested", "ref_strategy": "S-MLB-06"},
    {"research_id": "R-005", "sport": "NHL",
     "title": "Preseason excluded from forward betting (roster chaos)",
     "hypothesis": "Preseason results are predictable enough to price.",
     "method": "Reviewed 2026-27 preseason slate (split squads, Kraft Hockeyville neutral games) and Kalshi preseason coverage.",
     "data_window": "2026-09-19..26",
     "result": "REJECTED: preseason games are tracked for scores only; forward books open on 2026-09-29 regular season.",
     "status": "tested", "ref_strategy": "S-NHL-01"},
    {"research_id": "R-006", "sport": "NHL",
     "title": "Kalshi historical tier supplies real backtest prices (GAME/SPREAD/TOTAL)",
     "hypothesis": "Exchange price history exists for settled NHL games.",
     "method": "Inherited NHLComp market_price_points (64,922 rows) keyed by game; close-point extraction documented.",
     "data_window": "2024-25, 2025-26",
     "result": "CONFIRMED for 1,580 games (GAME). Backtests price from closes <= puck drop; NO-side legs excluded (no NO offer published).",
     "status": "tested", "ref_strategy": "S-NHL-01"},
    {"research_id": "R-007", "sport": "NBA",
     "title": "SBR archive covers Oct-Dec only; backtests are partial-season",
     "hypothesis": "Full-season free historical NBA odds exist.",
     "method": "Audited NBAComp hist_odds: 4,043 rows, Oct-Dec slices, 2013-2022, cross_checked=0.",
     "data_window": "2013-14..2022-23 (Oct-Dec)",
     "result": "PARTIAL: usable with REFERENCE grade cap + warmup-season exclusion. Full-season replay needs a second source.",
     "status": "tested", "ref_strategy": "S-NBA-01"},
    {"research_id": "R-008", "sport": "NBA",
     "title": "2026-27 openers already lined (ESPN/DK + Kalshi)",
     "hypothesis": "Opening-night markets are quotable a month out.",
     "method": "NBAComp snapshots: 672 ESPN/DK forward lines + 59 Kalshi markets/candles for 2026-10-20/21.",
     "data_window": "2026-10-03 preseason .. 2026-10-21",
     "result": "CONFIRMED, then corrected in PASS-3. Opening-night markets are quotable a month out, but the forward book must NOT arm on preseason lines: the inherited log carries no game_type column and every row used to be imported as REG, so exhibitions were indistinguishable from regular season. ingest.NBA_REG_OPENERS/NBA_REG_ENDS now derive PRE/REG/POST from verified season boundaries (evidence string stored per row) and run_forward admits NBA REG/POST only, so preseason games are excluded by data, not by convention.",
     "status": "tested", "ref_strategy": "S-NBA-01"},
    {"research_id": "R-009", "sport": "MULTI",
     "title": "Cross-sport backtest overlap engine deferred to v2",
     "hypothesis": "Single-day cross-sport backtest is feasible in v1 scope.",
     "method": "Scoped the join across four calendars + four price histories with one decision clock.",
     "data_window": "n/a",
     "result": "DEFERRED honestly: MULTI strategies are forward-only; component signal hit-rates shown as reference. Superseded by R-017: the overlap engine shipped in v1.1.",
     "status": "superseded", "ref_strategy": "S-MULTI-01"},
    {"research_id": "R-010", "sport": "NFL",
     "title": "Weather fields are sparse; WindChill fires only on observed data",
     "hypothesis": "Wind/temp coverage is complete for outdoor games.",
     "method": "Audited nflverse temp/wind population across 1999-2026.",
     "data_window": "1999-2026",
     "result": "Coverage is partial (modern seasons best). Rule: missing weather => no signal, never an assumption.",
     "status": "tested", "ref_strategy": "S-NFL-05"},
    {"research_id": "R-011", "sport": "NHL",
     "title": "2026-27 schedule row count (1344) is CORRECT: first 84-game season since 1993-94",
     "hypothesis": "Imported 2026-27 REG schedule has exactly 1312 games (82/team).",
     "method": "PASS-3: counted game_type=REG rows for season 20262027 in the snapshot (1344) and checked the number against public league/press sources instead of assuming an 82-game season: AP News 2026-27 schedule report, TSN, and the Wikipedia 2026-27 NHL season article (evidence pinned in data/seed/crosscheck/checks_20260922_pass3.json).",
     "data_window": "2026-27",
     "result": "HYPOTHESIS REJECTED, ANOMALY RESOLVED: the new CBA made 2026-27 an 84-game regular season -- the first since 1993-94 -- so 32 x 84 / 2 = 1344 games is exactly right (opening 2026-09-29, ending 2027-04-10; preseason cut to 4 games/team, 2026-09-19..26, 65 games, which the snapshot also holds). The imported rows are correct and were never trimmed. config.SEASON_LENGTH now carries the 84-game override for season 20262027 and the schedule-completeness check measures per-team counts against that verified length, so the false alarm cannot come back.",
     "status": "tested", "ref_strategy": "S-NHL-01"},
    {"research_id": "R-012", "sport": "MLB",
     "title": "2026 game-level log missing; standings snapshot bridges the gap",
     "hypothesis": "Full 2026 game log is obtainable in the seed window.",
     "method": "Sandbox has no StatsAPI access; schedule endpoint too large for the fetch channel.",
     "data_window": "2026-03..09",
     "result": "Gap documented: nightly collector backfills Mar-Sep 2026 game logs; until then 2026 MLB uses the verified standings snapshot + form splits.",
     "status": "hypothesis", "ref_strategy": "S-MLB-01"},
    {"research_id": "R-013", "sport": "MLB",
     "title": "PASS-2 audit: inherited MLB results cover 2015 (Apr-Jul) and 2023-2025 only",
     "hypothesis": "results_2015_2025.csv covers every season 2015-2025 (README v1 claim).",
     "method": "Row-counted the pinned seed file by season during the second-pass audit (8,619 rows).",
     "data_window": "2015-2025",
     "result": "REJECTED the claim: 2015 is Apr-Jul only (1,330 games), 2016-2022 hold zero rows, 2023-2025 are full. MLB backtests are confined to 2023-2025; the quality checker now flags the gap as missing-historical-periods and nothing is padded. Nightly backfill will restore 2016-2022 when StatsAPI access allows.",
     "status": "tested", "ref_strategy": "S-MLB-01"},
    {"research_id": "R-014", "sport": "MULTI",
     "title": "Player props and game props have no verified free historical feed",
     "hypothesis": "A free public source carries verifiable historical player-prop odds/results.",
     "method": "Surveyed nflverse (game-level only), StatsAPI, ESPN scoreboard blocks (game markets only), Kalshi series (winner/limited spreads), SBR archives (game markets).",
     "data_window": "n/a",
     "result": "NOT AVAILABLE for v1: supported leg markets are ML, spread/run/puck line, totals and team-derived markets that settle from official final scores. Player props would require player-level odds + availability data we cannot verify freely; they are excluded rather than simulated.",
     "status": "tested", "ref_strategy": "S-MULTI-02"},
    {"research_id": "R-015", "sport": "NFL",
     "title": "Forward tickets need a whole-date cutoff when start times are missing",
     "hypothesis": "Forward generation cannot bet a game that already kicked off.",
     "method": "PASS-2 review of nflverse rows: gameday is local-date, start_utc is not recorded. A 2026-09-22T01:30Z decision stamp could otherwise attach to the 2026-09-21 MNF after kickoff.",
     "data_window": "2026-25 forward runs",
     "result": "FIX: run_forward now skips games whose gameday is strictly before the decision date when start_utc is unknown (conservative: whole slate closes at UTC date rollover). backtest-leakage quality check enforces it on every run.",
     "status": "tested", "ref_strategy": "S-NFL-01"},
    {"research_id": "R-016", "sport": "MULTI",
     "title": "Injuries and availability: no point-in-time verified feed wired in v1",
     "hypothesis": "Starting lineups / injuries can be consumed as verifiable, timestamped inputs.",
     "method": "Reviewed official feeds (MLB probables on StatsAPI schedule; nflverse QB columns) and the MasterSite directory (NBAInjuryReport sibling exists as a signal source).",
     "data_window": "n/a",
     "result": "PARTIAL: nflverse QB names and MLB probable pitchers exist in seed inputs and are recorded on games (extra_json); a full point-in-time injury feed is NOT integrated, so no strategy conditions on injuries yet. MasterSite's NBAInjuryReport is catalogued as a candidate signal for a future version. Missing availability is treated like missing weather: no signal, never an assumption.",
     "status": "hypothesis", "ref_strategy": "S-NBA-02"},
    {"research_id": "R-017", "sport": "MULTI",
     "title": "Cross-sport backtest overlap engine (v1.1): window intersections are real and tradeable",
     "hypothesis": "A single-decision-clock cross-sport backtest is feasible over the existing verified per-sport windows without any new data source.",
     "method": "Computed pairwise intersections of BACKTEST_WINDOWS over final slates; pooled the MULTI source generators per slate at T12:00Z with close-only prices; reused the forward admission gate and builder unchanged; lottery weekly cap enforced per ISO week of the slate. The result line is regenerated from the actual build output on every seed run (update_r017_result) so the log can never drift from the record.",
     "data_window": "window intersections: NFL∩NBA Oct-Dec 2014-2022, NFL∩MLB Sep 2023-Sep 2025, NFL∩NHL Oct 2025-Jan 2026",
     "result": "computed at build time (update_r017_result)",
     "status": "tested", "ref_strategy": "S-MULTI-01"},
    {"research_id": "R-018", "sport": "MULTI",
     "title": "PASS-3 review: engine violated two documented MULTI rules; both fixed in v1.1",
     "hypothesis": "The parlay builder honors every versioned catalog construction rule.",
     "method": "Line-by-line review of parlay.build_parlays vs catalog text + live ticket inspection on the pinned seed (2026-09-22).",
     "data_window": "seed exports + pre-fix dry run on the pinned seed",
     "result": "REJECTED the hypothesis, then fixed: (1) S-MULTI-01's '1 leg per sport max' was unenforced -- all 3 pre-fix forward tickets and 214 of 227 dry-run backtest tickets carried up to 3 legs of one sport; the builder now takes a machine-readable max_per_sport cap from the catalog. (2) The lottery 'max 1 ticket per week' relied on a one-off note stamp applied after seeding; run_forward now enforces the ISO-week cap and stamps every ticket at creation, so nightly runs cannot double-file a week. Catalog rule text unchanged (still v1); the engine was brought into compliance.",
     "status": "tested", "ref_strategy": "S-MULTI-01"},
    {"research_id": "R-019", "sport": "NBA",
     "title": "PASS-3: NBA game_type was hardcoded REG; boundaries now derived from verified openers/ends",
     "hypothesis": "Every NBA row in the inherited log is a regular-season game.",
     "method": "Inspected NBAComp games.csv columns (game_id, season, game_date_et, tipoff_utc, away_team, home_team, away_score, home_score, status, source -- no type column), then counted per-team rows by season (max 110 for SAS, i.e. 28 games above an 82-game season), then derived PRE/POST boundaries and verified them against public schedule pages: opening nights 2023-10-24, 2024-10-22, 2025-10-21, 2026-10-20 and last full 30-team slates 2024-04-14, 2025-04-13, 2026-04-12 (nba.com key dates, ESPN/FOX/CBS schedule coverage, NBC New York; evidence pinned in checks_20260922_pass3.json). The log's own daily counts corroborate every boundary (exhibition cluster, gap, then two/three openers; final 15-game slate, then play-in pairs).",
     "data_window": "2023-24..2026-27 (2013-14..2022-23 slices already start on real openers)",
     "result": "HYPOTHESIS REJECTED, then fixed. Consequences before the fix: exhibitions polluted the Elo trail and could enter the forward book as soon as a line posted, violating the documented preseason exclusion. After the fix each row carries game_type PRE/REG/POST with the derivation evidence in games.verify_note and games.extra_json; per-team REG counts are exactly 82 for 2023-24..2025-26. Limitation kept honest: only boundaries with public evidence are used -- seasons without one keep the inherited REG label and the new schedule-completeness check reports any per-team count above the league length instead of silently relabelling rows.",
     "status": "tested", "ref_strategy": "S-NBA-01"},
    {"research_id": "R-020", "sport": "MULTI",
     "title": "PASS-3: three 'missing games' were verified real-world cancellations, not data gaps",
     "hypothesis": "Seasons whose game counts fall short of teams x league-length have import gaps that must be backfilled.",
     "method": "Measured per-team regular-season counts for every season, then checked each short season against public records: NFL 2022 (271 games; BUF and CIN at 16), MLB 2024 (2429 games; CLE and HOU at 161), NHL 2026-27 (1344 games; 84/team). Sources: ESPN/USA Today on the cancelled Bills@Bengals week-17 game, the Wikipedia 2024 MLB season article and cleveland.com on the rained-out 2024-09-29 HOU@CLE finale, AP News/TSN/Wikipedia on the 84-game 2026-27 NHL season (all pinned in checks_20260922_pass3.json).",
     "data_window": "NFL 2022, MLB 2024, NHL 2026-27",
     "result": "HYPOTHESIS REJECTED for all three: (1) NFL 2022 week 17 BUF@CIN was suspended after Damar Hamlin's cardiac arrest on 2023-01-02, cancelled on 2023-01-05 and declared a no contest -- never replayed, so 271 is the real total; (2) MLB's 2024-09-29 HOU@CLE finale was rained out after a 3h05m delay and not rescheduled because it could not affect playoff qualification, so 2429 is the real total; (3) NHL 2026-27 is an 84-game season (see R-011), so 1344 is the real total. Nothing was padded and nothing was 'fixed'. engine.EXPECTED_HISTORY now records known_short/in_progress/partial_seasons per sport so the schedule-completeness check reports unexplained deviations only, with the verified explanation attached to the three above.",
     "status": "tested", "ref_strategy": "S-NFL-01"},
    {"research_id": "R-021", "sport": "NFL",
     "title": "PASS-3: nightly ESPN matching dropped Monday-night results (UTC vs local gameday)",
     "hypothesis": "The nightly ESPN collector settles every finished game it can see.",
     "method": "Traced _upsert_espn_event on the verified 2026-09-21 MNF fixture: ESPN reports startDate 2026-09-22T00:15Z (UTC), so its date key is 2026-09-22, while nflverse files the local gameday 2026-09-21. The old lookup matched on an exact game_date, found nothing, and inserted a SECOND row keyed NFL:espn:401872947 -- the real result never reached the nflverse row the forward book had bet on.",
     "data_window": "nightly runs, 2026 season",
     "result": "CONFIRMED BUG, fixed: the lookup now tolerates +/-1 day around the ESPN date (preferring an exact league-game-id hit), so evening games that roll over UTC settle onto the existing row instead of duplicating it. Same pass also de-duplicated _price: a snapshot with an identical (game_key, market, selection, source_id, observed stamp) or an identical value inside one night is no longer stacked, which was inflating closing-value statistics and database size. Both behaviours are asserted by tests and by the audit's duplicate/economic-integrity checks.",
     "status": "tested", "ref_strategy": "S-NFL-01"},
    {"research_id": "R-022", "sport": "MULTI",
     "title": "PASS-3: equity curves were plotted in ledger-insertion order, not economic time",
     "hypothesis": "bankroll.current_amount and the published equity curve / max drawdown describe the same path.",
     "method": "Walked the ledger for every book: a replayed backtest writes all stakes first and settles later, so balance_after follows INSERT order. Recomputed each curve by summing amounts in (entry_ts, entry_id) order and compared the endpoint with bankroll.current_amount, then measured the drawdown both ways.",
     "data_window": "all books, backtest + forward",
     "result": "HYPOTHESIS REJECTED as published, fixed in the record: insertion-order curves showed impossible troughs (drawdowns above 100%, e.g. S-MLB-01 at 129.1%), because every stake was debited before any payout. books.summary now recomputes the curve in economic time, reports min_balance and ledger_drift alongside max_drawdown, and settle_all stamps each ledger row with the economic entry_ts. The new bankroll-integrity quality check independently re-walks every book and fails the run on a negative balance, a >100% drawdown, or any drift between the recomputed endpoint and current_amount -- so the check is order-independent and cannot be satisfied by re-sorting rows.",
     "status": "tested", "ref_strategy": "S-MLB-01"},
    {"research_id": "R-023", "sport": "NBA",
     "title": "PASS-3: the inherited log reports postponed games as 0-0 'finals'",
     "hypothesis": "Every row the inherited NBA log marks final carries a real score.",
     "method": "The new schedule-completeness check reported 5, 9 and 10 NBA teams above the 82-game regular season in 2023-24, 2024-25 and 2025-26. Tracing the extra games found 12 rows with away_score=0 and home_score=0 marked final -- an impossible basketball scoreline. Each was checked against public records (NBA.com and NBA Communications releases, ESPN's own game page for id 401585204, Mercury News/Deseret News, the LA Times, Forbes and the Wikipedia 2025-26 season article; all pinned in checks_20260922_pass3.json) and against the log itself, which contains the makeups as separate rows with real scores (e.g. 2024-02-15 GSW 140 UTA 137 and 2024-04-02 DAL 100 GSW 104 for the two January 2024 Warriors postponements).",
     "data_window": "2023-24..2025-26 (12 rows), plus one NHL preseason row captured mid-game",
     "result": "HYPOTHESIS REJECTED, defect fixed at ingest: 9 of the 12 are documented postponements -- Warriors@Jazz and Mavericks@Warriors (Jan 2024, after assistant coach Dejan Milojevic's death), Hornets@Lakers, Spurs@Lakers and Hornets@Clippers (Jan 2025 LA wildfires), Heat@Bulls (Jan 2026, moisture on the floor), Warriors@Timberwolves (Jan 2026, Minneapolis safety grounds), Nuggets@Grizzlies and Mavericks@Bucks (Jan 2026 winter storm). Those rows are recorded as status='postponed' with scores NULL, verified=1 and the citation in verify_note. The other three (HOU@ATL 2025-01-11, MIL@NOP 2025-01-22, NOP@ORL 2024-10-11 preseason) could not be matched to a published notice in this pass: they are recorded the same way with verified=0 and an explicit 'reason NOT independently verified' note -- flagged, never guessed. Every case files an impossible-scoreline issue, settle_leg now voids a leg on a postponed game (stake refunded, standard book rule) instead of leaving it pending forever, and _invalid_stats reports any 0-0 final that reaches the table again. Consequences before the fix: GSW was rated on a 0-0 'result', three seasons looked over-length, and any bet on those fixtures could have settled on an invented score.",
     "status": "tested", "ref_strategy": "S-NBA-01"},
    {"research_id": "R-024", "sport": "MULTI",
     "title": "PASS-3: two league rules the record had to respect (NBA Cup final, live snapshots)",
     "hypothesis": "A game's presence in the regular-season log means it counts toward the season record, and any score in a snapshot is a result.",
     "method": "Checked the NBA Cup rules against ESPN's Cup explainer, Sportico, Yahoo Sports and The Oklahoman, then against the log: the championship rows are 2023-12-09 IND@LAL, 2024-12-17 MIL@OKC and 2025-12-16 SAS@NYK and in each season exactly the two finalists carry 83 games. Separately inspected the NHL snapshot's state column and found a preseason fixture (NHL:2026010017 DET@CBJ 2026-09-21) captured as state=LIVE with 0-0, which the importer had turned into a final because it inferred status from score presence alone.",
     "data_window": "NBA 2023-24..2025-26, NHL 2026-27 preseason",
     "result": "HYPOTHESIS REJECTED on both counts, and both are now encoded: (1) every NBA Cup game counts toward the 82 EXCEPT the championship, so ingest.NBA_CUP_FINALS flags those rows (extra_json.nba_cup_championship + citation) and the completeness check subtracts them -- the finalists' 83rd game is real, played and rated, just not part of the 82. (2) ingest_nhl now trusts the upstream state before inferring anything from scores: LIVE stays live (never a result, never rated, never bettable -- the forward book admits status='scheduled' only), PRE/FUT stay scheduled, and a FINAL with 0-0 is recorded as postponed. _missing_scores was widened to report past-dated games still live or scheduled with no result.",
     "status": "tested", "ref_strategy": "S-NHL-01"},
]


def install_research(con: sqlite3.Connection) -> int:
    n = 0
    for r in RESEARCH_SEED:
        con.execute(
            """INSERT OR REPLACE INTO research(research_id, created_utc, sport, title,
               hypothesis, method, data_window, result, status, ref_strategy)
               VALUES (:research_id, '2026-09-22T02:00:00Z', :sport, :title, :hypothesis,
               :method, :data_window, :result, :status, :ref_strategy)""", r)
        n += 1
    return n


def install_users(con: sqlite3.Connection) -> int:
    """Persist the 28 competitor accounts (one per strategy manager)."""
    n = 0
    for s in CATALOG_BY_ID.values():
        con.execute(
            """INSERT OR REPLACE INTO users(username, role, strategy_id, created_utc, note)
               VALUES (?, 'competitor', ?, '2026-09-22T02:00:00Z', ?)""",
            (s["username"], s["strategy_id"],
             f"paper competitor managing {s['name']} ({s['sport']})"))
        n += 1
    return n


PASS2_VERIFICATIONS = [
    {"subject": "MLB 2023-08-19 MIA@LAD doubleheader (games 716909 & 716926)",
     "claim": "Two MIA@LAD finals on one date with identical 1-3 scores are two real "
              "games (Hurricane Hilary makeup DH), not a duplicated row.",
     "source_url": "https://www.baseball-reference.com/boxes/LAN/LAN202308192.shtml",
     "result": "match",
     "detail": "MLB.com recap confirms Game 1 was Dodgers 3-1; baseball-reference "
               "box LAN202308192 confirms Game 2 also Dodgers 3-1. Both StatsAPI "
               "game_pks exist. The duplicate-events quality check now exempts MLB "
               "rows with distinct league game ids; identical scores alone are no "
               "longer treated as a data error for baseball."},
]


def install_verifications(con: sqlite3.Connection) -> int:
    from parlaysports.store import add_verification
    n = 0
    for v in PASS2_VERIFICATIONS:
        add_verification(con, **v)
        n += 1
    return n


def update_r017_result(con: sqlite3.Connection) -> str:
    """Regenerate R-017's result line from the ACTUAL database record.

    The research log must never carry hand-typed numbers that can drift from
    the record: overlap counts and per-strategy ticket totals are measured
    from the database itself, so the line stays true whether it is refreshed
    at seed time or by a nightly convergence run.
    """
    from itertools import combinations

    from parlaysports.engine import BACKTEST_WINDOWS, slate_dates
    sport_dates = {sp: set(slate_dates(con, sp, w["seasons"], w["game_types"], "final"))
                   for sp, w in BACKTEST_WINDOWS.items()}
    pairs = []
    for a, b in combinations(sorted(sport_dates), 2):
        inter = sport_dates[a] & sport_dates[b]
        if inter:
            pairs.append(f"{a}&{b}: {len(inter)} slates ({min(inter)}..{max(inter)})")
    all_dates = sorted(set().union(*sport_dates.values())) if sport_dates else []
    n_multi = sum(1 for d in all_dates
                  if sum(d in s for s in sport_dates.values()) >= 2)
    tickets = ", ".join(
        f"{r['strategy_id']}: {r['n']} tickets / {r['legs']} legs over {r['slates']} slates"
        for r in con.execute(
            """SELECT strategy_id, COUNT(*) AS n, SUM(n_legs) AS legs,
                      COUNT(DISTINCT slate_date) AS slates
               FROM parlays WHERE test_mode='backtest' AND sport_scope='MULTI'
               GROUP BY strategy_id ORDER BY strategy_id""")) or "none"
    result = (
        f"CONFIRMED on the pinned seed: {n_multi} candidate slates where >=2 "
        f"per-sport backtest windows overlap ({'; '.join(pairs)}). Tickets on "
        f"record -- {tickets}. One decision clock per slate (T12:00Z), "
        f"closing prices only, the forward admission gate reused unchanged, and "
        f"the lottery weekly cap enforced per ISO week. Leg independence is "
        f"still assumed (no correlation model); overlap slates are computed "
        f"from verified finals only, never padded.")
    con.execute("UPDATE research SET result=? WHERE research_id='R-017'", (result,))
    return result


def main() -> dict:
    print("== ParlaySports seed ==", flush=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
        print(f"removed {DB_PATH}", flush=True)
    man = verify_manifest()
    print(f"manifest OK: {man['files']} files sha={man['manifest_sha256'][:12]}", flush=True)
    con = store.connect()
    out: dict = {"manifest": man}
    out["sources"] = sources.install_registry(con)
    out["nfl"] = ingest.ingest_nfl(con, retrieved_utc=RETRIEVED_SEED)
    print(f"NFL: {out['nfl']['games']} games, {out['nfl']['prices']} prices", flush=True)
    out["mlb_results"] = ingest.ingest_mlb_results(con, retrieved_utc=RETRIEVED_SEED)
    out["mlb_standings"] = ingest.ingest_mlb_standings_snapshot(con)
    out["mlb_fixtures"] = ingest.ingest_mlb_fixtures(con)
    print(f"MLB: {out['mlb_results']['games']} finals, "
          f"{out['mlb_standings']['teams']} teams, {out['mlb_fixtures']['fixtures']} fixtures",
          flush=True)
    out["nhl"] = ingest.ingest_nhl(con, retrieved_utc=RETRIEVED_SEED)
    print(f"NHL: {out['nhl']['games']} games, {out['nhl']['kalshi_prices']} kalshi prices",
          flush=True)
    out["nba"] = ingest.ingest_nba(con, retrieved_utc=RETRIEVED_SEED)
    print(f"NBA: {out['nba']['games']} games, {out['nba']['sbr_prices']} sbr prices",
          flush=True)
    out["crosscheck"] = ingest.ingest_crosscheck(con)
    print(f"crosscheck: {out['crosscheck']['checks']} verified checks from "
          f"{out['crosscheck']['files']} files", flush=True)
    # Hand-verified finals pinned during PASS-3 (see data/seed/updates). Applied
    # BEFORE ratings/backtests so the Elo trail and every book see the verified
    # results, and each row keeps its own source/URL/stamp + verification block.
    out["verified_updates"] = ingest.ingest_verified_updates(con)
    print(f"verified updates: {out['verified_updates']}", flush=True)
    con.commit()

    for sport in ("NFL", "MLB", "NHL", "NBA"):
        r = ratings.compute_elo(con, sport)
        print(f"Elo {sport}: {r['games_rated']} games, {r['teams']} teams", flush=True)
    out["elo"] = "ok"

    # Totals sigmas from WARMUP seasons only (never backtest seasons: no leakage).
    sigmas = {
        "NFL": compute_sigma(con, "NFL", [str(y) for y in range(1999, 2010)]),
        "MLB": compute_sigma(con, "MLB", ["2015"]),
        "NHL": compute_sigma(con, "NHL", ["20242025"]),
        "NBA": compute_sigma(con, "NBA", ["2013-14"]),
    }
    for sport, sig in sigmas.items():
        store.set_meta(con, f"TOTALS_SIGMA_{sport}", str(sig))
    out["sigmas"] = sigmas
    print(f"sigmas: {sigmas}", flush=True)
    out["parks"] = compute_park_factors(con)
    print(f"park factors: {len(out['parks'])} teams", flush=True)

    out["strategies"] = install_catalog(con)
    out["users"] = install_users(con)
    out["research"] = install_research(con)
    out["pass2_verifications"] = install_verifications(con)
    store.set_meta(con, "data_as_of_utc", RETRIEVED_SEED)
    store.set_meta(con, "seed_manifest_sha256", man["manifest_sha256"])
    con.commit()

    # Backtests: single-sport runners first, then the cross-sport overlap
    # engine for the MULTI strategies (R-017) on window-intersection slates.
    out["backtests"] = {}
    for sid in CATALOG_BY_ID:
        if sid in MULTI_SOURCES:
            continue
        r = engine.run_backtest(con, sid)
        out["backtests"][sid] = r
        print(f"backtest {sid}: {r['parlays']} parlays / {r['legs']} legs", flush=True)
    out["backtests_multi"] = engine.run_backtest_multi_all(
        con, [s for s in CATALOG_BY_ID if s in MULTI_SOURCES])
    for sid, r in sorted(out["backtests_multi"].items()):
        print(f"backtest-multi {sid}: {r['parlays']} parlays / {r['legs']} legs "
              f"over {r['slates']} overlap slates", flush=True)
    out["r017"] = update_r017_result(con)
    print(f"R-017 result: {out['r017'][:160]}...", flush=True)
    con.commit()
    out["settle_backtest"] = engine.settle_all(con, test_mode="backtest")
    print(f"settle backtest: {out['settle_backtest']}", flush=True)

    # Forward books: next scheduled slates from the imported fixtures.
    fwd_slates = [r["game_date"] for r in con.execute(
        """SELECT DISTINCT game_date FROM games WHERE status='scheduled'
           AND game_date >= '2026-09-21' AND game_date < '2026-10-15'
           AND ((sport='NFL') OR (sport='MLB' AND game_type='R')
             OR (sport='NHL' AND game_type IN ('REG','POST'))
             OR (sport='NBA' AND game_type IN ('REG','POST')))
           ORDER BY game_date LIMIT 14""").fetchall()]
    out["forward_slates"] = fwd_slates
    out["forward"] = {}
    for sid in CATALOG_BY_ID:
        if sid == "S-MULTI-04":
            continue  # lottery ticket is placed by the nightly job weekly
        r = engine.run_forward(con, sid, fwd_slates, decision_utc=RETRIEVED_SEED)
        out["forward"][sid] = r
        print(f"forward {sid}: {r['parlays']} parlays", flush=True)
    # Lottery tickets at seed time: run_forward itself enforces the documented
    # max-1-per-ISO-week cap and stamps each ticket's note at creation (R-018),
    # so no post-hoc labeling is needed.
    lotto = engine.run_forward(con, "S-MULTI-04", fwd_slates[:7],
                               decision_utc=RETRIEVED_SEED)
    con.commit()
    out["forward"]["S-MULTI-04"] = lotto
    print(f"forward S-MULTI-04: {lotto['parlays']} parlays", flush=True)

    q = quality.run_checks(con)
    store.set_meta(con, "last_quality_run", json.dumps(q))
    out["quality"] = {"total_problems": q["total_problems"]}
    print(f"quality: {q['total_problems']} problems", flush=True)
    out["export"] = export.export_all(con)
    print("export ok", flush=True)
    con.commit()
    con.close()
    print("== seed complete ==", flush=True)
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=1, default=str))
