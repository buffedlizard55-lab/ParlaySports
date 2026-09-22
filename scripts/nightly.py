"""Nightly collector (GitHub Actions): live APIs -> forward -> settle -> export.

Runs with full network on the Actions runner. Every source is independent:
a failure is recorded as an issue and the pipeline continues with the rest.
No source is ever guessed; missing data stays missing.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from parlaysports import engine, export, quality, ratings, store
from parlaysports.config import DB_PATH
from parlaysports.strategies import CATALOG_BY_ID
from parlaysports.util import utcnow_iso

UA = {"User-Agent": "ParlaySports-paper-research/1.0 (+https://github.com/buffedlizard55-lab/ParlaySports)"}
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


def fetch_json(url: str, timeout: int = 30, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url} :: {last}")


def touch_source(con: sqlite3.Connection, source_id: str) -> None:
    con.execute("UPDATE sources SET last_verified_utc=? WHERE source_id=?",
                (utcnow_iso(), source_id))


# ------------------------------------------------------------------ ESPN
ESPN_SPORTS = {
    "NFL": "football/nfl",
    "MLB": "baseball/mlb",
    "NHL": "hockey/nhl",
    "NBA": "basketball/nba",
}

ESPN_ABBR_FIX = {
    # ESPN -> canonical
    "NFL": {"LAR": "LA"},
    "NBA": {"NY": "NYK", "GS": "GSW", "SA": "SAS", "UTAH": "UTA",
            "WSH": "WAS", "NO": "NOP"},
    "MLB": {}, "NHL": {},
}


def collect_espn(con: sqlite3.Connection, sport: str, days_back=2, days_fwd=7) -> dict:
    path = ESPN_SPORTS[sport]
    n_games = n_prices = 0
    for delta in range(-days_back, days_fwd + 1):
        day = (datetime.now(timezone.utc) + timedelta(days=delta)).strftime("%Y%m%d")
        url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={day}&limit=50"
        try:
            data = fetch_json(url)
        except RuntimeError as e:
            store.add_issue(con, severity="warning", area="collect", sport=sport,
                            detail=f"ESPN scoreboard {day}: {e}")
            continue
        for ev in data.get("events", []):
            try:
                g, p = _upsert_espn_event(con, sport, ev, url)
                n_games += g
                n_prices += p
            except Exception as e:
                store.add_issue(con, severity="warning", area="collect", sport=sport,
                                detail=f"ESPN event {ev.get('id')}: {e}")
        con.commit()
    touch_source(con, "SRC_ESPN_SCOREBOARD")
    return {"games": n_games, "prices": n_prices}


def _upsert_espn_event(con, sport, ev, url) -> tuple[int, int]:
    comp = (ev.get("competitions") or [{}])[0]
    competitors = comp.get("competitors", [])
    if len(competitors) < 2:
        return 0, 0
    home = next(c for c in competitors if c.get("homeAway") == "home")
    away = next(c for c in competitors if c.get("homeAway") == "away")
    fix = ESPN_ABBR_FIX.get(sport, {})
    ha = fix.get(home["team"]["abbreviation"], home["team"]["abbreviation"])
    aa = fix.get(away["team"]["abbreviation"], away["team"]["abbreviation"])
    # Skip non-league exhibitions (NBA preseason vs non-NBA clubs etc.)
    if sport == "NBA" and (ha in ("STARS", "STRIPES", "WORLD") or aa in ("STARS", "STRIPES", "WORLD")):
        return 0, 0
    state = ((comp.get("status") or {}).get("type") or {}).get("state", "pre")
    status = {"pre": "scheduled", "in": "live", "post": "final"}.get(state, "scheduled")
    season = str((((ev.get("season") or {}).get("year")) or "")) or _season_guess(sport)
    date = (comp.get("date") or ev.get("date") or "")[:10]
    game_key = f"{sport}:espn:{ev['id']}"
    now = utcnow_iso()
    con.execute(
        """INSERT INTO games(game_key, sport, league_game_id, season, game_type, game_date,
           start_utc, week_or_slate, away_team, home_team, neutral, venue, status,
           away_score, home_score, overtime, source_id, source_url, retrieved_utc,
           verified, verify_note, extra_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, NULL,
           'SRC_ESPN_SCOREBOARD', ?, ?, 1, 'ESPN nightly pull', NULL)
           ON CONFLICT(game_key) DO UPDATE SET status=excluded.status,
             away_score=excluded.away_score, home_score=excluded.home_score,
             start_utc=COALESCE(excluded.start_utc, games.start_utc),
             retrieved_utc=excluded.retrieved_utc""",
        (game_key, sport, str(ev["id"]), season, _gametype(sport, ev),
         date, comp.get("date"), _week(ev), aa, ha,
         (comp.get("venue") or {}).get("fullName"),
         status,
         _int_or_none(home.get("score")) if status == "final" else None,
         _int_or_none(away.get("score")) if status == "final" else None, url, now))
    n_prices = 0
    for odd in comp.get("odds", []) or []:
        prov = ((odd.get("provider") or {}).get("name")) or "ESPN"
        # moneyline
        ml = odd.get("moneyline") or {}
        for sel, side in (("home", ml.get("home")), ("away", ml.get("away"))):
            o = _american(((side or {}).get("close") or {}).get("odds") or
                          ((side or {}).get("open") or {}).get("odds"))
            if o is not None:
                _price(con, game_key, "ML", sel, None, o, "market_reference",
                       "SRC_ESPN_SCOREBOARD", url, now,
                       f"ESPN odds block ({prov})")
                n_prices += 1
        ps = odd.get("pointSpread") or {}
        for sel, side in (("home", ps.get("home")), ("away", ps.get("away"))):
            node = (side or {}).get("close") or (side or {}).get("open") or {}
            line = _float_or_none(node.get("line"))
            o = _american(node.get("odds"))
            if line is not None and o is not None:
                _price(con, game_key, "SPREAD", sel, line, o, "market_reference",
                       "SRC_ESPN_SCOREBOARD", url, now, f"ESPN odds block ({prov})")
                n_prices += 1
        tot = odd.get("total") or {}
        for sel, side in (("over", tot.get("over")), ("under", tot.get("under"))):
            node = (side or {}).get("close") or (side or {}).get("open") or {}
            raw = node.get("line")
            line = _float_or_none(str(raw).lstrip("oOuU") if raw else None)
            o = _american(node.get("odds"))
            if line is not None and o is not None:
                _price(con, game_key, "TOTAL", sel, line, o, "market_reference",
                       "SRC_ESPN_SCOREBOARD", url, now, f"ESPN odds block ({prov})")
                n_prices += 1
    return 1, n_prices


def _price(con, game_key, market, selection, line, odds, otype, source_id,
           source_url, observed_utc, note) -> None:
    con.execute(
        """INSERT INTO prices(game_key, market, selection, line, odds_american,
           odds_type, source_id, source_url, observed_utc, close_flag, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)""",
        (game_key, market, selection, line, odds, otype, source_id,
         source_url, observed_utc, note))


def _int_or_none(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _float_or_none(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _american(x):
    if x is None:
        return None
    try:
        return float(str(x).replace("+", ""))
    except ValueError:
        return None


def _week(ev) -> str | None:
    w = ev.get("week") or {}
    return f"Week {w['number']}" if w.get("number") else None


def _gametype(sport, ev) -> str:
    t = ((ev.get("season") or {}).get("type", 2))
    if sport == "MLB":
        return "R" if t == 2 else ("PRE" if t == 1 else "POST")
    if sport == "NHL":
        return {1: "PRE", 2: "REG", 3: "POST"}.get(t, "REG")
    return {1: "PRE", 2: "REG", 3: "POST"}.get(t, "REG")


def _season_guess(sport) -> str:
    now = datetime.now(timezone.utc)
    if sport in ("NFL",):
        return str(now.year if now.month >= 3 else now.year - 1)
    if sport in ("MLB",):
        return str(now.year)
    y = now.year if now.month >= 7 else now.year - 1  # NHL/NBA span calendar years
    if sport == "NHL":
        return f"{y}{y + 1}"
    return f"{y}-{str(y + 1)[2:]}"


# ------------------------------------------------------------------- NHL
def collect_nhl(con: sqlite3.Connection) -> dict:
    n_games = 0
    try:
        data = fetch_json("https://api-web.nhle.com/v1/schedule/now")
    except RuntimeError as e:
        store.add_issue(con, severity="warning", area="collect", sport="NHL",
                        detail=f"NHL schedule/now: {e}")
        return {"games": 0}
    now = utcnow_iso()
    for week in data.get("gameWeek", []):
        for g in week.get("games", []):
            state = g.get("gameState", "PRE")
            status = {"OFF": "final", "FINAL": "final", "LIVE": "live", "CRIT": "live",
                      "PRE": "scheduled", "FUT": "scheduled"}.get(state, "scheduled")
            game_key = f"NHL:{g['id']}"
            con.execute(
                """INSERT INTO games(game_key, sport, league_game_id, season, game_type,
                   game_date, start_utc, week_or_slate, away_team, home_team, neutral,
                   venue, status, away_score, home_score, overtime, source_id,
                   source_url, retrieved_utc, verified, verify_note, extra_json)
                   VALUES (?, 'NHL', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, NULL,
                   'SRC_NHL_API', ?, ?, 1, 'NHL nightly pull', NULL)
                   ON CONFLICT(game_key) DO UPDATE SET status=excluded.status,
                     away_score=excluded.away_score, home_score=excluded.home_score,
                     retrieved_utc=excluded.retrieved_utc""",
                (game_key, str(g["id"]), str(g.get("season", "")),
                 {1: "PRE", 2: "REG", 3: "POST"}.get(g.get("gameType"), "REG"),
                 week.get("date", ""), g.get("startTimeUTC"),
                 g["awayTeam"]["abbrev"], g["homeTeam"]["abbrev"],
                 1 if g.get("neutralSite") else 0,
                 (g.get("venue") or {}).get("default"),
                 status,
                 g["awayTeam"].get("score") if status == "final" else None,
                 g["homeTeam"].get("score") if status == "final" else None,
                 "https://api-web.nhle.com/v1/schedule/now", now))
            n_games += 1
    # partner odds (reference lines)
    n_prices = 0
    try:
        odds = fetch_json("https://api-web.nhle.com/v1/partner-game/US/now")
        for g in odds.get("games", []):
            game_key = f"NHL:{g.get('gameId')}"
            for book in g.get("odds", []) or []:
                prov = book.get("provider", "partner")
                for sel, px in (("home", book.get("homeWinOdds")),
                                ("away", book.get("awayWinOdds"))):
                    o = _american(px)
                    if o is not None:
                        _price(con, game_key, "ML", sel, None, o,
                               "market_reference", "SRC_NHL_API",
                               "https://api-web.nhle.com/v1/partner-game/US/now",
                               now, f"NHL partner feed ({prov})")
                        n_prices += 1
    except RuntimeError as e:
        store.add_issue(con, severity="warning", area="collect", sport="NHL",
                        detail=f"NHL partner-game: {e}")
    touch_source(con, "SRC_NHL_API")
    con.commit()
    return {"games": n_games, "prices": n_prices}


# ------------------------------------------------------------------- MLB
def collect_mlb(con: sqlite3.Connection, days_back=3, days_fwd=7) -> dict:
    start = (datetime.now(timezone.utc) - timedelta(days=days_back)).strftime("%Y-%m-%d")
    end = (datetime.now(timezone.utc) + timedelta(days=days_fwd)).strftime("%Y-%m-%d")
    url = (f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&startDate={start}"
           f"&endDate={end}&hydrate=team,linescore,probablePitcher")
    n_games = 0
    try:
        data = fetch_json(url)
    except RuntimeError as e:
        store.add_issue(con, severity="warning", area="collect", sport="MLB",
                        detail=f"MLB schedule: {e}")
        return {"games": 0}
    now = utcnow_iso()
    for day in data.get("dates", []):
        for g in day.get("games", []):
            code = (g.get("status") or {}).get("detailedState", "")
            status = "final" if code == "Final" else (
                "live" if "In Progress" in code else "scheduled")
            ls = g.get("linescore") or {}
            teams = g.get("teams", {})
            game_key = f"MLB:{g['gamePk']}"
            abbr = {t.get("id"): t.get("abbreviation") for t in
                    [teams.get("away", {}).get("team", {}), teams.get("home", {}).get("team", {})]}
            con.execute(
                """INSERT INTO games(game_key, sport, league_game_id, season, game_type,
                   game_date, start_utc, week_or_slate, away_team, home_team, neutral,
                   venue, status, away_score, home_score, overtime, source_id,
                   source_url, retrieved_utc, verified, verify_note, extra_json)
                   VALUES (?, 'MLB', ?, ?, ?, ?, ?, NULL, ?, ?, 0, ?, ?, ?, ?, NULL,
                   'SRC_MLB_STATSAPI', ?, ?, 1, 'MLB nightly pull', ?)
                   ON CONFLICT(game_key) DO UPDATE SET status=excluded.status,
                     away_score=excluded.away_score, home_score=excluded.home_score,
                     start_utc=COALESCE(excluded.start_utc, games.start_utc),
                     retrieved_utc=excluded.retrieved_utc""",
                (game_key, str(g["gamePk"]), str(g.get("season", datetime.now(timezone.utc).year)),
                 g.get("gameType", "R"), day.get("date"), g.get("gameDate"),
                 (teams.get("away", {}).get("team", {}) or {}).get("abbreviation"),
                 (teams.get("home", {}).get("team", {}) or {}).get("abbreviation"),
                 (g.get("venue") or {}).get("name"), status,
                 teams.get("away", {}).get("score") if status == "final" else None,
                 teams.get("home", {}).get("score") if status == "final" else None,
                 url, now,
                 json.dumps({
                     "probable_away": (teams.get("away", {}).get("probablePitcher") or {}).get("fullName"),
                     "probable_home": (teams.get("home", {}).get("probablePitcher") or {}).get("fullName"),
                 })))
            n_games += 1
    # standings snapshot -> team_form
    season = datetime.now(timezone.utc).year
    try:
        st = fetch_json(f"https://statsapi.mlb.com/api/v1/standings?leagueId=103,104"
                        f"&season={season}&standingsTypes=regularSeason")
        for rec in st.get("records", []):
            for tr in rec.get("teamRecords", []):
                _upsert_team_form(con, tr, season, now)
    except RuntimeError as e:
        store.add_issue(con, severity="warning", area="collect", sport="MLB",
                        detail=f"MLB standings: {e}")
    touch_source(con, "SRC_MLB_STATSAPI")
    con.commit()
    return {"games": n_games}


def _upsert_team_form(con, tr, season, now) -> None:
    splits = {s["type"]: s for s in (tr.get("records", {}).get("splitRecords", []) or [])}
    exp = {s["type"]: s for s in (tr.get("records", {}).get("expectedRecords", []) or [])}
    xw = exp.get("xWinLossSeason") or exp.get("xWinLoss") or {}
    l10 = splits.get("lastTen", {})
    con.execute(
        """INSERT OR REPLACE INTO team_form(team_key, sport, team, season, as_of_utc,
           source_id, w, l, rs, ra, streak_code, streak_n, last10_w, last10_l,
           home_w, home_l, away_w, away_l, xw, xl, note)
           VALUES (?, 'MLB', ?, ?, ?, 'SRC_MLB_STATSAPI', ?, ?, ?, ?, ?, ?, ?, ?,
           ?, ?, ?, ?, ?, ?, 'nightly standings pull')""",
        (f"MLB:{tr['team']['abbreviation']}", tr["team"]["abbreviation"],
         str(season), now, tr.get("wins"), tr.get("losses"),
         tr.get("runsScored"), tr.get("runsAllowed"),
         (tr.get("streak") or {}).get("streakCode"),
         (tr.get("streak") or {}).get("streakNumber"),
         l10.get("wins"), l10.get("losses"),
         splits.get("home", {}).get("wins"), splits.get("home", {}).get("losses"),
         splits.get("away", {}).get("wins"), splits.get("away", {}).get("losses"),
         xw.get("wins"), xw.get("losses")))


# ---------------------------------------------------------------- Kalshi
KALSHI_SERIES = {
    "KXNFLGAME": "NFL", "KXNHLGAME": "NHL", "KXNBAGAME": "NBA", "KXMLBGAME": "MLB",
}

KALSHI_ALIASES = {
    # yes_sub_title -> (sport, abbrev). Conservative: exact matches only.
    "Arizona": ("NFL", "ARI"), "Atlanta": ("NFL", "ATL"), "Baltimore": ("NFL", "BAL"),
    "Buffalo": ("NFL", "BUF"), "Carolina": ("NFL", "CAR"), "Chicago": ("NFL", "CHI"),
    "Cincinnati": ("NFL", "CIN"), "Cleveland": ("NFL", "CLE"), "Dallas": ("NFL", "DAL"),
    "Denver": ("NFL", "DEN"), "Detroit": ("NFL", "DET"), "Green Bay": ("NFL", "GB"),
    "Houston": ("NFL", "HOU"), "Indianapolis": ("NFL", "IND"),
    "Jacksonville": ("NFL", "JAX"), "Kansas City": ("NFL", "KC"),
    "Las Vegas": ("NFL", "LV"), "Los Angeles C": ("NFL", "LAC"),
    "Los Angeles R": ("NFL", "LA"), "Miami": ("NFL", "MIA"),
    "Minnesota": ("NFL", "MIN"), "New England": ("NFL", "NE"),
    "New Orleans": ("NFL", "NO"), "New York G": ("NFL", "NYG"),
    "New York J": ("NFL", "NYJ"), "Philadelphia": ("NFL", "PHI"),
    "Pittsburgh": ("NFL", "PIT"), "San Francisco": ("NFL", "SF"),
    "Seattle": ("NFL", "SEA"), "Tampa Bay": ("NFL", "TB"), "Tennessee": ("NFL", "TEN"),
    "Washington": ("NFL", "WAS"),
}


def collect_kalshi(con: sqlite3.Connection, limit_per_series: int = 200) -> dict:
    n_prices = n_skip = 0
    for series, sport in KALSHI_SERIES.items():
        url = (f"https://api.elections.kalshi.com/trade-api/v2/markets"
               f"?series_ticker={series}&status=open&limit={limit_per_series}")
        try:
            data = fetch_json(url)
        except RuntimeError as e:
            store.add_issue(con, severity="warning", area="collect", sport=sport,
                            detail=f"Kalshi {series}: {e}")
            continue
        for m in data.get("markets", []):
            if m.get("status") not in ("active", "open"):
                continue
            alias = KALSHI_ALIASES.get(m.get("yes_sub_title", ""))
            if alias is None or alias[0] != sport:
                n_skip += 1
                continue
            # Match to a scheduled game: same team, date ~= occurrence date.
            occ = (m.get("occurrence_datetime") or m.get("close_time") or "")[:10]
            g = con.execute(
                """SELECT game_key, status FROM games WHERE sport=? AND game_date=?
                   AND (away_team=? OR home_team=?) AND status='scheduled' LIMIT 1""",
                (sport, occ, alias[1], alias[1])).fetchone()
            if g is None and occ:
                # occurrence is often the day AFTER a night game; try occ-1.
                try:
                    prev = (datetime.strptime(occ, "%Y-%m-%d") - timedelta(days=1)
                            ).strftime("%Y-%m-%d")
                    g = con.execute(
                        """SELECT game_key, status FROM games WHERE sport=?
                           AND game_date=? AND (away_team=? OR home_team=?)
                           AND status='scheduled' LIMIT 1""",
                        (sport, prev, alias[1], alias[1])).fetchone()
                except ValueError:
                    g = None
            if g is None:
                n_skip += 1
                continue
            ask = _float_or_none(m.get("yes_ask_dollars"))
            if ask is None or not 0.0 < ask < 1.0:
                continue
            from parlaysports.util import prob_to_american
            side = con.execute(
                "SELECT away_team, home_team FROM games WHERE game_key=?",
                (g["game_key"],)).fetchone()
            sel = "away" if side["away_team"] == alias[1] else "home"
            _price(con, g["game_key"], "ML", sel, None,
                   round(prob_to_american(min(max(ask, 0.001), 0.999)), 2),
                   "market_verified", "SRC_KALSHI", url, utcnow_iso(),
                   f"kalshi {m.get('ticker')} yes_ask={ask} (fees excluded)")
            n_prices += 1
    if n_prices:
        touch_source(con, "SRC_KALSHI")
    con.commit()
    return {"prices": n_prices, "skipped_unmapped": n_skip}


# -------------------------------------------------------------- nflverse
def collect_nflverse(con: sqlite3.Connection) -> dict:
    import csv
    import tempfile
    url = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
    except (urllib.error.URLError, TimeoutError) as e:
        store.add_issue(con, severity="warning", area="collect", sport="NFL",
                        detail=f"nfldata games.csv: {e}")
        return {"games": 0}
    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as fh:
        fh.write(raw)
        tmp = fh.name
    from parlaysports import ingest as ing
    before = con.execute("SELECT COUNT(*) AS n FROM games WHERE sport='NFL'").fetchone()["n"]
    stats = ing.ingest_nfl(con, csv_path=tmp, retrieved_utc=utcnow_iso())
    touch_source(con, "SRC_NFLVERSE_GAMES")
    con.commit()
    Path(tmp).unlink(missing_ok=True)
    return {"games": stats["games"], "finals": stats["finals"], "prices": stats["prices"]}


# ------------------------------------------------------------------ main
def main() -> dict:
    out: dict = {"date": TODAY}
    if not DB_PATH.exists():
        print("no database; running seed first", flush=True)
        from scripts import seed as seedmod
        out["seed"] = seedmod.main()
    con = store.connect()
    steps = [
        ("nflverse", lambda: collect_nflverse(con)),
        ("espn_nfl", lambda: collect_espn(con, "NFL")),
        ("espn_mlb", lambda: collect_espn(con, "MLB")),
        ("espn_nhl", lambda: collect_espn(con, "NHL")),
        ("espn_nba", lambda: collect_espn(con, "NBA")),
        ("nhl", lambda: collect_nhl(con)),
        ("mlb", lambda: collect_mlb(con)),
        ("kalshi", lambda: collect_kalshi(con)),
    ]
    for name, fn in steps:
        try:
            out[name] = fn()
            print(f"{name}: {out[name]}", flush=True)
        except Exception as e:
            out[name] = {"error": str(e)}
            store.add_issue(con, severity="error", area="collect", detail=f"{name}: {e}")
    for sport in ("NFL", "MLB", "NHL", "NBA"):
        try:
            out[f"elo_{sport}"] = ratings.compute_elo(con, sport)
        except Exception as e:
            out[f"elo_{sport}"] = {"error": str(e)}
    # Forward generation on fresh slates (next 7 days with scheduled games).
    slates = [r["game_date"] for r in con.execute(
        """SELECT DISTINCT game_date FROM games WHERE status='scheduled'
           AND game_date >= ? AND game_date <= date(?, '+7 days')
           AND ((sport='NFL' AND game_type IN ('REG','POST'))
             OR (sport='MLB' AND game_type='R')
             OR (sport='NHL' AND game_type IN ('REG','POST'))
             OR (sport='NBA' AND game_type IN ('REG','POST')))
           ORDER BY game_date""", (TODAY, TODAY)).fetchall()]
    out["slates"] = slates
    out["forward"] = {}
    for sid in CATALOG_BY_ID:
        if sid == "S-MULTI-04":
            week = datetime.now(timezone.utc).strftime("%G-W%V")
            exists = con.execute(
                "SELECT 1 FROM parlays WHERE strategy_id='S-MULTI-04' "
                "AND test_mode='forward' AND note LIKE ? LIMIT 1",
                (f"%{week}%",)).fetchone()
            if exists:
                out["forward"][sid] = {"skipped": f"weekly ticket exists ({week})"}
                continue
        try:
            out["forward"][sid] = engine.run_forward(con, sid, slates)
        except Exception as e:
            out["forward"][sid] = {"error": str(e)}
            store.add_issue(con, severity="error", area="forward",
                            detail=f"{sid}: {e}")
    if "S-MULTI-04" not in out["forward"] or "skipped" not in out["forward"].get("S-MULTI-04", {}):
        pass
    else:
        pass
    out["settle"] = engine.settle_all(con)
    q = quality.run_checks(con)
    store.set_meta(con, "last_quality_run", json.dumps(q))
    store.set_meta(con, "data_as_of_utc", utcnow_iso())
    out["quality_problems"] = q["total_problems"]
    out["export"] = export.export_all(con)
    con.commit()
    con.close()
    print("== nightly complete ==", flush=True)
    return out


if __name__ == "__main__":
    print(json.dumps(main(), indent=1, default=str))
