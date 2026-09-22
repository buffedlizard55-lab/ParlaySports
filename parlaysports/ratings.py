"""Point-in-time team ratings: Elo (all sports) + rolling scoring rates.

Look-ahead rules (enforced by construction + tested):
  * ratings for game G use only games with (game_date, game_key) < G's.
  * season rollover regresses toward 1500 BEFORE the season's first game.
  * preseason/exhibition games never move regular-season ratings.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .util import elo_expected

MODEL_VERSION = "elo-v1"

# Per-sport Elo parameters. HFA is in Elo points added to the home team.
ELO_PARAMS = {
    "NFL": {"k": 20.0, "hfa": 55.0, "carry": 0.75, "mov": True},
    "MLB": {"k": 12.0, "hfa": 24.0, "carry": 0.75, "mov": False},
    "NHL": {"k": 16.0, "hfa": 30.0, "carry": 0.75, "mov": False},
    "NBA": {"k": 20.0, "hfa": 60.0, "carry": 0.75, "mov": True},
}

# Game types that count for ratings (regular season + playoffs only).
COUNTED_TYPES = {"REG", "R", "POST", "P", "2", "3", "D", "L", "F", "W"}


def _k_factor(sport: str, base_k: float, margin: int, elo_diff: float) -> float:
    """Margin-of-victory multiplier (NBA/NFL only; 1.0 otherwise)."""
    if not ELO_PARAMS[sport]["mov"]:
        return base_k
    # Standard diminishing-returns MOV multiplier (FiveThirtyEight style).
    mult = math_log1p(abs(margin)) * (2.2 / (abs(elo_diff) * 0.001 + 2.2))
    return base_k * mult


def math_log1p(x: float) -> float:
    import math
    return math.log1p(x)


def compute_elo(con: sqlite3.Connection, sport: str,
                seasons: list[str] | None = None) -> dict[str, Any]:
    """(Re)build the Elo trail for one sport. Returns summary stats."""
    params = ELO_PARAMS[sport]
    q = """SELECT game_key, season, game_type, game_date, away_team, home_team,
                  away_score, home_score, neutral
           FROM games WHERE sport=? AND status='final'
             AND away_score IS NOT NULL AND home_score IS NOT NULL"""
    args: list[Any] = [sport]
    if seasons:
        q += " AND season IN (%s)" % ",".join("?" for _ in seasons)
        args.extend(seasons)
    q += " ORDER BY game_date, game_key"
    games = con.execute(q, args).fetchall()

    con.execute("DELETE FROM ratings WHERE sport=?", (sport,))
    elo: dict[str, float] = {}
    used: dict[str, int] = {}
    current_season: str | None = None
    n = 0
    for g in games:
        season = g["season"]
        if season != current_season:
            # Regress every known team toward 1500 at season rollover.
            for t in list(elo.keys()):
                elo[t] = params["carry"] * elo[t] + (1.0 - params["carry"]) * 1500.0
            current_season = season
        away, home = g["away_team"], g["home_team"]
        ra = elo.get(away, 1500.0)
        rb = elo.get(home, 1500.0)
        hfa = 0.0 if g["neutral"] else params["hfa"]
        # win probability for the HOME team (used by strategies via lookup)
        exp_home = elo_expected(rb + hfa, ra)
        counted = (g["game_type"] or "REG") in COUNTED_TYPES
        if counted:
            a_s, h_s = int(g["away_score"]), int(g["home_score"])
            if h_s > a_s:
                score_home = 1.0
            elif h_s < a_s:
                score_home = 0.0
            else:
                score_home = 0.5
            k = _k_factor(sport, params["k"], abs(h_s - a_s), (rb + hfa) - ra)
            # update from the home-team perspective, then mirror
            new_home = rb + k * (score_home - exp_home)
            new_away = ra - k * (score_home - exp_home)
        else:
            new_home, new_away = rb, ra
        for team, before, after in ((away, ra, new_away), (home, rb, new_home)):
            used[team] = used.get(team, 0) + (1 if counted else 0)
            con.execute(
                """INSERT OR REPLACE INTO ratings(sport, team, season, game_date, game_key,
                   elo_before, elo_after, games_used, model_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sport, team, season, g["game_date"], g["game_key"],
                 round(before, 3), round(after, 3), used[team], MODEL_VERSION),
            )
        elo[away], elo[home] = new_away, new_home
        n += 1
    return {"sport": sport, "games_rated": n, "teams": len(elo)}


def elo_before(con: sqlite3.Connection, sport: str, team: str,
               game_date: str, game_key: str, season: str) -> tuple[float, int]:
    """Latest Elo strictly before (game_date, game_key), with season regression.

    Returns (elo, games_used). Unknown teams start at 1500 with 0 used.
    """
    row = con.execute(
        """SELECT elo_after, games_used, season FROM ratings
           WHERE sport=? AND team=? AND (game_date < ? OR (game_date=? AND game_key < ?))
           ORDER BY game_date DESC, game_key DESC LIMIT 1""",
        (sport, team, game_date, game_date, game_key)).fetchone()
    if row is None:
        return 1500.0, 0
    elo, used, rated_season = float(row["elo_after"]), int(row["games_used"]), row["season"]
    if rated_season != season:
        carry = ELO_PARAMS[sport]["carry"]
        elo = carry * elo + (1.0 - carry) * 1500.0
        used = 0
    return elo, used


def elo_prob(con: sqlite3.Connection, sport: str, away: str, home: str,
             game_date: str, game_key: str, season: str,
             neutral: bool = False) -> dict[str, Any]:
    """Pre-game home/away win probabilities from point-in-time Elo."""
    ra, ua = elo_before(con, sport, away, game_date, game_key, season)
    rb, ub = elo_before(con, sport, home, game_date, game_key, season)
    hfa = 0.0 if neutral else ELO_PARAMS[sport]["hfa"]
    p_home = elo_expected(rb + hfa, ra)
    return {"p_home": p_home, "p_away": 1.0 - p_home,
            "elo_away": ra, "elo_home": rb, "used_away": ua, "used_home": ub,
            "model": "elo-v1"}


# ------------------------------------------------------- rolling scoring
def rolling_rates(con: sqlite3.Connection, sport: str, team: str,
                  game_date: str, game_key: str, season: str,
                  window: int = 10) -> dict[str, Any]:
    """Rolling scored/allowed per game over the last `window` finals before G.

    Also returns season-to-date rates and games counted. Strictly past only.
    UNION ALL keeps both team-side lookups on the (sport, team, game_date)
    indexes.
    """
    rows = con.execute(
        """SELECT game_date, game_key, season AS gseason, away_team, home_team, away_score, home_score
           FROM games WHERE sport=? AND status='final' AND away_team=?
             AND (game_date < ? OR (game_date=? AND game_key < ?))
           UNION ALL
           SELECT game_date, game_key, season AS gseason, away_team, home_team, away_score, home_score
           FROM games WHERE sport=? AND status='final' AND home_team=?
             AND (game_date < ? OR (game_date=? AND game_key < ?))
           ORDER BY game_date DESC, game_key DESC""",
        (sport, team, game_date, game_date, game_key,
         sport, team, game_date, game_date, game_key)).fetchall()
    scored: list[float] = []
    allowed: list[float] = []
    season_scored: list[float] = []
    season_allowed: list[float] = []
    for r in rows:
        in_season = r["gseason"] == season
        if r["away_team"] == team:
            s, a = float(r["away_score"]), float(r["home_score"])
        else:
            s, a = float(r["home_score"]), float(r["away_score"])
        if len(scored) < window:
            scored.append(s)
            allowed.append(a)
        if in_season:
            season_scored.append(s)
            season_allowed.append(a)
    def avg(x: list[float]) -> float | None:
        return sum(x) / len(x) if x else None
    return {
        "n_last": len(scored),
        "scored_last": avg(scored), "allowed_last": avg(allowed),
        "n_season": len(season_scored),
        "scored_season": avg(season_scored), "allowed_season": avg(season_allowed),
    }


def league_average(con: sqlite3.Connection, sport: str, season: str,
                   before_date: str) -> float | None:
    """League-average total (per game) for a season strictly before a date."""
    row = con.execute(
        """SELECT AVG(away_score + home_score) AS avg_total, COUNT(*) AS n
           FROM games WHERE sport=? AND season=? AND status='final'
             AND game_date < ?""",
        (sport, season, before_date)).fetchone()
    if row is None or (row["n"] or 0) < 10:
        return None
    return float(row["avg_total"])
