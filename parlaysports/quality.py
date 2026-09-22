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
    check("multi-backtest-integrity", lambda: _multi_backtest_integrity(con))
    check("bankroll-integrity", lambda: _bankroll_integrity(con))
    check("price-observed-after-decision", lambda: _price_after_decision(con))
    check("form-snapshot-leakage", lambda: _form_snapshot_leakage(con))
    check("schedule-completeness", lambda: _schedule_completeness(con))
    con.commit()
    return {"checks": results,
            "total_problems": sum(r["problems"] for r in results)}


def _missing_scores(con) -> list[str]:
    # Past-date games (before today UTC) still scheduled with no score.
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = con.execute(
        """SELECT game_key, sport, game_date, game_type, status FROM games
           WHERE status IN ('scheduled','live') AND game_date < ?
             AND game_type NOT IN ('PRE','E','S','A') LIMIT 50""",
        (today,)).fetchall()
    return [f"{r['game_key']} ({r['sport']} {r['game_date']}) still {r['status']}: "
            f"no result recorded" for r in rows]


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
    # A 0-0 final is impossible in every league we carry (a shootout still
    # produces a goal, a baseball game still produces a run). Such rows mean the
    # fixture was not played: ingest records them as postponed, so any 0-0 final
    # left in the table is an import defect that must be reported.
    for r in con.execute(
            "SELECT game_key, sport, game_date FROM games WHERE status='final' "
            "AND away_score=0 AND home_score=0 LIMIT 20"):
        out.append(f"{r['game_key']} ({r['sport']} {r['game_date']}): 0-0 final is an "
                   f"impossible scoreline - the fixture was not played as scheduled")
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


def _multi_backtest_integrity(con) -> list[str]:
    """MULTI backtest tickets (overlap engine, R-017) must span >=2 distinct
    sports AND sit on slates where >=2 per-sport backtest windows hold finals.
    Anything else means the engine leaked a single-sport or out-of-window date
    into a cross-sport book."""
    from .engine import BACKTEST_WINDOWS, slate_dates
    out: list[str] = []
    rows = con.execute(
        """SELECT p.parlay_id, p.slate_date,
                  (SELECT COUNT(DISTINCT l.sport) FROM legs l
                    WHERE l.parlay_id=p.parlay_id) AS n_sports
           FROM parlays p
           WHERE p.test_mode='backtest' AND p.sport_scope='MULTI'""").fetchall()
    if not rows:
        return out
    sport_dates: dict[str, set] = {}
    for sp, w in BACKTEST_WINDOWS.items():
        sport_dates[sp] = set(slate_dates(con, sp, w["seasons"], w["game_types"], "final"))
    for r in rows:
        if (r["n_sports"] or 0) < 2:
            out.append(f"{r['parlay_id']}: MULTI backtest ticket spans "
                       f"{r['n_sports']} sport(s); >=2 required")
        n_windows = sum(1 for sp in sport_dates if r["slate_date"] in sport_dates[sp])
        if n_windows < 2:
            out.append(f"{r['parlay_id']}: slate {r['slate_date']} is not a "
                       f">=2-sport backtest-window overlap ({n_windows} in window)")
    return out[:20]

# --------------------------------------------------- PASS-3 quality checks
def _bankroll_integrity(con) -> list[str]:
    """Recompute every book in economic time and refuse impossible states.

    Flags a negative balance at any point, a drawdown above 100% (an
    ordering/arithmetic bug) and any drift between the recomputed endpoint and
    bankroll.current_amount. The walk is order-independent by construction:
    amounts are summed in economic (entry_ts, entry_id) order, so re-sorting
    ledger rows cannot make the check pass.
    """
    from .util import money
    out: list[str] = []
    for b in con.execute("SELECT strategy_id, version, book, start_amount, "
                         "current_amount FROM bankroll").fetchall():
        bal = float(b["start_amount"])
        peak = bal
        maxdd = 0.0
        negative_at = None
        for r in con.execute(
                "SELECT entry_ts, amount FROM ledger WHERE strategy_id=? AND version=? "
                "AND book=? ORDER BY entry_ts, entry_id",
                (b["strategy_id"], b["version"], b["book"])).fetchall():
            bal = money(bal + float(r["amount"]))
            peak = max(peak, bal)
            if bal < 0 and negative_at is None:
                negative_at = r["entry_ts"]
            if peak > 0:
                maxdd = max(maxdd, (peak - bal) / peak)
        tag = f"{b['strategy_id']}/{b['book']}"
        if negative_at is not None:
            out.append(f"{tag}: balance went negative ({bal}) at {negative_at}; flat "
                       f"staking has no bankroll constraint (documented risk)")
        if maxdd > 1.0:
            out.append(f"{tag}: drawdown {maxdd:.1%} exceeds 100% (ordering/arithmetic bug)")
        if abs(bal - float(b["current_amount"])) > 0.01:
            out.append(f"{tag}: economic-time balance {bal} != bankroll.current_amount "
                       f"{b['current_amount']} (ledger drift)")
    return out[:20]


def _leg_detail(row) -> dict:
    import json as _json
    try:
        return _json.loads(row["leg_detail"] or "{}")
    except ValueError:
        return {}


def _price_after_decision(con) -> list[str]:
    """Every priced leg must use a price that existed when the bet was made.

    The leg's own price_id is authoritative:
      * a live snapshot (close_flag=0) must have observed_utc <= decision_utc;
      * a backtest leg may only price off a historical close (close_flag=1),
        because a live snapshot cannot have existed before the game was played.
        Historical closes legitimately carry the import-time stamp, which is
        why they are exempt from the first rule and required by this one;
      * an unpriced leg must declare its MODEL grade in the note -- never a
        silent guess at a market price;
      * the price stamp recorded in the leg features must match the price row.
    """
    out: list[str] = []
    cache: dict[int, tuple] = {}
    rows = con.execute(
        "SELECT p.parlay_id, p.test_mode, p.decision_utc, l.leg_id, l.leg_detail, "
        "l.odds_type FROM parlays p JOIN legs l ON l.parlay_id=p.parlay_id").fetchall()
    for r in rows:
        d = _leg_detail(r)
        if not d and (r["leg_detail"] or "").strip():
            out.append(f"{r['parlay_id']}: leg {r['leg_id']} has unparsable leg_detail")
            continue
        pid = d.get("price_id")
        if pid is None:
            # No market price is legitimate ONLY when the leg says so: a model
            # fair price (MODEL) or no price at all (UNPRICED, zero stake).
            note = str(d.get("note") or "")
            otype = r["odds_type"]
            if otype == "model_fair" and "MODEL" not in note:
                out.append(f"{r['parlay_id']}: leg {r['leg_id']} is priced from a model fair "
                           f"value but its note does not declare the MODEL grade")
            elif otype is None and "UNPRICED" not in note:
                out.append(f"{r['parlay_id']}: leg {r['leg_id']} has no price and its note "
                           f"does not declare the UNPRICED grade")
            elif otype not in (None, "model_fair") and not note:
                out.append(f"{r['parlay_id']}: leg {r['leg_id']} carries no price_id and no "
                           f"grade note (odds_type={otype})")
            continue
        if pid not in cache:
            pr = con.execute("SELECT close_flag, observed_utc FROM prices WHERE price_id=?",
                             (pid,)).fetchone()
            cache[pid] = ((pr["close_flag"], pr["observed_utc"]) if pr else (None, None))
        close_flag, observed = cache[pid]
        if close_flag is None:
            out.append(f"{r['parlay_id']}: leg {r['leg_id']} references missing price_id {pid}")
            continue
        if close_flag == 0 and observed and str(observed) > str(r["decision_utc"]):
            out.append(f"{r['parlay_id']} ({r['test_mode']}): leg {r['leg_id']} priced from a "
                       f"snapshot observed {observed} AFTER the {r['decision_utc']} decision")
        if r["test_mode"] == "backtest" and close_flag == 0:
            out.append(f"{r['parlay_id']}: backtest leg {r['leg_id']} priced off a live "
                       f"snapshot (close_flag=0) instead of a historical close")
        stamp = (d.get("features") or {}).get("price_observed_utc")
        if stamp and observed and str(stamp) != str(observed):
            out.append(f"{r['parlay_id']}: leg {r['leg_id']} records price_observed_utc "
                       f"{stamp} but price {pid} was observed {observed} (provenance drift)")
        if len(out) >= 25:
            break
    return out


def _form_snapshot_leakage(con) -> list[str]:
    """Standings/form snapshots used by a leg must predate the decision.

    MODEL legs that read team form record the snapshot stamp they used
    (features.form_as_of). A stamp after the decision is future information.
    """
    out: list[str] = []
    rows = con.execute(
        "SELECT p.parlay_id, p.decision_utc, l.leg_id, l.game_key, l.leg_detail "
        "FROM parlays p JOIN legs l ON l.parlay_id=p.parlay_id "
        "WHERE l.leg_detail LIKE '%form_as_of%'").fetchall()
    for r in rows:
        stamp = (_leg_detail(r).get("features") or {}).get("form_as_of")
        if stamp and str(stamp) > str(r["decision_utc"]):
            out.append(f"{r['parlay_id']}: leg {r['leg_id']} ({r['game_key']}) used a form "
                       f"snapshot dated {stamp} after the {r['decision_utc']} decision")
        if len(out) >= 20:
            break
    return out


def _schedule_completeness(con) -> list[str]:
    """Compare the regular-season record with the league's own season length.

    Detects missing games, duplicated schedule rows and mislabeled
    preseason/postseason rows. Deviations are reported with the numbers; a
    deviation that has been investigated is recorded in
    EXPECTED_HISTORY['known_short'] (with the verification pinned in
    data/seed/crosscheck) and suppressed here. Nothing is ever padded.
    """
    import collections

    from .config import REG_GAME_TYPE, season_length
    from .engine import EXPECTED_HISTORY
    out: list[str] = []
    for sport, gtype in REG_GAME_TYPE.items():
        spec = EXPECTED_HISTORY.get(sport, {})
        partial = set(spec.get("partial_seasons") or [])
        in_progress = set(spec.get("in_progress") or [])
        known_short = spec.get("known_short") or {}
        # Only games actually PLAYED count toward a season total, and games the
        # league itself excludes from the record (the NBA Cup championship) are
        # subtracted: both facts are documented per row in games.extra_json.
        for r in con.execute(
                "SELECT season, COUNT(*) AS n FROM games WHERE sport=? AND game_type=? "
                "AND status='final' AND (extra_json IS NULL "
                "OR extra_json NOT LIKE '%\"nba_cup_championship\": true%') "
                "GROUP BY season ORDER BY season", (sport, gtype)).fetchall():
            season, total = r["season"], r["n"]
            per_team: collections.Counter = collections.Counter()
            for g in con.execute(
                    "SELECT away_team AS a, home_team AS h FROM games "
                    "WHERE sport=? AND season=? AND game_type=? AND status='final' "
                    "AND (extra_json IS NULL "
                    "OR extra_json NOT LIKE '%\"nba_cup_championship\": true%')",
                    (sport, season, gtype)).fetchall():
                per_team[g["a"]] += 1
                per_team[g["h"]] += 1
            if not per_team:
                continue
            length = season_length(sport, season)
            over = {t: n for t, n in per_team.items() if n > length}
            if over:
                out.append(f"{sport} {season}: {len(over)} team(s) exceed the {length}-game "
                           f"regular season (max {max(over.values())}, e.g. "
                           f"{sorted(over.items())[:3]}) - mislabeled preseason/postseason "
                           f"rows or a duplicated schedule")
            if season in partial or season in in_progress:
                continue  # documented truncated window / season still running
            spread = max(per_team.values()) - min(per_team.values())
            expected = length * len(per_team) // 2
            if total == expected and spread <= 1:
                continue
            note = known_short.get(season)
            if note and total <= expected and spread <= 2:
                continue  # investigated and verified (see crosscheck evidence)
            short = sorted((t, n) for t, n in per_team.items() if n < length)
            out.append(f"{sport} {season}: {total} regular-season games vs {expected} expected "
                       f"({length}/team over {len(per_team)} teams); per-team spread {spread}; "
                       f"short: {short[:4]}")
    return out[:40]
