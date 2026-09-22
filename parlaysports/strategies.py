"""Versioned strategy catalog + signal generators.

Every strategy is a testable hypothesis with explicit selection rules, markets,
required data and limitations. Signal functions are pure given (game, prices,
ratings-as-of): they only read information dated strictly before tipoff.

Conventions:
  * model_prob for ML legs: Elo (NFL/NHL/NBA) or log5 blend (MLB).
  * spread cover probs: empirical market mapping from PRIOR seasons only
    (P(cover | ML-prob bucket, fav/dog)). Fallback: Elo-margin Normal model,
    recorded in features_json['cover_method'].
  * totals probs: Normal(model_total, sport_sigma); sigma is computed from
    prior-season game totals at seed time and stored in meta (never asserted).
  * edge = model_prob - market_prob (devigged two-way when both sides priced).
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .ratings import elo_prob, league_average, rolling_rates
from .util import american_to_prob, log5, prob_to_american, pythag

VERSION = "v1"

# ------------------------------------------------------------------ catalog
STRATEGIES: list[dict[str, Any]] = [
    # ------------------------------------------------------------- NFL
    {"strategy_id": "S-NFL-01", "version": VERSION, "username": "GridironGuru",
     "name": "Elo edge favorites", "sport": "NFL", "category": "ratings-vs-market",
     "hypothesis": "Pre-game Elo implies fair ML prices; short favorites where Elo exceeds the market by >=4pp win more often than the price implies.",
     "markets": "ML", "selection_rules": "ML leg on the side with edge>=0.04 and american<=-110 (favorite only).",
     "construction_rules": "2-3 legs, distinct games, top edge first.",
     "required_data": "nflverse scores+lines, Elo trail", "min_edge": 0.04,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Closing-line backtest (no CLV). Elo slow to price QB injuries."},
    {"strategy_id": "S-NFL-02", "version": VERSION, "username": "DivisionalDog",
     "name": "Divisional home dogs", "sport": "NFL", "category": "situational",
     "hypothesis": "Familiarity compresses divisional margins; home divisional underdogs cover at an exploitable rate.",
     "markets": "SPREAD", "selection_rules": "Home team, div_game=1, home spread line>0 (dog).",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "nflverse lines + div flag", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "No edge filter in v1; evaluated on cover rate vs -110 breakeven."},
    {"strategy_id": "S-NFL-03", "version": VERSION, "username": "Totalician",
     "name": "Efficiency totals", "sport": "NFL", "category": "totals",
     "hypothesis": "Blended rolling offense/defense scoring rates predict game totals better than the market total by >=3 points often enough to clear the hold.",
     "markets": "TOTAL", "selection_rules": "|model_total - line| >= 3.0 with >=6 rolling games per side.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "rolling scoring, league averages, totals sigma", "min_edge": 0.03,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Normal-totals assumption; weather handled by S-NFL-05, not here."},
    {"strategy_id": "S-NFL-04", "version": VERSION, "username": "RestEdge",
     "name": "Rest mismatch", "sport": "NFL", "category": "situational",
     "hypothesis": "A rest differential of >=3 days (mini-bye, bye, MNF short week) is underpriced in the spread.",
     "markets": "SPREAD",
     "selection_rules": "|away_rest - home_rest| >= 3; take the more-rested side's spread.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "nflverse rest columns", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Rest correlates with team quality late season; no opponent adjustment."},
    {"strategy_id": "S-NFL-05", "version": VERSION, "username": "WindChill",
     "name": "Wind chill under", "sport": "NFL", "category": "weather totals",
     "hypothesis": "Outdoor cold/high-wind games suppress scoring beyond what the total implies.",
     "markets": "TOTAL",
     "selection_rules": "roof in (outdoors, open, retractable-open...) AND (wind>=15 OR temp<=32). Under only. Missing weather => no signal.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "nflverse roof/temp/wind", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Weather snapshots are pre-game day level; dome-closure handling unknown for retractable roofs."},
    {"strategy_id": "S-NFL-06", "version": VERSION, "username": "ShortChalkFade",
     "name": "Short road chalk fade", "sport": "NFL", "category": "contrarian",
     "hypothesis": "Short road favorites (<=3) are overbet; the home side covers at value.",
     "markets": "SPREAD",
     "selection_rules": "Away line in [-3, 0) i.e. road favorite by <=3; take home spread.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "nflverse spreads", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "No handle data; 'overbet' is narrative, evaluated purely on cover rate."},
    # ------------------------------------------------------------- MLB
    {"strategy_id": "S-MLB-01", "version": VERSION, "username": "Pythagoras",
     "name": "Pythagorean log5", "sport": "MLB", "category": "ratings-vs-market",
     "hypothesis": "log5 of blended Pythagorean strength (season + recent + HFA) is a calibrated favorite/longshot-agnostic baseline; MODEL-grade legs.",
     "markets": "ML", "selection_rules": "Edge vs model-implied fair >= 0.05 using log5 blend. MODEL-priced (no market odds feed).",
     "construction_rules": "2-4 legs, distinct games.",
     "required_data": "2015-25 results; 2026 standings snapshot", "min_edge": 0.05,
     "stake": 10.0, "max_legs": 4, "status": "active",
     "limitations": "No market odds: cannot verify edge vs close; starting pitchers not modeled."},
    {"strategy_id": "S-MLB-02", "version": VERSION, "username": "StreakSmarter",
     "name": "Form streaks", "sport": "MLB", "category": "form",
     "hypothesis": "Teams on W3+ with strong L10 keep winning short-term vs cold opponents (or the market overreacts; either way the signal is tested).",
     "markets": "ML", "selection_rules": "Side on W3+ streak and L10>=.600 vs opp L10<=.400.",
     "construction_rules": "2-4 legs, distinct games.",
     "required_data": "standings snapshot (2026) / rolling L10 (history)", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 4, "status": "active",
     "limitations": "Streak data for 2026 comes from standings snapshot, not game log."},
    {"strategy_id": "S-MLB-03", "version": VERSION, "username": "RegressionRandy",
     "name": "xW-Luck fade", "sport": "MLB", "category": "regression",
     "hypothesis": "Teams underperforming their expected record (xW% - W% >= .050) are undervalued as underdogs.",
     "markets": "ML", "selection_rules": "(xW%-W%) >= .050 for the dog side (model prob < .5).",
     "construction_rules": "2-4 legs, distinct games.",
     "required_data": "xW snapshot (2026) / Pythag (history)", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 4, "status": "active",
     "limitations": "xW for history is Pythag-based, not Statcast xW."},
    {"strategy_id": "S-MLB-04", "version": VERSION, "username": "HomeCookin",
     "name": "Home/road splits", "sport": "MLB", "category": "situational",
     "hypothesis": "Extreme home/road split differentials (>=.200 combined edge, min 20 games) persist.",
     "markets": "ML", "selection_rules": "home_home% - away_away% >= .200 (min 20 games each).",
     "construction_rules": "2-4 legs, distinct games.",
     "required_data": "home/away splits", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 4, "status": "active",
     "limitations": "Small-sample early season; park effects not separated."},
    {"strategy_id": "S-MLB-05", "version": VERSION, "username": "CoorsTotal",
     "name": "Park-aware totals", "sport": "MLB", "category": "totals",
     "hypothesis": "Blended runs model + park factor identifies totals edges >=1.5 runs. MODEL line AND MODEL price (no market totals feed).",
     "markets": "TOTAL", "selection_rules": "|model_total - 8.8 (league proxy)| ... uses park factor & blended rates; edge>=1.5 runs.",
     "construction_rules": "2-4 legs, distinct games.",
     "required_data": "runs data, park factors (computed 2015-25)", "min_edge": 0.03,
     "stake": 10.0, "max_legs": 4, "status": "active",
     "limitations": "League-average proxy line (no market line): legs are MODEL-grade by construction."},
    {"strategy_id": "S-MLB-06", "version": VERSION, "username": "ScoreboardWatcher",
     "name": "September motivation", "sport": "MLB", "category": "situational",
     "hypothesis": "In September, teams within 5 games of a playoff spot outperform eliminated teams head-to-head.",
     "markets": "ML", "selection_rules": "September games only; chaser (WC/div race <=5 GB or clinched contender) vs eliminated (E) side.",
     "construction_rules": "2-4 legs, distinct games.",
     "required_data": "standings race snapshot", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 4, "status": "active",
     "limitations": "2026-only signal (no historical race snapshots); forward-test only."},
    # ------------------------------------------------------------- NHL
    {"strategy_id": "S-NHL-01", "version": VERSION, "username": "PuckElo",
     "name": "Puck Elo edge", "sport": "NHL", "category": "ratings-vs-market",
     "hypothesis": "Elo edges >=4pp vs Kalshi GAME prices convert to ML value.",
     "markets": "ML", "selection_rules": "edge>=0.04 vs Kalshi close (backtest) or live/model (forward REG only, no preseason).",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "NHL scores, Kalshi closes", "min_edge": 0.04,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Goalie confirmations not modeled; OT/shootout included in ML (documented)."},
    {"strategy_id": "S-NHL-02", "version": VERSION, "username": "HomeIceQ",
     "name": "Home ice + rest", "sport": "NHL", "category": "situational",
     "hypothesis": "Rested home favorites (opp on B2B or 3-in-4) win at value.",
     "markets": "ML", "selection_rules": "Home side where away team played yesterday (B2B) or 3 games in 4 nights.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "schedule-derived rest", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Travel distance/time zones not modeled."},
    {"strategy_id": "S-NHL-03", "version": VERSION, "username": "CreaseCrash",
     "name": "Goals-model totals", "sport": "NHL", "category": "totals",
     "hypothesis": "Blended GF/GA rates predict totals; edge>=0.75 goals vs 5.5/6.5 market or model line.",
     "markets": "TOTAL", "selection_rules": "|model_total - line| >= 0.75 with >=8 rolling games per side.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "GF/GA logs, totals sigma", "min_edge": 0.03,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Starting goalies not in v1; empty-net randomness is irreducible."},
    {"strategy_id": "S-NHL-04", "version": VERSION, "username": "BackToBacker",
     "name": "B2B fade", "sport": "NHL", "category": "situational",
     "hypothesis": "Teams on the second night of a back-to-back underperform the moneyline.",
     "markets": "ML", "selection_rules": "Fade (bet opponent of) any team playing its 2nd game in 2 nights.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "schedule-derived rest", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Backup-goalie starts correlate with B2B; not separated."},
    {"strategy_id": "S-NHL-05", "version": VERSION, "username": "PucklinePete",
     "name": "Puck-line value", "sport": "NHL", "category": "spread mapping",
     "hypothesis": "Big favorites' -1.5 puck lines are systematically mispriced vs empirical cover mapping.",
     "markets": "SPREAD", "selection_rules": "Model ML prob >= .62; -1.5 leg where empirical cover edge >= .04.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "Kalshi GAME + SPREAD history, cover mapping", "min_edge": 0.04,
     "stake": 10.0, "max_legs": 3, "status": "standby",
     "limitations": "STANDBY: no verified puck-line history in v1 (Kalshi SPREAD closes lack strikes in the inherited extract; only live quotes carry lines). Armed for forward tickets once nightly DK/Kalshi puck-line snapshots arrive. Empty-net goals decide many -1.5 covers; high variance."},
    {"strategy_id": "S-NHL-06", "version": VERSION, "username": "MuddyWater",
     "name": "Road dog process", "sport": "NHL", "category": "contrarian",
     "hypothesis": "Road underdogs with better L10 goal-share than the home favorite win at value.",
     "markets": "ML", "selection_rules": "Away dog (model prob < .5) with L10 GF% > opp L10 GF% by >= .05.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "goal logs", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "GF% is score-effected; no score-adjustment in v1."},
    # ------------------------------------------------------------- NBA
    {"strategy_id": "S-NBA-01", "version": VERSION, "username": "EloAndOrder",
     "name": "Elo vs close", "sport": "NBA", "category": "ratings-vs-market",
     "hypothesis": "Elo edges >=4pp vs closing ML/spread convert (SBR Oct-Dec backtest; forward on ESPN lines).",
     "markets": "ML,SPREAD", "selection_rules": "edge>=0.04 on ML; spread follows strong ML conviction (>=.60) via cover mapping.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "scores, SBR closes, ESPN lines", "min_edge": 0.04,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Backtest window is Oct-Dec only (SBR coverage). Injuries not modeled."},
    {"strategy_id": "S-NBA-02", "version": VERSION, "username": "LoadManager",
     "name": "Schedule-spot fade", "sport": "NBA", "category": "situational",
     "hypothesis": "B2B and 3-in-4 teams underperform the spread, especially on the road.",
     "markets": "SPREAD", "selection_rules": "Fade schedule-spot team (opp spread) when they are on B2B or 3-in-4.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "schedule-derived rest, spreads", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Load-management DNPs are the mechanism; not directly observed."},
    {"strategy_id": "S-NBA-03", "version": VERSION, "username": "PaceCase",
     "name": "Pace totals", "sport": "NBA", "category": "totals",
     "hypothesis": "Offensive/defensive efficiency blends predict totals; edge>=6 pts vs line.",
     "markets": "TOTAL", "selection_rules": "|model_total - line| >= 6.0 with >=6 rolling games per side.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "scoring logs, totals sigma", "min_edge": 0.03,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "No pace/possession inputs in v1 (scores only); injuries not modeled."},
    {"strategy_id": "S-NBA-04", "version": VERSION, "username": "HomeCourt",
     "name": "Elite home cover", "sport": "NBA", "category": "situational",
     "hypothesis": "Elite home teams (home W%>=.700, min 10) keep covering at home.",
     "markets": "SPREAD", "selection_rules": "home W%>=.700 (min 10 home games); take home spread.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "home/road splits", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Survivorship: threshold selects good teams; spread should adjust."},
    {"strategy_id": "S-NBA-05", "version": VERSION, "username": "MeanReversion",
     "name": "ATS streak fade", "sport": "NBA", "category": "contrarian",
     "hypothesis": "Teams on 4+ ATS win streaks are overpriced; fade the streak.",
     "markets": "SPREAD", "selection_rules": "Team covered 4+ straight (spread history required); bet opponent spread.",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "spread + score history", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Needs spread history: SBR window only for backtests."},
    {"strategy_id": "S-NBA-06", "version": VERSION, "username": "RoadDawg",
     "name": "Rested road dogs", "sport": "NBA", "category": "contrarian",
     "hypothesis": "Big rested road dogs (+6 or more, opp on short rest or them off 2+ days) cover at value.",
     "markets": "SPREAD", "selection_rules": "Away line >= +6.0 AND (away rest>=2 days OR home on B2B).",
     "construction_rules": "2-3 legs, distinct games.",
     "required_data": "spreads, schedule rest", "min_edge": 0.0,
     "stake": 10.0, "max_legs": 3, "status": "active",
     "limitations": "Tank/revenge narratives excluded; pure spread-vs-spot test."},
    # ----------------------------------------------------------- MULTI
    {"strategy_id": "S-MULTI-01", "version": VERSION, "username": "FourSportFavor",
     "name": "Cross-sport favorites", "sport": "MULTI", "category": "multi-sport",
     "hypothesis": "The best ML edge in each in-season sport compounds into a positive-expectation cross-sport parlay.",
     "markets": "ML", "selection_rules": "Top model edge per in-season sport (edge>=.03 on market legs; MODEL legs admitted at model_prob>=.60); 2-4 legs across >=2 sports.",
     "construction_rules": "1 leg per sport max, >=2 sports required.",
     "required_data": "all single-sport models", "min_edge": 0.03,
     "stake": 10.0, "max_legs": 4, "max_per_sport": 1, "status": "active",
     "limitations": "Backtest covers cross-sport overlap slates only (dates where >=2 per-sport backtest windows hold finals; see R-017); forward covers every in-season slate. Independence assumed across legs."},
    {"strategy_id": "S-MULTI-02", "version": VERSION, "username": "CrossSportEdge",
     "name": "Best edges any sport", "sport": "MULTI", "category": "multi-sport",
     "hypothesis": "Pooling the highest-edge legs across sports beats single-sport concentration.",
     "markets": "ML,SPREAD,TOTAL", "selection_rules": "Highest-edge legs across in-season sports (edge>=.03 on market legs; MODEL legs admitted at model_prob>=.60); 2-4 legs.",
     "construction_rules": "Distinct games; any sport mix.",
     "required_data": "all single-sport models", "min_edge": 0.03,
     "stake": 10.0, "max_legs": 4, "status": "active",
     "limitations": "Backtest covers cross-sport overlap slates only (dates where >=2 per-sport backtest windows hold finals; see R-017). Independence assumed across legs."},
    {"strategy_id": "S-MULTI-03", "version": VERSION, "username": "TotalChaos",
     "name": "Cross-sport totals", "sport": "MULTI", "category": "multi-sport",
     "hypothesis": "Totals edges in different sports are uncorrelated; parlaying them harvests multiple edges.",
     "markets": "TOTAL", "selection_rules": "Top totals edges across sports (edge>=.03 on market legs; MODEL legs admitted at model_prob>=.60); 2-4 legs.",
     "construction_rules": "Distinct games; totals only.",
     "required_data": "totals models", "min_edge": 0.03,
     "stake": 10.0, "max_legs": 4, "status": "active",
     "limitations": "Backtest covers cross-sport overlap slates only (dates where >=2 per-sport backtest windows hold finals; see R-017). Weather/pace correlation within a day is ignored."},
    {"strategy_id": "S-MULTI-04", "version": VERSION, "username": "LotteryTicket",
     "name": "Longshot lottery", "sport": "MULTI", "category": "multi-sport",
     "hypothesis": "Small-stake up-to-8-leg longshots on plus-money legs are a bounded entertainment book, tracked separately.",
     "markets": "ML,SPREAD,TOTAL", "selection_rules": "Prefer plus-money legs with model edge; up to 8 legs; max 1 ticket per week.",
     "construction_rules": "Up to 8 legs (0.2% min-prob gate may shorten), distinct games, $25 stake.",
     "required_data": "all single-sport models", "min_edge": 0.0,
     "stake": 25.0, "max_legs": 8, "status": "active",
     "limitations": "Backtest covers cross-sport overlap slates only, max 1 ticket per ISO week in both books (see R-017). Negative expectation expected; sized at 0.25%. Never chased."},
]

CATALOG_BY_ID = {s["strategy_id"]: s for s in STRATEGIES}


def install_catalog(con: sqlite3.Connection) -> int:
    from .store import add_issue
    n = 0
    for s in STRATEGIES:
        prev = con.execute(
            "SELECT * FROM strategies WHERE strategy_id=? AND version=?",
            (s["strategy_id"], s["version"])).fetchone()
        if prev is not None:
            changed = [k for k in ("name", "sport", "hypothesis", "markets",
                                   "selection_rules", "construction_rules",
                                   "required_data", "min_edge", "stake", "max_legs")
                       if str(prev[k]) != str(s.get(k))]
            if changed:
                # Versioned rules are immutable: a changed definition must get a
                # new version or every historical result becomes irreproducible.
                add_issue(con, severity="error", area="strategies",
                          sport=s["sport"],
                          detail=(f"{s['strategy_id']} {s['version']}: catalog "
                                  f"definition would modify fields {changed}; "
                                  f"refusing to overwrite — bump the version"))
                continue
        con.execute(
            """INSERT OR REPLACE INTO strategies(strategy_id, version, username, name, sport,
               category, hypothesis, markets, selection_rules, construction_rules,
               required_data, min_edge, stake, max_legs, status, created_utc,
               limitations, lineage)
               VALUES (:strategy_id, :version, :username, :name, :sport, :category,
               :hypothesis, :markets, :selection_rules, :construction_rules,
               :required_data, :min_edge, :stake, :max_legs, :status, :created_utc,
               :limitations, :lineage)""",
            {**s, "created_utc": "2026-09-22T02:00:00Z", "lineage": None},
        )
        n += 1
    return n


# ------------------------------------------------------------- price lookup
def get_prices(con: sqlite3.Connection, game_key: str,
               close_only: bool = False) -> dict[tuple[str, str], dict[str, Any]]:
    """Best price per (market, selection). Backtests prefer close_flag=1."""
    rows = con.execute(
        "SELECT * FROM prices WHERE game_key=? ORDER BY close_flag DESC, observed_utc DESC",
        (game_key,)).fetchall()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if close_only and not r["close_flag"]:
            continue
        key = (r["market"], r["selection"])
        if key not in out:
            out[key] = dict(r)
    return out


def devigged_probs(prices: dict[tuple[str, str], dict[str, Any]],
                   market: str, a: str, b: str) -> tuple[float | None, float | None]:
    """Devigged two-way market probs, or (None, None) if a side is missing."""
    pa, pb = prices.get((market, a)), prices.get((market, b))
    if pa is None or pb is None:
        return None, None
    if pa["odds_american"] is None or pb["odds_american"] is None:
        return None, None
    from .util import american_to_prob, devig_two_way
    return devig_two_way(american_to_prob(pa["odds_american"]),
                         american_to_prob(pb["odds_american"]))


def totals_sigma(con: sqlite3.Connection, sport: str) -> float:
    row = con.execute("SELECT value FROM meta WHERE key=?",
                      (f"TOTALS_SIGMA_{sport}",)).fetchone()
    if row is None:
        raise KeyError(f"totals sigma for {sport} not computed (run seed)")
    return float(row["value"])


def norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ------------------------------------------------- spread cover mapping
# Memoized per (sport, season, side, bucket): the mapping is static within a
# run because it only reads strictly-prior seasons. Cleared between runs.
_COVER_CACHE: dict[tuple, float | None] = {}


def cover_mapping(con: sqlite3.Connection, sport: str, before_season: str,
                  side_kind: str, ml_prob: float) -> float | None:
    """Empirical P(cover) from PRIOR seasons: bucket by ML prob and fav/dog.

    side_kind: 'fav' (laying points) or 'dog' (taking points). Buckets of 5pp
    over ML win prob. Minimum 30 samples or returns None (no signal).
    """
    bucket = math.floor(ml_prob * 20) / 20
    cache_key = (sport, before_season, side_kind, bucket)
    if cache_key in _COVER_CACHE:
        return _COVER_CACHE[cache_key]
    result = _cover_mapping_uncached(con, sport, before_season, side_kind,
                                     bucket, bucket + 0.05)
    _COVER_CACHE[cache_key] = result
    return result


def clear_cover_cache() -> None:
    _COVER_CACHE.clear()


def _cover_mapping_uncached(con, sport, before_season, side_kind,
                            lo: float, hi: float) -> float | None:
    season_row = con.execute(
        "SELECT 1 FROM games WHERE sport=? AND season < ? LIMIT 1",
        (sport, before_season)).fetchone()
    if not season_row:
        return None
    # NOTE: bucket filter on ML prob applied in Python (needs american_to_prob).
    rows = con.execute(
        """SELECT p.selection, p.line, m.odds_american AS ml,
                  g.home_score, g.away_score
           FROM prices p JOIN games g ON p.game_key = g.game_key
           JOIN prices m ON m.game_key = g.game_key AND m.market='ML'
                        AND m.selection = p.selection
           WHERE g.sport=? AND g.season < ? AND g.status='final'
             AND p.market='SPREAD' AND p.close_flag=1 AND m.close_flag=1
             AND ((?='fav' AND p.line < 0) OR (?='dog' AND p.line > 0))
             AND m.odds_american IS NOT NULL""",
        (sport, before_season, side_kind, side_kind)).fetchall()
    n = c = 0
    for r in rows:
        try:
            p = american_to_prob(r["ml"])
        except ValueError:
            continue
        if not (lo <= p < hi):
            continue
        if r["selection"] == "home":
            margin = (r["home_score"] + r["line"]) - r["away_score"]
        else:
            margin = (r["away_score"] + r["line"]) - r["home_score"]
        if margin == 0:
            continue  # pushes are excluded from the cover denominator
        n += 1
        if margin > 0:
            c += 1
    if n < 30:
        return None
    return c / n


# ------------------------------------------------------------- game helpers
def _extra(game: sqlite3.Row) -> dict[str, Any]:
    try:
        return json.loads(game["extra_json"]) if game["extra_json"] else {}
    except (ValueError, TypeError):
        return {}


def prev_final(con: sqlite3.Connection, sport: str, team: str,
               game_date: str, game_key: str) -> dict[str, Any] | None:
    row = con.execute(
        """SELECT game_date FROM games WHERE sport=? AND status='final'
           AND (away_team=? OR home_team=?) AND game_date < ?
           ORDER BY game_date DESC LIMIT 1""",
        (sport, team, team, game_date)).fetchone()
    return dict(row) if row else None


def _team_dates(con: sqlite3.Connection, sport: str, team: str,
                start_excl: str | None, end_excl: str, end_key: str | None = None,
                statuses: tuple[str, ...] = ("final",),
                limit: int | None = None) -> list[sqlite3.Row]:
    """Team's games in a date window from both sides of the boxscore.

    UNION ALL keeps both legs on the (sport, team, game_date) indexes; an
    OR predicate would table-scan. All boundaries are strict/exclusive on the
    (game_date, game_key) decision point so nothing at-or-after the game can
    leak in. Statuses are matched exactly as given.
    """
    st_list = ",".join("?" for _ in statuses)
    key_clause = " AND (game_date < ? OR (game_date = ? AND game_key < ?))" if end_key else " AND game_date < ?"
    args_tail: list[Any] = ([start_excl] if start_excl else [])
    if end_key:
        args_tail += [end_excl, end_excl, end_key]
    else:
        args_tail += [end_excl]
    q = f"""SELECT game_date, game_key, season, away_team, home_team,
                   away_score, home_score FROM games
            WHERE sport=? AND away_team=? AND status IN ({st_list})
              {"AND game_date >= ?" if start_excl else ""}{key_clause}
            UNION ALL
            SELECT game_date, game_key, season, away_team, home_team,
                   away_score, home_score FROM games
            WHERE sport=? AND home_team=? AND status IN ({st_list})
              {"AND game_date >= ?" if start_excl else ""}{key_clause}
            ORDER BY game_date DESC, game_key DESC"""
    head = [sport, team, *statuses, *args_tail]
    tail = [sport, team, *statuses, *args_tail]
    sql_args = [*head, *tail]
    if limit:
        q += " LIMIT ?"
        sql_args.append(limit)
    return con.execute(q, sql_args).fetchall()


def games_in_last_n_days(con: sqlite3.Connection, sport: str, team: str,
                         game_date: str, n: int) -> int:
    start = (datetime.strptime(game_date, "%Y-%m-%d") - timedelta(days=n)).strftime("%Y-%m-%d")
    rows = _team_dates(con, sport, team, start_excl=start, end_excl=game_date,
                       statuses=("final", "scheduled", "live"))
    return len(rows)


def is_b2b(con: sqlite3.Connection, sport: str, team: str, game_date: str) -> bool:
    rows = _team_dates(con, sport, team, start_excl=None, end_excl=game_date,
                       statuses=("final", "scheduled", "live"), limit=1)
    if not rows:
        return False
    d = datetime.strptime(game_date, "%Y-%m-%d")
    p = datetime.strptime(rows[0]["game_date"], "%Y-%m-%d")
    return (d - p).days == 1


def home_away_split(con: sqlite3.Connection, sport: str, team: str,
                    game_date: str, game_key: str, season: str) -> dict[str, Any]:
    rows = _team_dates(con, sport, team, start_excl=None, end_excl=game_date,
                       end_key=game_key)
    hw = hl = aw = al = 0
    for r in rows:
        if r["season"] != season:
            continue
        home_win = r["home_score"] > r["away_score"]
        if r["home_team"] == team:
            hw, hl = hw + (1 if home_win else 0), hl + (0 if home_win else 1)
        else:
            aw, al = aw + (0 if home_win else 1), al + (1 if home_win else 0)
    return {"home_w": hw, "home_l": hl, "away_w": aw, "away_l": al}


def last_n_form(con: sqlite3.Connection, sport: str, team: str,
                game_date: str, game_key: str, n: int = 10) -> dict[str, Any]:
    rows = _team_dates(con, sport, team, start_excl=None, end_excl=game_date,
                       end_key=game_key, limit=n)
    w = sum(1 for r in rows
            if (r["home_team"] == team and r["home_score"] > r["away_score"])
            or (r["away_team"] == team and r["away_score"] > r["home_score"]))
    streak, stype = 0, None
    for r in rows:
        won = ((r["home_team"] == team and r["home_score"] > r["away_score"])
               or (r["away_team"] == team and r["away_score"] > r["home_score"]))
        if stype is None:
            stype, streak = ("W" if won else "L"), 1
        elif ("W" if won else "L") == stype:
            streak += 1
        else:
            break
    return {"w": w, "l": len(rows) - w, "n": len(rows),
            "pct": (w / len(rows)) if rows else None,
            "streak": f"{stype}{streak}" if stype else None, "streak_n": streak,
            "streak_type": stype}


def ats_streak(con: sqlite3.Connection, sport: str, team: str,
               game_date: str, game_key: str) -> dict[str, Any]:
    """Consecutive covers (spread, close lines) ending before this game."""
    rows = con.execute(
        """SELECT g.game_date, g.game_key, g.away_team, g.home_team, g.away_score,
                  g.home_score, p.selection, p.line
           FROM games g JOIN prices p ON p.game_key=g.game_key
           WHERE g.sport=? AND g.status='final' AND p.market='SPREAD' AND p.close_flag=1
             AND (g.away_team=? OR g.home_team=?)
             AND (g.game_date < ? OR (g.game_date=? AND g.game_key < ?))
           ORDER BY g.game_date DESC, g.game_key DESC""",
        (sport, team, team, game_date, game_date, game_key)).fetchall()
    # one row per team-game: keep the row matching `team`
    seen: set[str] = set()
    covers = 0
    for r in rows:
        side = "home" if r["home_team"] == team else "away"
        if r["selection"] != side:
            continue
        key = f"{r['game_date']}{side}"
        if key in seen:
            continue
        seen.add(key)
        if side == "home":
            covered = (r["home_score"] + r["line"]) > r["away_score"]
            push = (r["home_score"] + r["line"]) == r["away_score"]
        else:
            covered = (r["away_score"] + r["line"]) > r["home_score"]
            push = (r["away_score"] + r["line"]) == r["home_score"]
        if push:
            continue
        if covered:
            covers += 1
        else:
            break
    return {"covers_streak": covers}


# ------------------------------------------------------------- MLB model
def mlb_team_strength(con: sqlite3.Connection, team: str, game_date: str,
                      game_key: str, season: str) -> dict[str, Any]:
    """Blended strength for MLB: season Pythag (60%) + last-20 Pythag (25%) + L10 win% (15%).

    2026 forward games use the verified standings snapshot (no 2026 game log yet).
    Historical games use the rolling game log. Sources recorded in `basis`.
    """
    if season == "2026":
        row = con.execute(
            """SELECT w, l, rs, ra, last10_w, last10_l, home_w, home_l, away_w, away_l,
                      xw, xl, streak_code, streak_n, as_of_utc
               FROM team_form WHERE sport='MLB' AND team=? AND season='2026'
               ORDER BY as_of_utc DESC LIMIT 1""", (team,)).fetchone()
        if row is None or row["rs"] is None:
            return {"p": None, "basis": "no-2026-snapshot"}
        season_py = pythag(row["rs"], row["ra"])
        l10 = (row["last10_w"] / (row["last10_w"] + row["last10_l"])
               if (row["last10_w"] or 0) + (row["last10_l"] or 0) > 0 else season_py)
        p = 0.70 * season_py + 0.30 * l10
        return {"p": p, "basis": "standings-snapshot-2026", "season_py": season_py,
                "l10": l10, "w": row["w"], "l": row["l"],
                "home": (row["home_w"], row["home_l"]),
                "away": (row["away_w"], row["away_l"]),
                "xw": row["xw"], "xl": row["xl"],
                "streak": row["streak_code"], "as_of": row["as_of_utc"]}
    rr = rolling_rates(con, "MLB", team, game_date, game_key, season, window=20)
    if rr["n_season"] is None or (rr["n_season"] or 0) < 10:
        return {"p": None, "basis": "insufficient-history"}
    season_py = pythag(rr["scored_season"] * rr["n_season"], rr["allowed_season"] * rr["n_season"])
    recent_py = season_py
    if (rr["n_last"] or 0) >= 10:
        recent_py = pythag(rr["scored_last"] * rr["n_last"], rr["allowed_last"] * rr["n_last"])
    form = last_n_form(con, "MLB", team, game_date, game_key, 10)
    l10 = form["pct"] if form["pct"] is not None else season_py
    return {"p": 0.60 * season_py + 0.25 * recent_py + 0.15 * l10,
            "basis": "rolling-log", "season_py": season_py,
            "recent_py": recent_py, "l10": l10}


def mlb_prob(con: sqlite3.Connection, away: str, home: str, game_date: str,
             game_key: str, season: str) -> dict[str, Any]:
    """Home win prob via log5(blended strengths) with +HFA to home strength."""
    sa = mlb_team_strength(con, away, game_date, game_key, season)
    sh = mlb_team_strength(con, home, game_date, game_key, season)
    if sa["p"] is None or sh["p"] is None:
        return {"p_home": None, "basis": "missing-strength"}
    # Home-field advantage as a strength bump (documented assumption: +0.020).
    p_home = log5(min(sh["p"] + 0.020, 0.95), sa["p"])
    return {"p_home": p_home, "p_away": 1.0 - p_home, "model": "mlb-log5-v1",
            "away": sa, "home": sh}


def park_factor(con: sqlite3.Connection, team: str) -> float | None:
    row = con.execute("SELECT value FROM meta WHERE key=?",
                      (f"PARK_{team}",)).fetchone()
    return float(row["value"]) if row else None


# ------------------------------------------------------- signal generators
Signal = dict[str, Any]


def _base_signal(strategy_id: str, game: sqlite3.Row, market: str, selection: str,
                 decision_utc: str) -> Signal:
    return {
        "strategy_id": strategy_id, "version": VERSION,
        "game_key": game["game_key"], "sport": game["sport"],
        "market": market, "selection": selection, "line": None,
        "model_prob": None, "market_prob": None, "edge": None,
        "decision_utc": decision_utc, "features": {}, "price_id": None,
        "odds_american": None, "odds_type": None, "note": "",
    }


def _attach_price(sig: Signal,
                  prices: dict[tuple[str, str], dict[str, Any]]) -> Signal:
    pr = prices.get((sig["market"], sig["selection"]))
    if pr is None:
        sig["note"] = "no price for this market/selection (UNPRICED leg)"
        return sig
    sig["price_id"] = pr["price_id"]
    sig["odds_american"] = pr["odds_american"]
    sig["odds_type"] = pr["odds_type"]
    if sig["line"] is None:
        sig["line"] = pr["line"]
    sig["features"]["price_source"] = pr["source_id"]
    sig["features"]["price_observed_utc"] = pr["observed_utc"]
    return sig


def _model_price_ml(sig: Signal, p: float, fair_american: float) -> Signal:
    sig["model_prob"] = round(p, 4)
    sig["odds_american"] = round(fair_american, 1)
    sig["odds_type"] = "model_fair"
    sig["features"]["price_source"] = "SRC_MODEL"
    return sig


# ------------------------------------------------------------- NFL signals
def sig_nfl_01(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if game["game_type"] not in ("REG", "POST"):
        return []
    ep = elo_prob(con, "NFL", game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"],
                  bool(game["neutral"]))
    mp_a, mp_h = devigged_probs(prices, "ML", "away", "home")
    if mp_a is None:
        return []
    out = []
    for side, model_p, mkt_p in (("away", ep["p_away"], mp_a), ("home", ep["p_home"], mp_h)):
        pr = prices.get(("ML", side))
        if pr is None or pr["odds_american"] is None or pr["odds_american"] > -110:
            continue  # favorites only
        edge = model_p - mkt_p
        if edge >= 0.04:
            s = _base_signal("S-NFL-01", game, "ML", side, decision_utc)
            s.update(model_prob=round(model_p, 4), market_prob=round(mkt_p, 4),
                     edge=round(edge, 4))
            s["features"] = {"elo_away": round(ep["elo_away"], 1),
                             "elo_home": round(ep["elo_home"], 1)}
            out.append(_attach_price(s, prices))
    return out


def sig_nfl_02(con, game, prices, decision_utc, close_only) -> list[Signal]:
    ex = _extra(game)
    if game["game_type"] != "REG" or ex.get("div_game") != 1:
        return []
    pr = prices.get(("SPREAD", "home"))
    if pr is None or pr["line"] is None or pr["line"] <= 0:
        return []
    mp_a, mp_h = devigged_probs(prices, "SPREAD", "away", "home")
    cov = cover_mapping(con, "NFL", game["season"], "dog",
                        1.0 - (mp_h or 0.5)) if mp_h else None
    s = _base_signal("S-NFL-02", game, "SPREAD", "home", decision_utc)
    s.update(model_prob=round(cov, 4) if cov else None,
             market_prob=round(mp_h, 4) if mp_h else None,
             edge=round(cov - mp_h, 4) if (cov and mp_h) else None)
    s["features"] = {"div_game": 1, "cover_method": "empirical-map" if cov else "none"}
    return [_attach_price(s, prices)]


def _model_total_nfl(con, game) -> dict[str, Any] | None:
    season, gd, gk = game["season"], game["game_date"], game["game_key"]
    ra = rolling_rates(con, "NFL", game["away_team"], gd, gk, season, 8)
    rh = rolling_rates(con, "NFL", game["home_team"], gd, gk, season, 8)
    lg = league_average(con, "NFL", season, gd)
    if lg is None or (ra["n_last"] or 0) < 6 or (rh["n_last"] or 0) < 6:
        return None
    avg_pts = lg / 2.0
    away_pts = (ra["scored_last"] + rh["allowed_last"]) / 2.0
    home_pts = (rh["scored_last"] + ra["allowed_last"]) / 2.0 + 1.0  # documented HFA bump
    return {"model_total": round(away_pts + home_pts, 1),
            "features": {"away_scored": ra["scored_last"], "away_allowed": ra["allowed_last"],
                         "home_scored": rh["scored_last"], "home_allowed": rh["allowed_last"],
                         "league_avg_total": lg}}


def sig_nfl_03(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if game["game_type"] not in ("REG", "POST"):
        return []
    mt = _model_total_nfl(con, game)
    pr_o = prices.get(("TOTAL", "over"))
    if mt is None or pr_o is None or pr_o["line"] is None:
        return []
    line = pr_o["line"]
    diff = mt["model_total"] - line
    if abs(diff) < 3.0:
        return []
    sigma = totals_sigma(con, "NFL")
    p_over = 1.0 - norm_cdf((line - mt["model_total"]) / sigma)
    side = "over" if diff > 0 else "under"
    mp_o, mp_u = devigged_probs(prices, "TOTAL", "over", "under")
    mkt = mp_o if side == "over" else mp_u
    model_p = p_over if side == "over" else 1.0 - p_over
    if mkt is None or model_p - mkt < 0.03:
        return []
    s = _base_signal("S-NFL-03", game, "TOTAL", side, decision_utc)
    s.update(model_prob=round(model_p, 4), market_prob=round(mkt, 4),
             edge=round(model_p - mkt, 4))
    s["features"] = {**mt["features"], "model_total": mt["model_total"],
                     "sigma": sigma, "dist": "normal"}
    return [_attach_price(s, prices)]


def sig_nfl_04(con, game, prices, decision_utc, close_only) -> list[Signal]:
    ex = _extra(game)
    ar, hr = ex.get("away_rest"), ex.get("home_rest")
    if ar is None or hr is None or abs(ar - hr) < 3:
        return []
    side = "away" if ar > hr else "home"
    pr = prices.get(("SPREAD", side))
    if pr is None:
        return []
    mp_a, mp_h = devigged_probs(prices, "SPREAD", "away", "home")
    s = _base_signal("S-NFL-04", game, "SPREAD", side, decision_utc)
    s["market_prob"] = round((mp_a if side == "away" else mp_h) or 0, 4) or None
    s["features"] = {"away_rest": ar, "home_rest": hr}
    return [_attach_price(s, prices)]


OUTDOOR_ROOFS = {"outdoors", "open", "outdoor", "retractable-open", "open-air"}


def sig_nfl_05(con, game, prices, decision_utc, close_only) -> list[Signal]:
    ex = _extra(game)
    roof = str(ex.get("roof") or "").lower()
    if roof not in OUTDOOR_ROOFS:
        return []
    wind, temp = ex.get("wind"), ex.get("temp")
    if wind is None and temp is None:
        return []  # missing weather => no signal, never assume
    if not ((wind is not None and wind >= 15) or (temp is not None and temp <= 32)):
        return []
    pr = prices.get(("TOTAL", "under"))
    if pr is None:
        return []
    mp_o, mp_u = devigged_probs(prices, "TOTAL", "over", "under")
    s = _base_signal("S-NFL-05", game, "TOTAL", "under", decision_utc)
    s["market_prob"] = round(mp_u, 4) if mp_u else None
    s["features"] = {"roof": roof, "wind": wind, "temp": temp}
    return [_attach_price(s, prices)]


def sig_nfl_06(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if game["game_type"] != "REG":
        return []
    pr_a = prices.get(("SPREAD", "away"))
    if pr_a is None or pr_a["line"] is None:
        return []
    if not (-3.0 <= pr_a["line"] < 0):
        return []
    pr = prices.get(("SPREAD", "home"))
    if pr is None:
        return []
    mp_a, mp_h = devigged_probs(prices, "SPREAD", "away", "home")
    s = _base_signal("S-NFL-06", game, "SPREAD", "home", decision_utc)
    s["market_prob"] = round(mp_h, 4) if mp_h else None
    s["features"] = {"away_line": pr_a["line"]}
    return [_attach_price(s, prices)]


# ------------------------------------------------------------- MLB signals
def sig_mlb_01(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if game["game_type"] != "R":
        return []
    mp = mlb_prob(con, game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"])
    if mp["p_home"] is None:
        return []
    out = []
    for side, p in (("away", mp["p_away"]), ("home", mp["p_home"])):
        fair = prob_to_american(p)
        edge_proxy = abs(p - 0.5) * 2  # conviction proxy; edge vs market unknowable
        if edge_proxy >= 0.10:  # >=55%/45% conviction
            s = _base_signal("S-MLB-01", game, "ML", side, decision_utc)
            _model_price_ml(s, p, fair)
            s["features"] = {"basis": mp["model"], "conviction": round(edge_proxy, 3)}
            s["note"] = "MODEL-grade: no market odds feed; fair price from log5 blend"
            out.append(s)
    return out


def sig_mlb_02(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if game["game_type"] != "R":
        return []
    mp = mlb_prob(con, game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"])
    if mp["p_home"] is None:
        return []
    out = []
    for side, tm, opp in (("away", game["away_team"], game["home_team"]),
                          ("home", game["home_team"], game["away_team"])):
        info = mp[side]
        l10 = info.get("l10")
        streak = info.get("streak")
        opp_l10 = mp["home" if side == "away" else "away"].get("l10")
        if isinstance(info, dict) and l10 is not None and opp_l10 is not None:
            hot = streak and str(streak).startswith("W") and int(str(streak)[1:] or 0) >= 3
            if game["season"] != "2026":
                f = last_n_form(con, "MLB", tm, game["game_date"], game["game_key"], 10)
                hot = f["streak_type"] == "W" and (f["streak_n"] or 0) >= 3
                l10, opp_l10 = f["pct"], last_n_form(
                    con, "MLB", opp, game["game_date"], game["game_key"], 10)["pct"]
            else:
                try:
                    n = int(str(streak)[1:])
                except (TypeError, ValueError):
                    n = 0
                hot = str(streak or "").startswith("W") and n >= 3
            if hot and (l10 or 0) >= 0.600 and (opp_l10 or 1) <= 0.400:
                p = mp["p_away"] if side == "away" else mp["p_home"]
                s = _base_signal("S-MLB-02", game, "ML", side, decision_utc)
                _model_price_ml(s, p, prob_to_american(p))
                s["features"] = {"streak": streak, "l10": l10, "opp_l10": opp_l10}
                s["note"] = "MODEL-grade leg"
                out.append(s)
    return out


def sig_mlb_03(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if game["game_type"] != "R":
        return []
    mp = mlb_prob(con, game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"])
    if mp["p_home"] is None:
        return []
    out = []
    for side in ("away", "home"):
        p = mp["p_away"] if side == "away" else mp["p_home"]
        if p >= 0.5:
            continue  # dogs only
        info = mp[side]
        if game["season"] == "2026" and info.get("xw") is not None:
            xwp = info["xw"] / (info["xw"] + info["xl"])
            wp = info["w"] / (info["w"] + info["l"])
        else:
            xwp = info.get("season_py") or 0.5
            f = last_n_form(con, "MLB",
                            game["away_team"] if side == "away" else game["home_team"],
                            game["game_date"], game["game_key"], 162)
            wp = f["pct"] if f["pct"] is not None else 0.5
        if xwp - wp >= 0.050:
            s = _base_signal("S-MLB-03", game, "ML", side, decision_utc)
            _model_price_ml(s, p, prob_to_american(p))
            s["features"] = {"xw_pct": round(xwp, 3), "w_pct": round(wp, 3)}
            s["note"] = "MODEL-grade leg"
            out.append(s)
    return out


def sig_mlb_04(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if game["game_type"] != "R":
        return []
    mp = mlb_prob(con, game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"])
    if mp["p_home"] is None:
        return []
    if game["season"] == "2026":
        hw, hl = mp["home"].get("home") or (0, 0)
        aw, al = mp["away"].get("away") or (0, 0)
    else:
        sh = home_away_split(con, "MLB", game["home_team"], game["game_date"],
                             game["game_key"], game["season"])
        sa = home_away_split(con, "MLB", game["away_team"], game["game_date"],
                             game["game_key"], game["season"])
        hw, hl, aw, al = sh["home_w"], sh["home_l"], sa["away_w"], sa["away_l"]
    if hw + hl < 20 or aw + al < 20:
        return []
    if hw / (hw + hl) - aw / (aw + al) < 0.200:
        return []
    p = mp["p_home"]
    s = _base_signal("S-MLB-04", game, "ML", "home", decision_utc)
    _model_price_ml(s, p, prob_to_american(p))
    s["features"] = {"home_home_pct": round(hw / (hw + hl), 3),
                     "away_away_pct": round(aw / (aw + al), 3)}
    s["note"] = "MODEL-grade leg"
    return [s]


def sig_mlb_05(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if game["game_type"] != "R":
        return []
    season, gd, gk = game["season"], game["game_date"], game["game_key"]
    if season == "2026":
        ma = mlb_team_strength(con, game["away_team"], gd, gk, season)
        mh = mlb_team_strength(con, game["home_team"], gd, gk, season)
        if ma["p"] is None or mh["p"] is None:
            return []
        # Need RS/RA per game: pull from snapshot via team_form directly.
        ra = con.execute("SELECT rs, ra, w, l FROM team_form WHERE sport='MLB' AND team=? "
                         "AND season='2026' ORDER BY as_of_utc DESC LIMIT 1",
                         (game["away_team"],)).fetchone()
        rh = con.execute("SELECT rs, ra, w, l FROM team_form WHERE sport='MLB' AND team=? "
                         "AND season='2026' ORDER BY as_of_utc DESC LIMIT 1",
                         (game["home_team"],)).fetchone()
        if ra is None or rh is None:
            return []
        a_rs, a_ra = ra["rs"] / (ra["w"] + ra["l"]), ra["ra"] / (ra["w"] + ra["l"])
        h_rs, h_ra = rh["rs"] / (rh["w"] + rh["l"]), rh["ra"] / (rh["w"] + rh["l"])
    else:
        rra = rolling_rates(con, "MLB", game["away_team"], gd, gk, season, 20)
        rrh = rolling_rates(con, "MLB", game["home_team"], gd, gk, season, 20)
        if (rra["n_season"] or 0) < 20 or (rrh["n_season"] or 0) < 20:
            return []
        a_rs, a_ra = rra["scored_season"], rra["allowed_season"]
        h_rs, h_ra = rrh["scored_season"], rrh["allowed_season"]
    lg = league_average(con, "MLB", season if season != "2026" else "2025", gd)
    lg_pg = (lg / 2.0) if lg else 4.5
    exp_a = (a_rs + h_ra) / 2.0 / lg_pg * lg_pg
    exp_h = (h_rs + a_ra) / 2.0 / lg_pg * lg_pg
    model_total = exp_a + exp_h
    pf = park_factor(con, game["home_team"]) or 1.0
    model_total *= pf
    proxy_line = 8.8  # documented league proxy (no market line in v1)
    diff = model_total - proxy_line
    if abs(diff) < 1.5:
        return []
    side = "over" if diff > 0 else "under"
    sigma = totals_sigma(con, "MLB")
    p_over = 1.0 - norm_cdf((proxy_line - model_total) / sigma)
    p = p_over if side == "over" else 1.0 - p_over
    s = _base_signal("S-MLB-05", game, "TOTAL", side, decision_utc)
    s.update(model_prob=round(p, 4), line=round(proxy_line, 1))
    _model_price_ml(s, p, prob_to_american(p))
    s["line"] = round(proxy_line, 1)
    s["features"] = {"model_total": round(model_total, 2), "proxy_line": proxy_line,
                     "park_factor": pf, "sigma": sigma}
    s["note"] = "MODEL-grade leg: proxy line, no market total feed"
    return [s]


# 2026 race snapshot (verified standings 2026-09-22): team -> (status, gb).
# status: 'race' (within 5 of spot / leader), 'out' (eliminated E), 'clinched'.
MLB_2026_RACE = {
    "TB": ("clinched", 0), "NYY": ("clinched", 0), "BOS": ("clinched", 0),
    "TOR": ("out", 99), "BAL": ("out", 99),
    "CLE": ("race", 0), "CWS": ("race", 1), "MIN": ("out", 99),
    "DET": ("out", 99), "KC": ("out", 99),
    "TEX": ("race", 0), "HOU": ("race", 1), "SEA": ("out", 99),
    "ATH": ("out", 99), "OAK": ("out", 99), "LAA": ("out", 99),
    "ATL": ("clinched", 0), "PHI": ("race", 0), "MIA": ("out", 99),
    "WSH": ("out", 99), "NYM": ("out", 99),
    "MIL": ("clinched", 0), "CHC": ("race", 0), "PIT": ("out", 99),
    "STL": ("out", 99), "CIN": ("out", 99),
    "LAD": ("clinched", 0), "SD": ("race", 0), "ARI": ("race", 4),
    "SF": ("out", 99), "COL": ("out", 99),
}


def sig_mlb_06(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if game["season"] != "2026" or game["game_type"] != "R":
        return []  # forward-test only (no historical race snapshots)
    if not game["game_date"].startswith("2026-09"):
        return []
    a = MLB_2026_RACE.get(game["away_team"], ("unknown", 99))
    h = MLB_2026_RACE.get(game["home_team"], ("unknown", 99))
    side = None
    if a[0] == "race" and h[0] == "out":
        side = "away"
    elif h[0] == "race" and a[0] == "out":
        side = "home"
    if side is None:
        return []
    mp = mlb_prob(con, game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"])
    if mp["p_home"] is None:
        return []
    p = mp["p_away"] if side == "away" else mp["p_home"]
    s = _base_signal("S-MLB-06", game, "ML", side, decision_utc)
    _model_price_ml(s, p, prob_to_american(p))
    s["features"] = {"away_race": a, "home_race": h}
    s["note"] = "MODEL-grade leg; motivation signal"
    return [s]


# ------------------------------------------------------------- NHL signals
REG_ONLY = {"REG", "2", "POST", "3"}


def _nhl_game_ok(game) -> bool:
    return game["game_type"] in ("REG", "POST", "2", "3")


def sig_nhl_01(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nhl_game_ok(game):
        return []
    ep = elo_prob(con, "NHL", game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"],
                  bool(game["neutral"]))
    mp_a, mp_h = devigged_probs(prices, "ML", "away", "home")
    if mp_a is None:
        return []
    out = []
    for side, model_p, mkt_p in (("away", ep["p_away"], mp_a), ("home", ep["p_home"], mp_h)):
        if model_p - mkt_p >= 0.04:
            s = _base_signal("S-NHL-01", game, "ML", side, decision_utc)
            s.update(model_prob=round(model_p, 4), market_prob=round(mkt_p, 4),
                     edge=round(model_p - mkt_p, 4))
            s["features"] = {"elo_away": round(ep["elo_away"], 1),
                             "elo_home": round(ep["elo_home"], 1)}
            out.append(_attach_price(s, prices))
    return out


def sig_nhl_02(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nhl_game_ok(game):
        return []
    spot = is_b2b(con, "NHL", game["away_team"], game["game_date"]) or \
        games_in_last_n_days(con, "NHL", game["away_team"], game["game_date"], 4) >= 3
    if not spot:
        return []
    s = _base_signal("S-NHL-02", game, "ML", "home", decision_utc)
    mp_a, mp_h = devigged_probs(prices, "ML", "away", "home")
    s["market_prob"] = round(mp_h, 4) if mp_h else None
    s["features"] = {"away_b2b_or_3in4": True}
    return [_attach_price(s, prices)]


def _model_total_goals(con, game, sport) -> dict[str, Any] | None:
    season, gd, gk = game["season"], game["game_date"], game["game_key"]
    ra = rolling_rates(con, sport, game["away_team"], gd, gk, season, 10)
    rh = rolling_rates(con, sport, game["home_team"], gd, gk, season, 10)
    lg = league_average(con, sport, season, gd)
    if lg is None or (ra["n_last"] or 0) < 8 or (rh["n_last"] or 0) < 8:
        return None
    avg = lg / 2.0
    away_g = (ra["scored_last"] + rh["allowed_last"]) / 2.0
    home_g = (rh["scored_last"] + ra["allowed_last"]) / 2.0
    return {"model_total": round(away_g + home_g, 2),
            "features": {"away_scored": ra["scored_last"], "away_allowed": ra["allowed_last"],
                         "home_scored": rh["scored_last"], "home_allowed": rh["allowed_last"],
                         "league_avg_total": lg}}


def sig_nhl_03(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nhl_game_ok(game):
        return []
    mt = _model_total_goals(con, game, "NHL")
    pr_o = prices.get(("TOTAL", "over"))
    line = pr_o["line"] if pr_o and pr_o["line"] is not None else None
    if mt is None:
        return []
    if line is None:
        line, lined = 6.0, False  # documented fallback proxy when no market line
    else:
        lined = True
    diff = mt["model_total"] - line
    if abs(diff) < 0.75:
        return []
    side = "over" if diff > 0 else "under"
    sigma = totals_sigma(con, "NHL")
    p_over = 1.0 - norm_cdf((line - mt["model_total"]) / sigma)
    p = p_over if side == "over" else 1.0 - p_over
    mp_o, mp_u = devigged_probs(prices, "TOTAL", "over", "under")
    mkt = (mp_o if side == "over" else mp_u)
    s = _base_signal("S-NHL-03", game, "TOTAL", side, decision_utc)
    s.update(model_prob=round(p, 4), line=line)
    if mkt is not None:
        s.update(market_prob=round(mkt, 4), edge=round(p - mkt, 4))
        if p - mkt < 0.03:
            return []
        return [_attach_price(s, prices)]
    _model_price_ml(s, p, prob_to_american(p))
    s["line"] = line
    s["features"] = {**mt["features"], "model_total": mt["model_total"],
                     "proxy_line": (not lined)}
    s["note"] = "MODEL-grade leg (no market total)"
    return [s]


def sig_nhl_04(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nhl_game_ok(game):
        return []
    out = []
    for tired, fade_side in ((game["away_team"], "home"), (game["home_team"], "away")):
        if is_b2b(con, "NHL", tired, game["game_date"]):
            s = _base_signal("S-NHL-04", game, "ML", fade_side, decision_utc)
            mp_a, mp_h = devigged_probs(prices, "ML", "away", "home")
            mkt = mp_a if fade_side == "away" else mp_h
            s["market_prob"] = round(mkt, 4) if mkt else None
            s["features"] = {"faded_b2b_team": tired}
            out.append(_attach_price(s, prices))
    return out


def sig_nhl_05(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nhl_game_ok(game):
        return []
    ep = elo_prob(con, "NHL", game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"],
                  bool(game["neutral"]))
    out = []
    for side, p in (("away", ep["p_away"]), ("home", ep["p_home"])):
        if p < 0.62:
            continue
        pr = prices.get(("SPREAD", side))
        if pr is None or pr["line"] is None or abs(abs(pr["line"]) - 1.5) > 0.01:
            continue  # -1.5 puck lines only
        cov = cover_mapping(con, "NHL", game["season"], "fav", p)
        mp_a, mp_h = devigged_probs(prices, "SPREAD", "away", "home")
        mkt = mp_a if side == "away" else mp_h
        if cov is None or mkt is None or cov - mkt < 0.04:
            continue
        s = _base_signal("S-NHL-05", game, "SPREAD", side, decision_utc)
        s.update(model_prob=round(cov, 4), market_prob=round(mkt, 4),
                 edge=round(cov - mkt, 4))
        s["features"] = {"cover_method": "empirical-map", "ml_model_p": round(p, 3)}
        out.append(_attach_price(s, prices))
    return out


def sig_nhl_06(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nhl_game_ok(game):
        return []
    ep = elo_prob(con, "NHL", game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"],
                  bool(game["neutral"]))
    if ep["p_away"] >= 0.5:
        return []
    ra = rolling_rates(con, "NHL", game["away_team"], game["game_date"],
                       game["game_key"], game["season"], 10)
    rh = rolling_rates(con, "NHL", game["home_team"], game["game_date"],
                       game["game_key"], game["season"], 10)
    if (ra["n_last"] or 0) < 8 or (rh["n_last"] or 0) < 8:
        return []
    gf_a = ra["scored_last"] / (ra["scored_last"] + ra["allowed_last"])
    gf_h = rh["scored_last"] / (rh["scored_last"] + rh["allowed_last"])
    if gf_a - gf_h < 0.05:
        return []
    s = _base_signal("S-NHL-06", game, "ML", "away", decision_utc)
    s.update(model_prob=round(ep["p_away"], 4))
    mp_a, _ = devigged_probs(prices, "ML", "away", "home")
    s["market_prob"] = round(mp_a, 4) if mp_a else None
    s["features"] = {"away_gf_pct": round(gf_a, 3), "home_gf_pct": round(gf_h, 3)}
    return [_attach_price(s, prices)]


# ------------------------------------------------------------- NBA signals
def _nba_game_ok(game) -> bool:
    return game["game_type"] in ("REG", "POST")


def sig_nba_01(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nba_game_ok(game):
        return []
    ep = elo_prob(con, "NBA", game["away_team"], game["home_team"],
                  game["game_date"], game["game_key"], game["season"],
                  bool(game["neutral"]))
    mp_a, mp_h = devigged_probs(prices, "ML", "away", "home")
    out = []
    if mp_a is not None:
        for side, model_p, mkt_p in (("away", ep["p_away"], mp_a),
                                     ("home", ep["p_home"], mp_h)):
            if model_p - mkt_p >= 0.04:
                s = _base_signal("S-NBA-01", game, "ML", side, decision_utc)
                s.update(model_prob=round(model_p, 4), market_prob=round(mkt_p, 4),
                         edge=round(model_p - mkt_p, 4))
                s["features"] = {"elo_away": round(ep["elo_away"], 1),
                                 "elo_home": round(ep["elo_home"], 1)}
                out.append(_attach_price(s, prices))
    # spread follows strong ML conviction via cover mapping
    for side, p in (("away", ep["p_away"]), ("home", ep["p_home"])):
        if p < 0.60:
            continue
        pr = prices.get(("SPREAD", side))
        if pr is None or pr["line"] is None:
            continue
        kind = "fav" if pr["line"] < 0 else "dog"
        cov = cover_mapping(con, "NBA", game["season"], kind, p)
        mp2 = devigged_probs(prices, "SPREAD", "away", "home")
        mkt = mp2[0] if side == "away" else mp2[1]
        if cov is None or mkt is None or cov - mkt < 0.04:
            continue
        s = _base_signal("S-NBA-01", game, "SPREAD", side, decision_utc)
        s.update(model_prob=round(cov, 4), market_prob=round(mkt, 4),
                 edge=round(cov - mkt, 4))
        s["features"] = {"cover_method": "empirical-map"}
        out.append(_attach_price(s, prices))
    return out


def sig_nba_02(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nba_game_ok(game):
        return []
    out = []
    for tired, fade_side in ((game["away_team"], "home"), (game["home_team"], "away")):
        spot = is_b2b(con, "NBA", tired, game["game_date"]) or \
            games_in_last_n_days(con, "NBA", tired, game["game_date"], 4) >= 3
        if not spot:
            continue
        pr = prices.get(("SPREAD", fade_side))
        if pr is None:
            continue
        s = _base_signal("S-NBA-02", game, "SPREAD", fade_side, decision_utc)
        mp = devigged_probs(prices, "SPREAD", "away", "home")
        mkt = mp[0] if fade_side == "away" else mp[1]
        s["market_prob"] = round(mkt, 4) if mkt else None
        s["features"] = {"faded_spot_team": tired}
        out.append(_attach_price(s, prices))
    return out


def sig_nba_03(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nba_game_ok(game):
        return []
    season, gd, gk = game["season"], game["game_date"], game["game_key"]
    ra = rolling_rates(con, "NBA", game["away_team"], gd, gk, season, 8)
    rh = rolling_rates(con, "NBA", game["home_team"], gd, gk, season, 8)
    lg = league_average(con, "NBA", season, gd)
    pr_o = prices.get(("TOTAL", "over"))
    if lg is None or pr_o is None or pr_o["line"] is None:
        return []
    if (ra["n_last"] or 0) < 6 or (rh["n_last"] or 0) < 6:
        return []
    model = ((ra["scored_last"] + rh["allowed_last"]) / 2.0 +
             (rh["scored_last"] + ra["allowed_last"]) / 2.0 + 1.5)
    line = pr_o["line"]
    if abs(model - line) < 6.0:
        return []
    side = "over" if model > line else "under"
    sigma = totals_sigma(con, "NBA")
    p_over = 1.0 - norm_cdf((line - model) / sigma)
    p = p_over if side == "over" else 1.0 - p_over
    mp_o, mp_u = devigged_probs(prices, "TOTAL", "over", "under")
    mkt = mp_o if side == "over" else mp_u
    if mkt is None or p - mkt < 0.03:
        return []
    s = _base_signal("S-NBA-03", game, "TOTAL", side, decision_utc)
    s.update(model_prob=round(p, 4), market_prob=round(mkt, 4), edge=round(p - mkt, 4))
    s["features"] = {"model_total": round(model, 1), "sigma": sigma}
    return [_attach_price(s, prices)]


def sig_nba_04(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nba_game_ok(game):
        return []
    sp = home_away_split(con, "NBA", game["home_team"], game["game_date"],
                         game["game_key"], game["season"])
    if sp["home_w"] + sp["home_l"] < 10:
        return []
    if sp["home_w"] / (sp["home_w"] + sp["home_l"]) < 0.700:
        return []
    pr = prices.get(("SPREAD", "home"))
    if pr is None:
        return []
    s = _base_signal("S-NBA-04", game, "SPREAD", "home", decision_utc)
    s["features"] = {"home_split": sp}
    return [_attach_price(s, prices)]


def sig_nba_05(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nba_game_ok(game):
        return []
    out = []
    for hot, fade_side in ((game["away_team"], "home"), (game["home_team"], "away")):
        st = ats_streak(con, "NBA", hot, game["game_date"], game["game_key"])
        if st["covers_streak"] < 4:
            continue
        pr = prices.get(("SPREAD", fade_side))
        if pr is None:
            continue
        s = _base_signal("S-NBA-05", game, "SPREAD", fade_side, decision_utc)
        s["features"] = {"faded_ats_streak": st["covers_streak"], "team": hot}
        out.append(_attach_price(s, prices))
    return out


def sig_nba_06(con, game, prices, decision_utc, close_only) -> list[Signal]:
    if not _nba_game_ok(game):
        return []
    pr = prices.get(("SPREAD", "away"))
    if pr is None or pr["line"] is None or pr["line"] < 6.0:
        return []
    rested = not is_b2b(con, "NBA", game["away_team"], game["game_date"]) and \
        games_in_last_n_days(con, "NBA", game["away_team"], game["game_date"], 2) == 0
    home_tired = is_b2b(con, "NBA", game["home_team"], game["game_date"])
    if not (rested or home_tired):
        return []
    s = _base_signal("S-NBA-06", game, "SPREAD", "away", decision_utc)
    s["features"] = {"away_line": pr["line"], "away_rested": rested,
                     "home_b2b": home_tired}
    return [_attach_price(s, prices)]


# ------------------------------------------------------------- dispatcher
GENERATORS = {
    "S-NFL-01": sig_nfl_01, "S-NFL-02": sig_nfl_02, "S-NFL-03": sig_nfl_03,
    "S-NFL-04": sig_nfl_04, "S-NFL-05": sig_nfl_05, "S-NFL-06": sig_nfl_06,
    "S-MLB-01": sig_mlb_01, "S-MLB-02": sig_mlb_02, "S-MLB-03": sig_mlb_03,
    "S-MLB-04": sig_mlb_04, "S-MLB-05": sig_mlb_05, "S-MLB-06": sig_mlb_06,
    "S-NHL-01": sig_nhl_01, "S-NHL-02": sig_nhl_02, "S-NHL-03": sig_nhl_03,
    "S-NHL-04": sig_nhl_04, "S-NHL-05": sig_nhl_05, "S-NHL-06": sig_nhl_06,
    "S-NBA-01": sig_nba_01, "S-NBA-02": sig_nba_02, "S-NBA-03": sig_nba_03,
    "S-NBA-04": sig_nba_04, "S-NBA-05": sig_nba_05, "S-NBA-06": sig_nba_06,
}

# MULTI strategies reuse single-sport generators per leg.
MULTI_SOURCES = {
    "S-MULTI-01": ["S-NFL-01", "S-MLB-01", "S-NHL-01", "S-NBA-01"],
    "S-MULTI-02": ["S-NFL-01", "S-NFL-03", "S-MLB-01", "S-MLB-05", "S-NHL-01",
                   "S-NHL-03", "S-NBA-01", "S-NBA-03"],
    "S-MULTI-03": ["S-NFL-03", "S-NFL-05", "S-MLB-05", "S-NHL-03", "S-NBA-03"],
    "S-MULTI-04": ["S-NFL-01", "S-NFL-02", "S-NFL-06", "S-MLB-01", "S-MLB-02",
                   "S-MLB-03", "S-MLB-04", "S-NHL-01", "S-NHL-06", "S-NBA-01",
                   "S-NBA-06"],
}


def signals_for_game(con: sqlite3.Connection, strategy_id: str, game: sqlite3.Row,
                     decision_utc: str, test_mode: str) -> list[Signal]:
    """Generate signals for one game. Backtests use closing prices only."""
    if strategy_id in MULTI_SOURCES:
        return []  # multi legs are pooled by the engine, not per-game here
    gen = GENERATORS.get(strategy_id)
    if gen is None:
        return []
    close_only = (test_mode == "backtest")
    prices = get_prices(con, game["game_key"], close_only=close_only)
    sigs = gen(con, game, prices, decision_utc, close_only)
    for s in sigs:
        s["strategy_id"] = strategy_id
        if (s.get("edge") is None and s.get("model_prob") is not None
                and s.get("odds_american") is not None
                and s.get("odds_type") in ("model_fair", "assumed")):
            # MODEL leg: rank by edge vs the documented proxy/assumed price.
            # The proxy basis stays labeled on the leg; this invents no market.
            try:
                mp = american_to_prob(s["odds_american"])
            except ValueError:
                mp = None
            if mp is not None:
                s["market_prob"] = round(mp, 4)
                s["edge"] = round(float(s["model_prob"]) - mp, 4)
    return sigs
