"""Importers: verified snapshots -> normalized games / prices / team_form.

Every row keeps source_id + source_url + retrieved_utc. Importers validate
(row counts, score sanity, duplicate keys) and log issues instead of guessing.
Idempotent: re-running replaces only rows from the same source batch.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import SEED_DIR
from .store import add_issue, dump_json
from .util import utcnow_iso

# ------------------------------------------------------------------ helpers
def _game_exists(con: sqlite3.Connection, game_key: str) -> bool:
    return con.execute("SELECT 1 FROM games WHERE game_key=?", (game_key,)).fetchone() is not None


def _insert_game(con: sqlite3.Connection, g: dict[str, Any]) -> None:
    # Historical records are never silently modified: if a re-import would
    # change an already-recorded final score, file a conflict issue first
    # (the replacement then becomes a documented correction, not a quiet edit).
    prev = con.execute(
        "SELECT status, away_score, home_score, source_id FROM games WHERE game_key=?",
        (g["game_key"],)).fetchone()
    if prev is not None and prev["status"] == "final":
        old = (prev["away_score"], prev["home_score"])
        new = (g.get("away_score"), g.get("home_score"))
        if g.get("status") == "final" and None not in old and old != new:
            add_issue(con, severity="error", area="ingest", sport=g.get("sport"),
                      game_key=g["game_key"],
                      detail=(f"conflicting-results: final score changed "
                              f"{old[0]}-{old[1]} ({prev['source_id']}) -> "
                              f"{new[0]}-{new[1]} ({g.get('source_id')}); "
                              f"correction logged, prior values preserved in this issue"))
    con.execute(
        """INSERT OR REPLACE INTO games(game_key, sport, league_game_id, season, game_type,
           game_date, start_utc, week_or_slate, away_team, home_team, neutral, venue,
           status, away_score, home_score, overtime, source_id, source_url,
           retrieved_utc, verified, verify_note, extra_json)
           VALUES (:game_key, :sport, :league_game_id, :season, :game_type, :game_date,
           :start_utc, :week_or_slate, :away_team, :home_team, :neutral, :venue, :status,
           :away_score, :home_score, :overtime, :source_id, :source_url, :retrieved_utc,
           :verified, :verify_note, :extra_json)""",
        g,
    )


def _insert_price(con: sqlite3.Connection, *, game_key: str, market: str, selection: str,
                  line: float | None, odds_american: float | None, odds_type: str,
                  source_id: str, source_url: str, observed_utc: str,
                  close_flag: int = 0, note: str = "") -> int:
    # Idempotent re-import: an identical quote (same source + observation time)
    # is never duplicated. Prices are otherwise append-only history.
    dup = con.execute(
        """SELECT price_id FROM prices WHERE game_key=? AND market=? AND selection=?
           AND COALESCE(line,-999999)=COALESCE(?,-999999)
           AND COALESCE(odds_american,-999999)=COALESCE(?,-999999)
           AND source_id=? AND observed_utc=? AND close_flag=?""",
        (game_key, market, selection, line, odds_american,
         source_id, observed_utc, close_flag)).fetchone()
    if dup is not None:
        return int(dup["price_id"])
    cur = con.execute(
        """INSERT INTO prices(game_key, market, selection, line, odds_american, odds_type,
           source_id, source_url, observed_utc, close_flag, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (game_key, market, selection, line, odds_american, odds_type,
         source_id, source_url, observed_utc, close_flag, note),
    )
    return int(cur.lastrowid)


def _num(x: Any) -> float | None:
    if x is None:
        return None
    s = str(x).strip()
    if s == "" or s.lower() == "null":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int(x: Any) -> int | None:
    v = _num(x)
    return int(v) if v is not None else None


# --------------------------------------------------------------------- NFL
NFL_SOURCE_URL = "https://github.com/nflverse/nfldata/blob/master/data/games.csv"


def ingest_nfl(con: sqlite3.Connection, csv_path: str | Path | None = None,
               retrieved_utc: str | None = None) -> dict[str, Any]:
    """Import nflverse games.csv: schedules, scores, lines, weather, rest."""
    csv_path = Path(csv_path or SEED_DIR / "nfl" / "games.csv")
    retrieved_utc = retrieved_utc or utcnow_iso()
    stats = {"games": 0, "finals": 0, "scheduled": 0, "prices": 0,
             "bad_scores": 0, "seasons": set()}
    with open(csv_path, newline="") as fh:
        for r in csv.DictReader(fh):
            season = r["season"]
            stats["seasons"].add(season)
            game_key = f"NFL:{r['game_id']}"
            a_score, h_score = _int(r["away_score"]), _int(r["home_score"])
            if (a_score is None) != (h_score is None):
                stats["bad_scores"] += 1
                add_issue(con, severity="error", area="ingest", sport="NFL",
                          game_key=game_key,
                          detail=f"half-missing score away={r['away_score']} home={r['home_score']}")
                continue
            if a_score is not None and (a_score < 0 or h_score < 0 or a_score > 80 or h_score > 80):
                stats["bad_scores"] += 1
                add_issue(con, severity="error", area="ingest", sport="NFL",
                          game_key=game_key,
                          detail=f"implausible score {a_score}-{h_score}")
                continue
            status = "final" if a_score is not None else "scheduled"
            stats["finals" if status == "final" else "scheduled"] += 1
            gametime = (r.get("gametime") or "").strip()
            start_utc = None  # nflverse gametime is local ET wall time; kept in extra
            _insert_game(con, {
                "game_key": game_key, "sport": "NFL",
                "league_game_id": r["game_id"], "season": season,
                "game_type": r.get("game_type") or "REG",
                "game_date": r["gameday"], "start_utc": start_utc,
                "week_or_slate": f"Week {r.get('week')}",
                "away_team": r["away_team"], "home_team": r["home_team"],
                "neutral": 0, "venue": r.get("stadium") or None,
                "status": status, "away_score": a_score, "home_score": h_score,
                "overtime": _int(r.get("overtime")),
                "source_id": "SRC_NFLVERSE_GAMES", "source_url": NFL_SOURCE_URL,
                "retrieved_utc": retrieved_utc, "verified": 1,
                "verify_note": "nflverse snapshot; MNF cross-checked vs ESPN/DK",
                "extra_json": dump_json({
                    "gametime_local": gametime, "weekday": r.get("weekday"),
                    "roof": r.get("roof"), "surface": r.get("surface"),
                    "temp": _num(r.get("temp")), "wind": _num(r.get("wind")),
                    "away_rest": _int(r.get("away_rest")), "home_rest": _int(r.get("home_rest")),
                    "div_game": _int(r.get("div_game")),
                    "away_qb": r.get("away_qb_name"), "home_qb": r.get("home_qb_name"),
                    "espn": r.get("espn"), "old_game_id": r.get("old_game_id"),
                }),
            })
            stats["games"] += 1
            spread = _num(r.get("spread_line"))
            total = _num(r.get("total_line"))
            aml, hml = _num(r.get("away_moneyline")), _num(r.get("home_moneyline"))
            aso, hso = _num(r.get("away_spread_odds")), _num(r.get("home_spread_odds"))
            uo, oo = _num(r.get("under_odds")), _num(r.get("over_odds"))
            is_final = status == "final"
            # nflverse aggregates lines without naming the per-row bookmaker,
            # so these are reference-grade, not verified-direct quotes.
            otype = "market_reference"
            ourl = NFL_SOURCE_URL
            if aml is not None and hml is not None:
                _insert_price(con, game_key=game_key, market="ML", selection="away",
                              line=None, odds_american=aml, odds_type=otype,
                              source_id="SRC_NFLVERSE_GAMES", source_url=ourl,
                              observed_utc=retrieved_utc, close_flag=1 if is_final else 0,
                              note="close" if is_final else "current-line snapshot")
                _insert_price(con, game_key=game_key, market="ML", selection="home",
                              line=None, odds_american=hml, odds_type=otype,
                              source_id="SRC_NFLVERSE_GAMES", source_url=ourl,
                              observed_utc=retrieved_utc, close_flag=1 if is_final else 0,
                              note="close" if is_final else "current-line snapshot")
                stats["prices"] += 2
            if spread is not None and aso is not None and hso is not None:
                # nflverse convention (verified on 2026_02_NYG_LA vs ESPN/DK block):
                # spread_line>0 => home favored; away_line=+spread, home_line=-spread.
                _insert_price(con, game_key=game_key, market="SPREAD", selection="away",
                              line=spread,
                              odds_american=aso, odds_type=otype,
                              source_id="SRC_NFLVERSE_GAMES", source_url=ourl,
                              observed_utc=retrieved_utc, close_flag=1 if is_final else 0,
                              note="close" if is_final else "current-line snapshot")
                _insert_price(con, game_key=game_key, market="SPREAD", selection="home",
                              line=-spread,
                              odds_american=hso, odds_type=otype,
                              source_id="SRC_NFLVERSE_GAMES", source_url=ourl,
                              observed_utc=retrieved_utc, close_flag=1 if is_final else 0,
                              note="close" if is_final else "current-line snapshot")
                stats["prices"] += 2
            if total is not None and uo is not None and oo is not None:
                _insert_price(con, game_key=game_key, market="TOTAL", selection="over",
                              line=total, odds_american=oo, odds_type=otype,
                              source_id="SRC_NFLVERSE_GAMES", source_url=ourl,
                              observed_utc=retrieved_utc, close_flag=1 if is_final else 0,
                              note="close" if is_final else "current-line snapshot")
                _insert_price(con, game_key=game_key, market="TOTAL", selection="under",
                              line=total, odds_american=uo, odds_type=otype,
                              source_id="SRC_NFLVERSE_GAMES", source_url=ourl,
                              observed_utc=retrieved_utc, close_flag=1 if is_final else 0,
                              note="close" if is_final else "current-line snapshot")
                stats["prices"] += 2
    stats["seasons"] = sorted(stats["seasons"])
    return stats


# --------------------------------------------------------------------- MLB
MLB_RESULTS_URL = ("https://github.com/buffedlizard55-lab/MLB-Prediction-model-backtest"
                   "/blob/main/data/games.csv")


def ingest_mlb_results(con: sqlite3.Connection, csv_path: str | Path | None = None,
                       retrieved_utc: str | None = None) -> dict[str, Any]:
    """Import MLB game results 2015-2025 (scores only, no odds)."""
    csv_path = Path(csv_path or SEED_DIR / "mlb" / "results_2015_2025.csv")
    retrieved_utc = retrieved_utc or utcnow_iso()
    stats = {"games": 0, "bad": 0, "seasons": set()}
    with open(csv_path, newline="") as fh:
        for r in csv.DictReader(fh):
            stats["seasons"].add(r["season"])
            game_key = f"MLB:{r['game_pk']}"
            a_score, h_score = _int(r["away_score"]), _int(r["home_score"])
            if a_score is None or h_score is None:
                stats["bad"] += 1
                continue
            if a_score < 0 or h_score < 0 or a_score > 40 or h_score > 40 or a_score == h_score:
                stats["bad"] += 1
                add_issue(con, severity="error", area="ingest", sport="MLB",
                          game_key=game_key,
                          detail=f"implausible/tied score {a_score}-{h_score}")
                continue
            _insert_game(con, {
                "game_key": game_key, "sport": "MLB",
                "league_game_id": str(r["game_pk"]), "season": str(r["season"]),
                "game_type": r.get("game_type") or "R",
                "game_date": r["official_date"], "start_utc": None,
                "week_or_slate": None,
                "away_team": _mlb_abbr(r["away_team"]), "home_team": _mlb_abbr(r["home_team"]),
                "neutral": 0, "venue": None,
                "status": "final", "away_score": a_score, "home_score": h_score,
                "overtime": None,
                "source_id": "SRC_SIBLING_MLB_RESULTS", "source_url": MLB_RESULTS_URL,
                "retrieved_utc": retrieved_utc, "verified": 0,
                "verify_note": "inherited snapshot; counts validated, scores spot-check pending nightly",
                "extra_json": dump_json({
                    "away_team_id": r.get("away_team_id"), "home_team_id": r.get("home_team_id"),
                    "away_name": r.get("away_team"), "home_name": r.get("home_team"),
                }),
            })
            stats["games"] += 1
    stats["seasons"] = sorted(stats["seasons"])
    return stats


_MLB_ABBR = {
    "Arizona Diamondbacks": "ARI", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Cleveland Indians": "CLE",
    "Colorado Rockies": "COL", "Detroit Tigers": "DET", "Houston Astros": "HOU",
    "Kansas City Royals": "KC", "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD",
    "Miami Marlins": "MIA", "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN",
    "New York Mets": "NYM", "New York Yankees": "NYY", "Athletics": "ATH",
    "Oakland Athletics": "OAK", "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT",
    "San Diego Padres": "SD", "Seattle Mariners": "SEA", "San Francisco Giants": "SF",
    "St. Louis Cardinals": "STL", "Tampa Bay Rays": "TB", "Texas Rangers": "TEX",
    "Toronto Blue Jays": "TOR", "Washington Nationals": "WSH",
}


def _mlb_abbr(name: str) -> str:
    return _MLB_ABBR.get((name or "").strip(), (name or "").strip()[:3].upper())


def ingest_mlb_standings_snapshot(con: sqlite3.Connection,
                                  json_path: str | Path | None = None) -> dict[str, Any]:
    """Import the verified 2026-09-22 MLB standings snapshot into team_form."""
    json_path = Path(json_path or SEED_DIR / "mlb" / "standings_2026_20260922.json")
    snap = json.loads(Path(json_path).read_text())
    as_of = snap["as_of_utc"]
    n = 0
    for t in snap["teams"]:
        con.execute(
            """INSERT OR REPLACE INTO team_form(team_key, sport, team, season, as_of_utc,
               source_id, w, l, rs, ra, streak_code, streak_n, last10_w, last10_l,
               home_w, home_l, away_w, away_l, xw, xl, note)
               VALUES (?, 'MLB', ?, '2026', ?, 'SRC_MLB_STATSAPI', ?, ?, ?, ?, ?, ?, ?, ?,
               ?, ?, ?, ?, ?, ?, ?)""",
            (f"MLB:{t['abbr']}", t["abbr"], as_of, t["w"], t["l"], t["rs"], t["ra"],
             t.get("streak"), t.get("streak_n"), t.get("last10_w"), t.get("last10_l"),
             t.get("home_w"), t.get("home_l"), t.get("away_w"), t.get("away_l"),
             t.get("xw"), t.get("xl"), snap.get("source_url")),
        )
        n += 1
    return {"teams": n, "as_of_utc": as_of}


def ingest_mlb_fixtures(con: sqlite3.Connection,
                        json_path: str | Path | None = None) -> dict[str, Any]:
    """Import upcoming 2026 MLB fixtures (schedule + game_pks, no scores)."""
    json_path = Path(json_path or SEED_DIR / "mlb" / "fixtures_2026_final_week.json")
    snap = json.loads(Path(json_path).read_text())
    retrieved_utc = snap["retrieved_utc"]
    n = 0
    for f in snap["fixtures"]:
        game_key = f"MLB:{f['game_pk']}"
        if _game_exists(con, game_key):
            continue
        _insert_game(con, {
            "game_key": game_key, "sport": "MLB",
            "league_game_id": str(f["game_pk"]), "season": "2026",
            "game_type": f.get("game_type") or "R",
            "game_date": f["date"], "start_utc": f.get("start_utc"),
            "week_or_slate": "final-week", "away_team": f["away"], "home_team": f["home"],
            "neutral": 0, "venue": f.get("venue"), "status": "scheduled",
            "away_score": None, "home_score": None, "overtime": None,
            "source_id": "SRC_SIBLING_VACSCHED",
            "source_url": snap.get("source_url", ""),
            "retrieved_utc": retrieved_utc, "verified": 1,
            "verify_note": "league fixture list via VacationSchedule verified export",
            "extra_json": dump_json({"game_pk": f["game_pk"]}),
        })
        n += 1
    return {"fixtures": n}


# --------------------------------------------------------------------- NHL
def ingest_nhl(con: sqlite3.Connection, seed_dir: str | Path | None = None,
               retrieved_utc: str | None = None) -> dict[str, Any]:
    """Import NHL games + Kalshi GAME closes + DK partner snapshots (seed CSVs)."""
    seed_dir = Path(seed_dir or SEED_DIR / "nhl")
    retrieved_utc = retrieved_utc or utcnow_iso()
    stats: dict[str, Any] = {"games": 0, "finals": 0, "scheduled": 0,
                             "kalshi_prices": 0, "dk_prices": 0, "seasons": set()}
    team_map: dict[str, str] = {}
    tpath = seed_dir / "teams.csv"
    if tpath.exists():
        with open(tpath, newline="") as fh:
            for r in csv.DictReader(fh):
                team_map[str(r["team_id"])] = r["abbrev"]
    gpath = seed_dir / "games.csv"
    with open(gpath, newline="") as fh:
        for r in csv.DictReader(fh):
            stats["seasons"].add(r["season"])
            game_key = f"NHL:{r['game_id']}"
            a_score, h_score = _int(r["away_score"]), _int(r["home_score"])
            state = (r.get("state") or "").upper()
            # Trust the upstream state before inferring anything from scores:
            # a snapshot taken while a game is LIVE carries 0-0 and must never
            # be recorded as a final result, and a 0-0 "FINAL" is impossible in
            # hockey (a shootout still produces a goal) so it means the fixture
            # was not played as scheduled.
            zero_zero = (a_score in (0, None) and h_score in (0, None))
            if state == "LIVE":
                status = "live"
                stats["live"] = stats.get("live", 0) + 1
            elif state in ("PRE", "FUT", ""):
                status = "scheduled" if zero_zero else "final"
                stats["scheduled" if status == "scheduled" else "finals"] += 1
            elif zero_zero:
                status = "postponed"
                a_score = h_score = None
                stats["postponed"] = stats.get("postponed", 0) + 1
                detail = (f"impossible-scoreline: NHL {game_key} {r['game_date']} "
                          f"reported FINAL with 0-0 by the inherited snapshot; recorded as "
                          f"not played, never settled, never rated - flagged, not guessed")
                if not con.execute("SELECT 1 FROM issues WHERE detail=?", (detail,)).fetchone():
                    add_issue(con, severity="error", area="ingest", sport="NHL",
                              game_key=game_key, detail=detail)
            else:
                status = "final"
                stats["finals"] += 1
            _insert_game(con, {
                "game_key": game_key, "sport": "NHL",
                "league_game_id": str(r["game_id"]), "season": str(r["season"]),
                "game_type": {"1": "PRE", "2": "REG", "3": "POST"}.get(str(r.get("game_type")), str(r.get("game_type"))),
                "game_date": r["game_date"], "start_utc": r.get("start_time_utc") or None,
                "week_or_slate": None,
                "away_team": team_map.get(str(r["away_id"]), str(r["away_id"])),
                "home_team": team_map.get(str(r["home_id"]), str(r["home_id"])),
                "neutral": 0, "venue": r.get("venue") or None,
                "status": status, "away_score": a_score, "home_score": h_score,
                "overtime": None,
                "source_id": "SRC_SIBLING_NHLCOMP",
                "source_url": "https://github.com/buffedlizard55-lab/NHLComp",
                "retrieved_utc": retrieved_utc, "verified": 0,
                "verify_note": ("inherited snapshot; re-verified by nightly NHL API pull"
                                + (f"; upstream state {state} -- a live/pre-game snapshot is "
                                   f"never recorded as a result" if status == "live" else "")
                                + ("; 0-0 FINAL is impossible in hockey, recorded as not played"
                                   if status == "postponed" else "")),
                "extra_json": dump_json({"nhl_state": state,
                                         "away_id": r.get("away_id"),
                                         "home_id": r.get("home_id")}),
            })
            stats["games"] += 1
    kpath = seed_dir / "kalshi_game_prices.csv"
    if kpath.exists():
        with open(kpath, newline="") as fh:
            for r in csv.DictReader(fh):
                game_key = f"NHL:{r['game_id']}"
                ask = _num(r["ask"])
                if ask is None or not 0.0 < ask < 1.0:
                    continue
                _insert_price(con, game_key=game_key, market="ML",
                              selection=r["selection"], line=None,
                              odds_american=_kalshi_prob_to_american(ask),
                              odds_type="market_verified",
                              source_id="SRC_KALSHI", source_url=r.get("source_url") or "",
                              observed_utc=r.get("observed_utc") or retrieved_utc,
                              close_flag=1,
                              note=f"kalshi {r.get('contract')} {r.get('point')} "
                                   f"ask={ask} (fees excluded)")
                stats["kalshi_prices"] += 1
    dpath = seed_dir / "dk_snapshots.csv"
    if dpath.exists():
        with open(dpath, newline="") as fh:
            for r in csv.DictReader(fh):
                game_key = f"NHL:{r['game_id']}"
                for sel, price_col, line_col in (("home", "home_price", "home_qualifier"),
                                                 ("away", "away_price", "away_qualifier")):
                    px = _num(r[price_col])
                    if px is None:
                        continue
                    market = {"OVER_UNDER": "TOTAL", "PUCK_LINE": "SPREAD",
                              "MONEYLINE": "ML"}.get(r["market_desc"], r["market_desc"])
                    line = _dk_line(r[line_col], market)
                    _insert_price(con, game_key=game_key, market=market,
                                  selection=sel if market != "TOTAL" else
                                  ("over" if sel == "home" else "under"),
                                  line=line, odds_american=px,
                                  odds_type="market_reference",
                                  source_id="SRC_NHL_API",
                                  source_url=r.get("source_url") or "",
                                  observed_utc=r.get("retrieved_at") or retrieved_utc,
                                  close_flag=0,
                                  note=f"NHL partner feed ({r.get('partner')}) reference")
                    stats["dk_prices"] += 1
    stats["seasons"] = sorted(stats["seasons"])
    return stats


def _kalshi_prob_to_american(p: float) -> float:
    from .util import prob_to_american
    return round(prob_to_american(min(max(p, 0.001), 0.999)), 2)


def _dk_line(qual: str | None, market: str) -> float | None:
    if not qual:
        return None
    q = qual.strip().upper().replace("O", "").replace("U", "").replace("+", "")
    try:
        v = float(q)
    except ValueError:
        return None
    if market == "SPREAD" and qual.strip().startswith("-"):
        return -abs(v)
    return v


# --------------------------------------------------------------------- NBA
# The inherited NBAComp log carries no game-type column, and every row was
# previously imported as REG -- which mixed preseason exhibitions into the
# ratings trail and left the forward book free to bet them. Opening nights
# below are VERIFIED against public league/press sources (see
# data/seed/crosscheck); rows dated before the opener are preseason (PRE).
# Seasons 2013-14..2022-23 need no split: their earliest rows already sit on
# the real openers (checked season by season against the same sources).
NBA_REG_OPENERS: dict[str, tuple[str, str]] = {
    "2023-24": ("2023-10-24",
                "https://sportsgeardaily.com/basketball/when-does-nba-basketball-season-start"),
    "2024-25": ("2024-10-22",
                "https://www.foxsports.com/stories/nba/nba-schedule-release"),
    "2025-26": ("2025-10-21", "https://www.nba.com/news/key-dates?experience=app"),
    "2026-27": ("2026-10-20",
                "https://www.espn.com/nba/story/_/id/49471934/nba-full-schedule-2026-2027-games-watch-faq-rivalries-matchups"),
}


# Last full 30-team regular-season slate per season; everything after it is
# play-in/postseason (POST). Two of the three are externally verified, the
# third is derived from the log itself (see the evidence strings).
NBA_REG_ENDS: dict[str, tuple[str, str]] = {
    "2023-24": ("2024-04-14",
                "https://sportsgeardaily.com/basketball/when-does-nba-basketball-season-start"),
    "2024-25": ("2025-04-13",
                "https://www.foxsports.com/stories/nba/nba-schedule-release"),
    "2025-26": ("2026-04-12",
                "https://en.wikipedia.org/wiki/2025%E2%80%9326_NBA_season "
                "(regular season Oct 21 2025 - Apr 12 2026, play-in Apr 14-17; the log's "
                "last 15-game slate is 2026-04-12 and play-in pairs start 2026-04-14)"),
}


# The inherited NBAComp log reports these fixtures as 0-0 "finals". A 0-0
# finish is impossible in basketball, and every row below is an externally
# documented postponement: the league kept the fixture on the schedule and the
# upstream collector wrote zeros. Each carries its citation; a row WITHOUT one
# is still treated as not played (never settled, never rated) and stays flagged
# as unverified rather than being guessed at.
NBA_POSTPONEMENTS: dict[str, tuple[str, str]] = {
    "401585204": ("postponed 2024-01-17 after the death of Warriors assistant coach "
                  "Dejan Milojevic; replayed 2024-02-15 (GSW 140 UTA 137)",
                  "https://www.nba.com/news/nba-postpones-warriors-vs-jazz-game"),
    "401585217": ("postponed 2024-01-19 in the same week; replayed 2024-04-02 "
                  "(DAL 100 GSW 104)",
                  "https://www.mercurynews.com/2024-01-26/nba-reschedules-postponed-games-following-dejan-milojevics-death/"),
    "401705090": ("postponed 2025-01-09 by the Los Angeles wildfires; replayed 2025-02-19",
                  "https://www.nba.com/news/nba-postpones-hornets-vs-lakers-jan-9-2025"),
    "401705103": ("postponed 2025-01-11 by the Los Angeles wildfires",
                  "https://pr.nba.com/spurs-lakers-hornets-clippers-games-postponed/"),
    "401705104": ("postponed 2025-01-11 by the Los Angeles wildfires; replayed 2025-03-16",
                  "https://pr.nba.com/spurs-lakers-hornets-clippers-games-postponed/"),
    "401810384": ("postponed 2026-01-08 (moisture on the floor at United Center); "
                  "replayed 2026-01-29",
                  "https://pr.nba.com/category/nba-schedule/"),
    "401810499": ("postponed 2026-01-24 on safety-and-security grounds in Minneapolis; "
                  "replayed 2026-01-25",
                  "https://www.forbes.com/sites/mikefore/2026-01-27/nba-games-postponed-due-to-the-2026-winter-storm-revised-dates/"),
    "401810506": ("postponed 2026-01-25 by the January 2026 North American winter storm; "
                  "replayed 2026-03-18",
                  "https://www.nba.com/news/nba-postpones-games-in-memphis-and-milwaukee-due-to-massive-winter-storm"),
    "401810507": ("postponed 2026-01-25 by the January 2026 North American winter storm; "
                  "replayed 2026-03-31",
                  "https://www.nba.com/news/nba-postpones-games-in-memphis-and-milwaukee-due-to-massive-winter-storm"),
}

# NBA Cup championship dates. Every Cup game counts toward the 82-game record
# EXCEPT the final, so the two finalists legitimately play 83 (ESPN 2025 Cup
# explainer; Sportico: "the final is the lone game in the NBA Cup that does not
# count for a team's regular season record"). The log itself corroborates it:
# exactly the two finalists carry one extra game per season.
NBA_CUP_FINALS: dict[str, tuple[str, str]] = {
    "2023-24": ("2023-12-09", "https://www.espn.com/nba/story/_/id/46609036/"
                               "2025-nba-season-tournament-cup-format-highlights-updates"),
    "2024-25": ("2024-12-17", "https://www.sportico.com/feature/nba-in-season-tournament"
                               "-format-schedule-groups-explainer-1234746081/"),
    "2025-26": ("2025-12-16", "https://www.sportico.com/feature/nba-in-season-tournament"
                               "-format-schedule-groups-explainer-1234746081/"),
}


def _nba_game_type(season: str, game_date: str) -> tuple[str, str]:
    """(game_type, evidence) for one NBA row.

    PRE before the verified opening night, POST after the last full regular
    season slate, REG in between. Seasons without a verified boundary keep the
    inherited REG label (their slices start on the real opener).
    """
    opener = NBA_REG_OPENERS.get(season)
    end = NBA_REG_ENDS.get(season)
    if opener and game_date < opener[0]:
        return "PRE", f"derived: before verified {season} opening night {opener[0]} ({opener[1]})"
    if end and game_date > end[0]:
        return "POST", f"derived: after {season} regular season end {end[0]} ({end[1]})"
    if opener:
        return "REG", f"verified {season} opening night {opener[0]} ({opener[1]})"
    return "REG", "inherited snapshot starts on the season opener (SBR Oct-Dec slice)"


def ingest_nba(con: sqlite3.Connection, seed_dir: str | Path | None = None,
               retrieved_utc: str | None = None) -> dict[str, Any]:
    """Import NBA games + SBR historical closes + ESPN forward lines (seed CSVs)."""
    seed_dir = Path(seed_dir or SEED_DIR / "nba")
    retrieved_utc = retrieved_utc or utcnow_iso()
    stats: dict[str, Any] = {"games": 0, "finals": 0, "scheduled": 0,
                             "sbr_prices": 0, "forward_lines": 0, "seasons": set()}
    gpath = seed_dir / "games.csv"
    with open(gpath, newline="") as fh:
        for r in csv.DictReader(fh):
            stats["seasons"].add(r["season"])
            game_key = f"NBA:{r['game_id']}"
            nba_type, type_evidence = _nba_game_type(str(r["season"]), r["game_date_et"])
            stats["preseason"] = stats.get("preseason", 0) + (1 if nba_type == "PRE" else 0)
            stats["postseason"] = stats.get("postseason", 0) + (1 if nba_type == "POST" else 0)
            a_score, h_score = _int(r["away_score"]), _int(r["home_score"])
            status = (r.get("status") or "").lower()
            espn_id = str(r["game_id"]).split(":")[-1]
            # A 0-0 "final" is an impossible basketball scoreline: upstream kept
            # the postponed fixture and wrote zeros. Record it as not played,
            # drop the invented scores, cite the postponement when one is
            # verified, and file an issue so the correction is never silent.
            postponed = status == "final" and a_score == 0 and h_score == 0
            postpone_note = ""
            if postponed:
                status = "postponed"
                a_score = h_score = None
                stats["postponed"] = stats.get("postponed", 0) + 1
                reason, cite = NBA_POSTPONEMENTS.get(espn_id, (None, None))
                if reason:
                    postpone_note = f"verified postponement: {reason} ({cite})"
                    stats["postponed_verified"] = stats.get("postponed_verified", 0) + 1
                else:
                    postpone_note = ("inherited log reports a 0-0 'final' (impossible "
                                     "scoreline); recorded as not played, reason NOT "
                                     "independently verified - flagged, never guessed")
                detail = (f"impossible-scoreline: NBA {game_key} {r['game_date_et']} "
                          f"{r['away_team']}@{r['home_team']} reported as a 0-0 final by the "
                          f"inherited log; {postpone_note}")
                if not con.execute("SELECT 1 FROM issues WHERE detail=?", (detail,)).fetchone():
                    add_issue(con, severity="error", area="ingest", sport="NBA",
                              game_key=game_key, detail=detail)
            cup = NBA_CUP_FINALS.get(str(r["season"]))
            cup_final = bool(cup and r["game_date_et"] == cup[0])
            if postponed:
                pass                      # keep status='postponed': never re-infer it
            else:
                status = "final" if status == "final" and a_score is not None else (
                    "scheduled" if a_score is None else "final")
            if status == "final":
                stats["finals"] += 1
            elif status == "postponed":
                pass                      # counted above, with its verification state
            else:
                stats["scheduled"] += 1
            _insert_game(con, {
                "game_key": game_key, "sport": "NBA",
                "league_game_id": str(r["game_id"]), "season": str(r["season"]),
                "game_type": nba_type, "game_date": r["game_date_et"],
                "start_utc": r.get("tipoff_utc") or None,
                "week_or_slate": None,
                "away_team": r["away_team"], "home_team": r["home_team"],
                "neutral": 0, "venue": None,
                "status": status, "away_score": a_score, "home_score": h_score,
                "overtime": None,
                "source_id": "SRC_SIBLING_NBACOMP",
                "source_url": "https://github.com/buffedlizard55-lab/NBAComp",
                "retrieved_utc": retrieved_utc,
                "verified": 1 if (nba_type in ("PRE", "POST") or postpone_note.startswith(
                    "verified") or cup_final) else 0,
                "verify_note": "; ".join(filter(None, [
                    "inherited snapshot; re-verified by nightly ESPN pull",
                    f"game_type {type_evidence}", postpone_note,
                    (f"NBA Cup championship {cup[0]}: played but NOT counted toward the "
                     f"82-game record ({cup[1]})" if cup_final else "")])),
                "extra_json": dump_json({"upstream_source": r.get("source"),
                                         "game_type_evidence": type_evidence,
                                         "postponed": postpone_note or None,
                                         "nba_cup_championship": cup_final or None,
                                         "cup_evidence": cup[1] if cup_final else None}),
            })
            stats["games"] += 1
    spath = seed_dir / "sbr_odds.csv"
    if spath.exists():
        with open(spath, newline="") as fh:
            for r in csv.DictReader(fh):
                game_key = f"NBA:{r['game_key_suffix']}"
                if not _game_exists(con, game_key):
                    # SBR rows predate the ESPN game log; store a slim game row.
                    _insert_game(con, {
                        "game_key": game_key, "sport": "NBA",
                        "league_game_id": r["game_key_suffix"], "season": r["season"],
                        "game_type": "REG", "game_date": r["game_date_et"],
                        "start_utc": None, "week_or_slate": None,
                        "away_team": r["away"], "home_team": r["home"],
                        "neutral": 0, "venue": None, "status": "final",
                        "away_score": _int(r["away_final"]),
                        "home_score": _int(r["home_final"]), "overtime": None,
                        "source_id": "SRC_SBR_NBA", "source_url": r.get("source_url") or "",
                        "retrieved_utc": retrieved_utc, "verified": 0,
                        "verify_note": "SBR single source; scores not cross-checked",
                        "extra_json": dump_json({"season": r["season"]}),
                    })
                    stats["games"] += 1
                    stats["finals"] += 1
                ourl = r.get("source_url") or ""
                ct, chs = _num(r["close_total"]), _num(r["close_home_spread"])
                mla, mlh = _num(r["ml_away"]), _num(r["ml_home"])
                if ct is not None:
                    for sel, odds in (("over", -110.0), ("under", -110.0)):
                        _insert_price(con, game_key=game_key, market="TOTAL",
                                      selection=sel, line=ct, odds_american=odds,
                                      odds_type="market_reference",
                                      source_id="SRC_SBR_NBA", source_url=ourl,
                                      observed_utc=retrieved_utc, close_flag=1,
                                      note="SBR close total; juice not published, -110 assumed+LABELED")
                        stats["sbr_prices"] += 1
                if chs is not None:
                    _insert_price(con, game_key=game_key, market="SPREAD",
                                  selection="home", line=chs, odds_american=-110.0,
                                  odds_type="market_reference",
                                  source_id="SRC_SBR_NBA", source_url=ourl,
                                  observed_utc=retrieved_utc, close_flag=1,
                                  note="SBR close spread; juice -110 assumed+LABELED")
                    _insert_price(con, game_key=game_key, market="SPREAD",
                                  selection="away", line=-chs, odds_american=-110.0,
                                  odds_type="market_reference",
                                  source_id="SRC_SBR_NBA", source_url=ourl,
                                  observed_utc=retrieved_utc, close_flag=1,
                                  note="SBR close spread; juice -110 assumed+LABELED")
                    stats["sbr_prices"] += 2
                if mla is not None and mlh is not None:
                    for sel, odds in (("away", mla), ("home", mlh)):
                        _insert_price(con, game_key=game_key, market="ML",
                                      selection=sel, line=None, odds_american=odds,
                                      odds_type="market_reference",
                                      source_id="SRC_SBR_NBA", source_url=ourl,
                                      observed_utc=retrieved_utc, close_flag=1,
                                      note="SBR close moneyline")
                        stats["sbr_prices"] += 1
    fpath = seed_dir / "forward_lines.csv"
    if fpath.exists():
        with open(fpath, newline="") as fh:
            for r in csv.DictReader(fh):
                game_key = f"NBA:{r['game_id']}"
                if not _game_exists(con, game_key):
                    continue
                market = r["market"].upper()
                sel = r["selection"]
                if market == "TOTAL":
                    selection = "over" if sel.lower().startswith("o") else "under"
                    parts = sel.split()
                    line = _num(parts[-1]) if len(parts) > 1 else _num(r.get("line"))
                elif market == "SPREAD":
                    selection = "home" if "home" in sel.lower() else "away"
                    line = _num(r.get("line"))
                else:
                    selection, line = sel, _num(r.get("line"))
                price = _num(r.get("price"))
                otype = (r.get("odds_type") or "").strip() or "market_reference"
                if price is None:
                    price, otype = -110.0, "market_reference"
                src = "SRC_KALSHI" if "kalshi" in (r.get("source_url") or "") else "SRC_ESPN_SCOREBOARD"
                note = ("Kalshi opener ask (fees excluded)" if src == "SRC_KALSHI"
                        else "ESPN/DK forward line; juice -110 assumed+LABELED")
                _insert_price(con, game_key=game_key, market=market,
                              selection=selection, line=line, odds_american=price,
                              odds_type=otype, source_id=src,
                              source_url=r.get("source_url") or "",
                              observed_utc=r.get("captured_utc") or retrieved_utc,
                              close_flag=0, note=note)
                stats["forward_lines"] += 1
    stats["seasons"] = sorted(stats["seasons"])
    return stats


# ------------------------------------------------- cross-check evidence
def ingest_crosscheck(con: sqlite3.Connection,
                      json_path: str | Path | None = None) -> dict[str, Any]:
    """Record hand-verified cross-checks.

    Loads EVERY ``data/seed/crosscheck/checks_*.json`` file (sorted) so new
    independent verification passes accumulate instead of replacing the old
    evidence. Each check carries subject, claim, source URL, result + detail.
    """
    from .store import add_verification
    paths = ([Path(json_path)] if json_path
             else sorted((SEED_DIR / "crosscheck").glob("checks_*.json")))
    n = 0
    files = 0
    for path in paths:
        data = json.loads(Path(path).read_text())
        files += 1
        for c in data["checks"]:
            add_verification(con, subject=c["subject"], claim=c["claim"],
                             source_url=c["source_url"], result=c["result"],
                             detail=f"[{Path(path).name}] {c['detail']}")
            n += 1
    return {"checks": n, "files": files}


# ------------------------------------------------- verified result updates
def ingest_verified_updates(con: sqlite3.Connection,
                            directory: str | Path | None = None) -> dict[str, Any]:
    """Apply hand-verified results/fixtures from ``data/seed/updates/*.json``.

    Every row must carry its own source_id, source_url and retrieved_utc plus a
    verification block; rows without them are refused (never guessed). Existing
    rows are updated field-by-field (a changed final score files a
    conflicting-results issue via _insert_game); new rows are inserted whole.
    """
    from .store import add_verification
    directory = Path(directory or SEED_DIR / "updates")
    stats = {"files": 0, "applied": 0, "inserted": 0, "updated": 0, "refused": 0}
    if not directory.exists():
        return stats
    for path in sorted(directory.glob("*.json")):
        blob = json.loads(path.read_text())
        stats["files"] += 1
        for u in blob.get("updates", []):
            missing = [k for k in ("game_key", "sport", "source_id", "source_url",
                                   "retrieved_utc") if not u.get(k)]
            if missing:
                stats["refused"] += 1
                add_issue(con, severity="error", area="ingest", sport=u.get("sport"),
                          game_key=u.get("game_key"),
                          detail=f"verified-update refused from {path.name}: "
                                 f"missing {missing} (never guess provenance)")
                continue
            prev = con.execute("SELECT * FROM games WHERE game_key=?",
                               (u["game_key"],)).fetchone()
            row = dict(prev) if prev is not None else {}
            row.update({k: v for k, v in u.items()
                        if k not in ("verification",) and v is not None})
            row.setdefault("league_game_id", u["game_key"].split(":", 1)[1])
            row.setdefault("season", "")
            row.setdefault("game_type", "REG")
            row.setdefault("neutral", 0)
            row.setdefault("venue", None)
            row.setdefault("week_or_slate", None)
            row.setdefault("away_score", None)
            row.setdefault("home_score", None)
            row.setdefault("overtime", None)
            row.setdefault("start_utc", None)
            row.setdefault("extra_json", None)
            row["verified"] = 1
            row["verify_note"] = (u.get("verify_note")
                                  or f"hand-verified update ({path.name})")
            _insert_game(con, row)
            stats["applied"] += 1
            stats["updated" if prev is not None else "inserted"] += 1
            v = u.get("verification")
            if v:
                add_verification(con, subject=v["subject"], claim=v["claim"],
                                 source_url=v.get("source_url", u["source_url"]),
                                 result=v["result"], detail=v["detail"])
    return stats
