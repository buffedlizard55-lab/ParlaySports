"""Data-quality checks: detect and flag, never silently fix.

Checks cover: missing scores, missing odds, duplicates, conflicting results,
invalid stats, timestamp order, stale snapshots, settlement recomputation,
ledger integrity, parlay math, backtest leakage guards.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from .parlay import parlay_decimal, settle_leg, settle_parlay
from .store import add_issue, verify_ledger_chain
from .util import american_to_decimal, american_to_prob


def run_checks(con: sqlite3.Connection) -> dict[str, Any]:
    # Idempotent: clear prior open auto-check rows ([name] prefix) so each
    # scan produces a fresh finding set instead of piling up duplicates.
    con.execute("DELETE FROM issues WHERE status='open' AND detail LIKE '[%'")
    resolved = {r["detail"] for r in
                con.execute("SELECT detail FROM issues WHERE status='resolved'")}
    results: list[dict[str, Any]] = []

    def check(name: str, fn) -> None:
        try:
            problems = fn()
        except Exception as e:  # checks must never crash the pipeline
            problems = [f"check crashed: {e}"]
        filed, suppressed = [], 0
        for p in problems[:200]:
            detail = f"[{name}] {p}"
            if detail in resolved:
                suppressed += 1  # already investigated; resolution stands
                continue
            add_issue(con, severity="warning" if "stale" in name else "error",
                      area="quality", detail=detail)
            filed.append(p)
        results.append({"check": name, "problems": len(filed),
                        "detail": filed[:25], "suppressed_resolved": suppressed})

    check("missing-scores-past-games", lambda: _missing_scores(con))
    check("missing-odds-priced-parlays", lambda: _missing_odds(con))
    check("duplicate-events", lambda: _duplicates(con))
    check("conflicting-results", lambda: _conflicts(con))
    check("invalid-stats", lambda: _invalid_stats(con))
    check("timestamp-order", lambda: _timestamps(con))
    check("stale-snapshots", lambda: _stale(con))
    check("settlement-recompute", lambda: _recompute_settlements(con))
    check("ledger-chain", lambda: verify_ledger_chain(con))
    check("parlay-math", lambda: _parlay_math(con))
    check("backtest-leakage", lambda: _leakage(con))
    check("odds-sanity", lambda: _odds_sanity(con))
    check("missing-historical-periods", lambda: _missing_periods(con))
    con.commit()
    return {"checks": results,
            "total_problems": sum(r["problems"] for r in results)}


def _missing_scores(con) -> list[str]:
    # Past-date games (before today UTC) still scheduled with no score.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = con.execute(
        """SELECT game_key, sport, game_date, game_type FROM games
           WHERE status='scheduled' AND game_date < ?
             AND game_type NOT IN ('PRE','E','S','A') LIMIT 50""",
        (today,)).fetchall()
    return [f"{r['game_key']} ({r['sport']} {r['game_date']}) still scheduled" for r in rows]


def _missing_odds(con) -> list[str]:
    rows = con.execute(
        """SELECT p.parlay_id, COUNT(*) AS n FROM parlays p
           JOIN legs l ON l.parlay_id=p.parlay_id
           WHERE p.pricing_grade != 'UNPRICED' AND l.odds_american IS NULL
           GROUP BY p.parlay_id LIMIT 20""").fetchall()
    return [f"{r['parlay_id']}: {r['n']} legs without odds but grade != UNPRICED"
            for r in rows]


def _duplicates(con) -> list[str]:
    # Same matchup/date twice is legitimate ONLY for MLB doubleheaders where
    # every row has a distinct league game id (verified real case: MIA@LAD
    # 2023-08-19 Hurricane-Hilary makeup DH, both games 3-1; see verifications).
    # Anything sharing a league game id, or any non-MLB repeat, is flagged.
    rows = con.execute(
        """SELECT sport, game_date, away_team, home_team, away_score, home_score,
                  COUNT(*) AS n, COUNT(DISTINCT league_game_id) AS ids
           FROM games GROUP BY sport, game_date, away_team, home_team,
                         away_score, home_score
           HAVING n > 1 LIMIT 20""").fetchall()
    out = []
    for r in rows:
        if r["sport"] == "MLB" and r["ids"] == r["n"]:
            continue  # verified doubleheader pattern: distinct game ids
        out.append(f"{r['sport']} {r['game_date']} {r['away_team']}@{r['home_team']} "
                   f"({r['away_score']}-{r['home_score']}): {r['n']} rows, "
                   f"{r['ids']} distinct league ids")
    return out


def _conflicts(con) -> list[str]:
    # Same game_key stored twice with different scores is impossible (PK) and a
    # re-import that changes a final files an ingest issue; here we hunt the
    # cross-row case: two 'final' rows for the same matchup/date with different
    # scores (one of them must be wrong). MLB doubleheaders are exempt only when
    # the league game ids differ AND the scores differ (two real games).
    out = []
    rows = con.execute(
        """SELECT sport, game_date, away_team, home_team,
                  COUNT(*) AS n, COUNT(DISTINCT league_game_id) AS ids,
                  COUNT(DISTINCT COALESCE(away_score,'?') || '-' ||
                        COALESCE(home_score,'?')) AS score_versions
           FROM games WHERE status='final'
           GROUP BY sport, game_date, away_team, home_team
           HAVING score_versions > 1 LIMIT 20""").fetchall()
    for r in rows:
        detail = (f"{r['sport']} {r['game_date']} {r['away_team']}@{r['home_team']}: "
                  f"{r['score_versions']} different final scores across {r['n']} rows")
        if r["sport"] == "MLB" and r["ids"] == r["n"] and r["score_versions"] == r["n"]:
            continue  # legitimate doubleheader: all rows distinct games+scores
        out.append(detail)
    for r in con.execute(
            """SELECT game_key FROM games WHERE status='final'
               AND (away_score IS NULL OR home_score IS NULL) LIMIT 20"""):
        out.append(f"{r['game_key']}: final without complete score")
    return out


def _invalid_stats(con) -> list[str]:
    out = []
    for r in con.execute(
            "SELECT game_key, sport, away_score, home_score FROM games "
            "WHERE status='final' AND (away_score < 0 OR home_score < 0) LIMIT 20"):
        out.append(f"{r['game_key']}: negative score")
    for r in con.execute(
            "SELECT game_key FROM games WHERE sport='MLB' AND status='final' "
            "AND away_score=home_score LIMIT 20"):
        out.append(f"{r['game_key']}: tied MLB final (impossible)")
    for r in con.execute(
            "SELECT game_key, sport, away_score, home_score FROM games "
            "WHERE status='final' AND ((sport='NHL' AND (away_score>15 OR home_score>15)) "
            "OR (sport='NBA' AND (away_score>200 OR home_score>200)) "
            "OR (sport='NFL' AND (away_score>80 OR home_score>80))) LIMIT 20"):
        out.append(f"{r['game_key']}: implausible score {r['away_score']}-{r['home_score']}")
    return out


def _timestamps(con) -> list[str]:
    out = []
    for r in con.execute(
            """SELECT p.parlay_id, p.decision_utc, g.start_utc, g.game_key
               FROM parlays p JOIN legs l ON l.parlay_id=p.parlay_id
               JOIN games g ON g.game_key=l.game_key
               WHERE g.start_utc IS NOT NULL AND p.decision_utc >= g.start_utc LIMIT 20"""):
        out.append(f"{r['parlay_id']}: decision {r['decision_utc']} not before start "
                   f"{r['start_utc']} ({r['game_key']})")
    for r in con.execute(
            "SELECT parlay_id FROM parlays WHERE settled_utc IS NOT NULL "
            "AND settled_utc < decision_utc LIMIT 20"):
        out.append(f"{r['parlay_id']}: settled before decided")
    return out


def _stale(con) -> list[str]:
    out = []
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r in con.execute(
            "SELECT source_id, name, last_verified_utc FROM sources "
            "WHERE last_verified_utc < ?", (cutoff,)):
        out.append(f"{r['source_id']}: last verified {r['last_verified_utc']}")
    return out


def _recompute_settlements(con) -> list[str]:
    out = []
    rows = con.execute(
        "SELECT * FROM parlays WHERE status IN ('won','lost','push')").fetchall()
    for p in rows:
        legs = con.execute("SELECT * FROM legs WHERE parlay_id=?",
                           (p["parlay_id"],)).fetchall()
        recomputed = []
        for leg in legs:
            g = con.execute("SELECT * FROM games WHERE game_key=?",
                            (leg["game_key"],)).fetchone()
            if g is None or g["status"] != "final":
                recomputed = []
                break
            res = settle_leg(dict(leg), dict(g))
            recomputed.append({"result": res["result"],
                               "odds_american": leg["odds_american"]})
        if not recomputed:
            continue
        outcome = settle_parlay(dict(p), recomputed)
        if outcome["status"] != p["status"]:
            out.append(f"{p['parlay_id']}: stored {p['status']} vs recomputed "
                       f"{outcome['status']}")
        elif outcome["pnl"] is not None and p["pnl"] is not None and \
                abs(float(outcome["pnl"]) - float(p["pnl"])) > 0.01:
            out.append(f"{p['parlay_id']}: stored PnL {p['pnl']} vs recomputed "
                       f"{outcome['pnl']}")
        if p["payout"] is not None and outcome["payout"] is not None and \
                abs(float(outcome["payout"]) - float(p["payout"])) > 0.01:
            out.append(f"{p['parlay_id']}: stored payout {p['payout']} vs recomputed "
                       f"{outcome['payout']}")
        if len(out) >= 20:
            break
    return out


def _parlay_math(con) -> list[str]:
    out = []
    rows = con.execute(
        "SELECT * FROM parlays WHERE pricing_grade != 'UNPRICED' "
        "AND combined_decimal IS NOT NULL").fetchall()
    for p in rows:
        # Decision-time math used ALL legs; recompute from all legs.
        legs = con.execute("SELECT odds_american FROM legs WHERE parlay_id=?",
                           (p["parlay_id"],)).fetchall()
        if any(l["odds_american"] is None for l in legs):
            continue
        try:
            dec = parlay_decimal([american_to_decimal(l["odds_american"]) for l in legs])
        except (ValueError, TypeError):
            out.append(f"{p['parlay_id']}: bad leg odds in math check")
            continue
        # Stored combined_decimal covers ALL legs at decision time; pushes reduce
        # at settle. Recompute from all legs regardless of result.
        if abs(dec - float(p["combined_decimal"])) > 0.01:
            out.append(f"{p['parlay_id']}: stored {p['combined_decimal']} vs {dec:.4f}")
        if len(out) >= 20:
            break
    return out


def _leakage(con) -> list[str]:
    out = []
    # Signals must be decided before the game date (backtests use midday stamp).
    for r in con.execute(
            """SELECT s.signal_id, s.decision_utc, g.game_date, g.game_key
               FROM signals s JOIN games g ON g.game_key=s.game_key
               WHERE substr(s.decision_utc,1,10) > g.game_date LIMIT 20"""):
        out.append(f"signal {r['signal_id']}: decided {r['decision_utc']} after "
                   f"game date {r['game_date']} ({r['game_key']})")
    # Forward tickets may never be created for a gameday already past at
    # decision time (whole-date cutoff for games with no recorded start).
    for r in con.execute(
            """SELECT DISTINCT p.parlay_id, p.decision_utc, g.game_date, g.game_key
               FROM parlays p JOIN legs l ON l.parlay_id=p.parlay_id
               JOIN games g ON g.game_key=l.game_key
               WHERE p.test_mode='forward'
                 AND ((g.start_utc IS NOT NULL AND p.decision_utc >= g.start_utc)
                   OR (g.start_utc IS NULL
                       AND g.game_date < substr(p.decision_utc,1,10)))
               LIMIT 20"""):
        out.append(f"forward {r['parlay_id']}: decided {r['decision_utc']} not "
                   f"strictly before game {r['game_key']} (date {r['game_date']})")
    # Ratings rows must never be dated after the game they describe.
    for r in con.execute(
            """SELECT r.sport, r.team, r.game_key FROM ratings r
               JOIN games g ON g.game_key=r.game_key
               WHERE r.game_date != g.game_date LIMIT 20"""):
        out.append(f"rating {r['sport']}/{r['team']}/{r['game_key']}: date mismatch")
    return out


def _missing_periods(con) -> list[str]:
    """Flag seasons inside the known coverage span that hold zero games, and
    partially-covered seasons. Gaps are reported, never filled.
    """
    from .engine import EXPECTED_HISTORY
    out = []
    for sport, spec in EXPECTED_HISTORY.items():
        counts = {r["season"]: r["n"] for r in con.execute(
            "SELECT season, COUNT(*) AS n FROM games WHERE sport=? AND status='final'"
            " GROUP BY season", (sport,)).fetchall()}
        for season in spec["have"]:
            n = counts.get(season, 0)
            if n == 0:
                out.append(f"{sport} {season}: no games in database "
                           f"(declared coverage gap; never padded)")
            elif sport == "MLB" and season != "2026" and n < 1500:
                out.append(f"{sport} {season}: only {n} finals "
                           f"(partial season in seed; window is truncated, not filled)")
    return out


def _odds_sanity(con) -> list[str]:
    out = []
    for r in con.execute(
            "SELECT price_id, game_key, market, selection, odds_american FROM prices "
            "WHERE odds_american IS NOT NULL AND (odds_american=0 OR odds_american > 100000 "
            "OR odds_american < -100000) LIMIT 20"):
        out.append(f"price {r['price_id']} ({r['game_key']} {r['market']}/{r['selection']}): "
                   f"{r['odds_american']}")
    # Two-way markets should not both imply >60% (arbitrage-ish data error).
    for r in con.execute(
            """SELECT a.game_key, a.market, a.odds_american AS o1, b.odds_american AS o2
               FROM prices a JOIN prices b ON a.game_key=b.game_key AND a.market=b.market
               WHERE a.selection IN ('away','over') AND b.selection IN ('home','under')
                 AND a.odds_american IS NOT NULL AND b.odds_american IS NOT NULL
                 AND a.rowid < b.rowid LIMIT 5000"""):
        try:
            p1, p2 = american_to_prob(r["o1"]), american_to_prob(r["o2"])
        except ValueError:
            continue
        if p1 + p2 < 0.95 or p1 + p2 > 1.25:
            out.append(f"{r['game_key']} {r['market']}: overround {p1 + p2:.3f}")
            if len(out) >= 20:
                break
    return out
