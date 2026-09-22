"""Offline reproduction harness for the nightly pipeline (diagnostic tool).

The GitHub Actions nightly job runs with network; this sandbox does not. To
reproduce a CI failure locally we replay the *structural* effects of the live
collectors against a copy of the seeded database:

  * nflverse re-import  -> same pinned CSV, NEW retrieved timestamp (this is
    exactly what a fresh download does to the prices table: one new close row
    per quote with a later observed_utc).
  * verified results    -> real, hand-verified finals applied the way the
    collectors would apply them (see data/seed/crosscheck for the evidence).
  * ESPN upsert shape   -> a duplicate row keyed ``SPORT:espn:<id>`` whose
    game_date is the UTC date, which is what _upsert_espn_event writes.
  * MLB StatsAPI shape  -> new game rows for the last 3 days + a standings
    snapshot upsert with as_of_utc = now.

Then it runs the same post-collect phases as scripts/nightly.py (Elo, MULTI
backtest refresh, forward, settle, quality, export) and finally the PASS-2
audit against the simulated database/site directories.

Nothing here writes to the real database or the committed exports.
Usage:  python3 scripts/sim_nightly_offline.py [--keep]
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

SIM_DIR = Path("/tmp/parlaysports-sim")


def _fail(msg: str) -> None:
    raise SystemExit(msg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep the simulated DB")
    args = ap.parse_args()

    from parlaysports import config

    if not config.DB_PATH.exists():
        _fail("no seeded database; run `make seed` first")
    SIM_DIR.mkdir(parents=True, exist_ok=True)
    # The audit checks the unit-test suite and the static site shell too, so the
    # simulation mirrors them in: symlinks stay current with the working tree and
    # keep the harness honest (a real regression is never masked as "artifact").
    for name in ("tests", "index.html", "styles.css", "app.js"):
        link = SIM_DIR / name
        if not link.exists():
            try:
                link.symlink_to(ROOT / name)
            except OSError:
                shutil.copytree(ROOT / name, link) if (ROOT / name).is_dir() \
                    else shutil.copy(ROOT / name, link)
    sim_db = SIM_DIR / "parlaysports.db"
    sim_site = SIM_DIR / "site"
    for stale in SIM_DIR.glob("parlaysports.db*"):
        stale.unlink()
    if sim_site.exists():
        shutil.rmtree(sim_site)
    shutil.copy(config.DB_PATH, sim_db)

    # ---- point every module at the simulation copies -------------------
    config.DB_PATH = sim_db
    config.SITE_DIR = sim_site
    import parlaysports.store as store
    import parlaysports.export as export
    import parlaysports.quality as quality
    import parlaysports.engine as engine
    import parlaysports.ratings as ratings
    import parlaysports.ingest as ingest
    from parlaysports.util import utcnow_iso

    _connect = store.connect
    store.connect = lambda path=None: _connect(sim_db if path is None else path)
    export.SITE_DIR = sim_site

    con = store.connect()
    now = utcnow_iso()
    print(f"== offline nightly simulation ({now}) ==", flush=True)
    print(f"sim db: {sim_db}\nsim site: {sim_site}", flush=True)

    # ---- 1. nflverse re-import with a fresh retrieval timestamp ---------
    stats = ingest.ingest_nfl(con, retrieved_utc=now)
    print(f"nflverse re-import: {stats['games']} games, {stats['prices']} prices",
          flush=True)

    # ---- 2. verified finals the live collectors would have picked up ----
    verified = [
        # NFL week 2 MNF, verified 2026-09-22 against ESPN API + ESPN recap
        # + USA Today live blog (see data/seed/crosscheck).
        ("NFL:2026_02_NYG_LA", "final", 6, 28),
    ]
    for key, status, a, h in verified:
        row = con.execute("SELECT status, away_score, home_score FROM games "
                          "WHERE game_key=?", (key,)).fetchone()
        if row is None:
            print(f"  ! {key} not in database", flush=True)
            continue
        con.execute("UPDATE games SET status=?, away_score=?, home_score=? "
                    "WHERE game_key=?", (status, a, h, key))
        print(f"  applied verified final {key}: {row['status']} -> {status} {a}-{h}",
              flush=True)

    # ---- 3. ESPN pull through the REAL collector path -------------------
    # This section used to hand-roll the pre-fix insert shape to reproduce the
    # Actions failure (a duplicate SPORT:espn:<id> row on the UTC date, which
    # broke the audit and stopped the exports from publishing). Now it calls the
    # collector itself, so the harness fails again if the fix ever regresses.
    import importlib.util
    spec = importlib.util.spec_from_file_location("nightly_mod", ROOT / "scripts" / "nightly.py")
    nightly = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nightly)
    espn_events = [
        # Monday-night kickoff 2026-09-22T00:15Z: ESPN files it on the next UTC
        # day while nflverse keeps the local gameday 2026-09-21.
        dict(sport="NFL", espn_id="401872947", season="2026", game_type="REG",
             date="2026-09-22", start_utc="2026-09-22T00:15Z", week="Week 2",
             away="NYG", home="LA", venue="SoFi Stadium", status="final",
             away_score=6, home_score=28, url="sim"),
    ]
    for ev in espn_events:
        key = nightly._guarded_upsert_game(con, **ev)
        print(f"  ESPN pull -> {key} (event date {ev['date']})", flush=True)
        for market, sel, line, odds in (("ML", "home", None, -380.0),
                                        ("ML", "away", None, 300.0)):
            nightly._price(con, key, market, sel, line, odds, "market_reference",
                           "SRC_ESPN_SCOREBOARD", "sim", now, "simulated ESPN odds block")
        # a second visit in the same night must not stack identical snapshots
        for market, sel, line, odds in (("ML", "home", None, -380.0),):
            nightly._price(con, key, market, sel, line, odds, "market_reference",
                           "SRC_ESPN_SCOREBOARD", "sim", now, "simulated ESPN odds block")
    n_espn = con.execute("SELECT COUNT(*) AS n FROM games WHERE sport='NFL' "
                         "AND game_key LIKE '%:espn:%'").fetchone()["n"]
    print(f"  NFL rows keyed by ESPN id after the pull: {n_espn} (must be 0 when the "
          f"league log already has the game)", flush=True)

    # ---- 4. MLB StatsAPI shape: recent finals + standings snapshot ------
    mlb_finals = [
        # gamePk, officialDate, gameDate(UTC), away, home, a, h  (verified 2026-09-22)
        ("MLB:824787", "2026-09-21", "2026-09-21T22:35:00Z", "TOR", "BAL", 3, 4),
        ("MLB:824221", "2026-09-21", "2026-09-21T22:40:00Z", "WSH", "DET", 2, 9),
        ("MLB:823169", "2026-09-21", "2026-09-22T01:45:00Z", "MIN", "SF", 2, 5),
    ]
    for key, official, start, away, home, a, h in mlb_finals:
        con.execute(
            """INSERT INTO games(game_key, sport, league_game_id, season, game_type,
               game_date, start_utc, week_or_slate, away_team, home_team, neutral,
               venue, status, away_score, home_score, overtime, source_id,
               source_url, retrieved_utc, verified, verify_note, extra_json)
               VALUES (?, 'MLB', ?, '2026', 'R', ?, ?, NULL, ?, ?, 0, NULL, 'final',
               ?, ?, NULL, 'SRC_MLB_STATSAPI', 'sim', ?, 1,
               'simulated MLB nightly pull', NULL)
               ON CONFLICT(game_key) DO UPDATE SET status=excluded.status,
                 away_score=excluded.away_score, home_score=excluded.home_score,
                 start_utc=COALESCE(excluded.start_utc, games.start_utc),
                 retrieved_utc=excluded.retrieved_utc""",
            (key, key.split(":", 1)[1], official, start, away, home, a, h, now))
    # standings snapshot upsert with as_of = now (as _upsert_team_form does)
    n_form = 0
    for r in con.execute("SELECT team, w, l, rs, ra FROM team_form "
                         "WHERE sport='MLB' AND season='2026'").fetchall():
        con.execute(
            """INSERT OR REPLACE INTO team_form(team_key, sport, team, season,
               as_of_utc, source_id, w, l, rs, ra, streak_code, streak_n,
               last10_w, last10_l, home_w, home_l, away_w, away_l, xw, xl, note)
               VALUES (?, 'MLB', ?, '2026', ?, 'SRC_MLB_STATSAPI', ?, ?, ?, ?,
               NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
               'simulated nightly standings pull')""",
            (f"MLB:{r['team']}", r["team"], now, r["w"], r["l"], r["rs"], r["ra"]))
        n_form += 1
    print(f"  simulated MLB pull: {len(mlb_finals)} finals, {n_form} form rows",
          flush=True)
    con.commit()

    # ---- 5. post-collect phases, identical to scripts/nightly.py --------
    for sport in ("NFL", "MLB", "NHL", "NBA"):
        out = ratings.compute_elo(con, sport)
        print(f"elo {sport}: {out['games_rated']} games", flush=True)

    from parlaysports.strategies import CATALOG_BY_ID, MULTI_SOURCES
    multi_ids = [s for s in CATALOG_BY_ID if s in MULTI_SOURCES]
    res = engine.run_backtest_multi_all(con, multi_ids)
    for sid, r in sorted(res.items()):
        print(f"multi-refresh {sid}: {r['parlays']} new parlays", flush=True)

    today = now[:10]
    slates = [r["game_date"] for r in con.execute(
        """SELECT DISTINCT game_date FROM games WHERE status='scheduled'
           AND game_date >= ? AND game_date <= date(?, '+7 days')
           AND ((sport='NFL' AND game_type IN ('REG','POST'))
             OR (sport='MLB' AND game_type='R')
             OR (sport='NHL' AND game_type IN ('REG','POST'))
             OR (sport='NBA' AND game_type IN ('REG','POST')))
           ORDER BY game_date""", (today, today)).fetchall()]
    print(f"slates: {slates}", flush=True)
    for sid in CATALOG_BY_ID:
        r = engine.run_forward(con, sid, slates)
        if r["parlays"]:
            print(f"forward {sid}: {r['parlays']} parlays", flush=True)
    print(f"settle: {engine.settle_all(con)}", flush=True)
    q = quality.run_checks(con)
    store.set_meta(con, "last_quality_run", __import__("json").dumps(q))
    store.set_meta(con, "data_as_of_utc", now)
    print(f"quality problems: {q['total_problems']}", flush=True)
    for c in q["checks"]:
        if c["problems"]:
            print(f"  [{c['check']}] {c['problems']}: {c['detail'][:3]}", flush=True)
    export.export_all(con)
    con.commit()
    con.close()

    # ---- 6. the PASS-2 audit against the simulated state ----------------
    print("\n== audit against simulated post-nightly state ==", flush=True)
    import scripts.audit as audit
    audit.DB_PATH = sim_db
    audit.SITE_DIR = sim_site
    # audit writes <ROOT>/data/audit_report.json: keep the simulation's report
    # out of the repository's real one.
    (SIM_DIR / "data").mkdir(parents=True, exist_ok=True)
    audit.ROOT = SIM_DIR
    rc = audit.main()
    if not args.keep:
        for stale in SIM_DIR.glob("parlaysports.db*"):
            stale.unlink()
    return rc


if __name__ == "__main__":
    sys.exit(main())
