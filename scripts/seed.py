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
     "result": "CONFIRMED. NBA forward book arms on preseason lines; opener parlays generate once non-preseason lines post.",
     "status": "tested", "ref_strategy": "S-NBA-01"},
    {"research_id": "R-009", "sport": "MULTI",
     "title": "Cross-sport backtest overlap engine deferred to v2",
     "hypothesis": "Single-day cross-sport backtest is feasible in v1 scope.",
     "method": "Scoped the join across four calendars + four price histories with one decision clock.",
     "data_window": "n/a",
     "result": "DEFERRED honestly: MULTI strategies are forward-only; component signal hit-rates shown as reference.",
     "status": "hypothesis", "ref_strategy": "S-MULTI-01"},
    {"research_id": "R-010", "sport": "NFL",
     "title": "Weather fields are sparse; WindChill fires only on observed data",
     "hypothesis": "Wind/temp coverage is complete for outdoor games.",
     "method": "Audited nflverse temp/wind population across 1999-2026.",
     "data_window": "1999-2026",
     "result": "Coverage is partial (modern seasons best). Rule: missing weather => no signal, never an assumption.",
     "status": "tested", "ref_strategy": "S-NFL-05"},
    {"research_id": "R-011", "sport": "NHL",
     "title": "2026-27 schedule row-count anomaly (1344 vs 1312)",
     "hypothesis": "Imported 2026-27 REG schedule has exactly 1312 games.",
     "method": "Counted game_type=2 rows for season 20262027 in inherited snapshot.",
     "data_window": "2026-27",
     "result": "ANOMALY OPEN: 1344 rows. Imported as scheduled; quality gate flags per-team counts != 82; nightly NHL pull will confirm/trim.",
     "status": "hypothesis", "ref_strategy": "S-NHL-01"},
    {"research_id": "R-012", "sport": "MLB",
     "title": "2026 game-level log missing; standings snapshot bridges the gap",
     "hypothesis": "Full 2026 game log is obtainable in the seed window.",
     "method": "Sandbox has no StatsAPI access; schedule endpoint too large for the fetch channel.",
     "data_window": "2026-03..09",
     "result": "Gap documented: nightly collector backfills Mar-Sep 2026 game logs; until then 2026 MLB uses the verified standings snapshot + form splits.",
     "status": "hypothesis", "ref_strategy": "S-MLB-01"},
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
    out["research"] = install_research(con)
    store.set_meta(con, "data_as_of_utc", RETRIEVED_SEED)
    store.set_meta(con, "seed_manifest_sha256", man["manifest_sha256"])
    con.commit()

    # Backtests (single-sport only; multi is forward-only by design).
    out["backtests"] = {}
    for sid in CATALOG_BY_ID:
        if sid in MULTI_SOURCES:
            continue
        r = engine.run_backtest(con, sid)
        out["backtests"][sid] = r
        print(f"backtest {sid}: {r['parlays']} parlays / {r['legs']} legs", flush=True)
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
    # One lottery ticket at seed time (labeled with ISO week).
    lotto = engine.run_forward(con, "S-MULTI-04", fwd_slates[:7],
                               decision_utc=RETRIEVED_SEED)
    con.execute("UPDATE parlays SET note = note || ' 2026-W39' WHERE strategy_id='S-MULTI-04'"
                " AND test_mode='forward'")
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
