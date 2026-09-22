"""Backtest, forward-test and settlement runners.

Backtest integrity:
  * slate dates iterate chronologically; signals use only prices/ratings with
    timestamps strictly before the slate (close prices for finals; ratings
    trail is point-in-time by construction).
  * MULTI strategies are forward-only in v1 (no cross-sport overlap engine);
    the backtest runner refuses them loudly instead of faking it.
  * every staked parlay appends stake + settle entries to the hash-chained
    ledger under the matching book ('backtest' | 'forward').
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .parlay import (build_parlays, insert_parlay, settle_leg, settle_parlay)
from .store import add_issue, get_parlay, is_settled, ledger_append
from .strategies import (CATALOG_BY_ID, MULTI_SOURCES, VERSION,
                         signals_for_game)
from .util import money, utcnow_iso

# Backtest windows per sport (priced history available). The first season of
# each dataset is a WARMUP (totals-sigma + ratings stabilization) and is never
# backtested: NFL 1999-2009, MLB 2015, NHL 2024-25, NBA 2013-14.
BACKTEST_WINDOWS = {
    # NFL: full line coverage 2010+; 2006/2008 partial (excluded: outside window).
    "NFL": {"seasons": [str(y) for y in range(2010, 2026)],
            "game_types": ["REG", "POST"]},
    # MLB: outcome-only 2016-2025 (no market odds; MODEL-grade).
    "MLB": {"seasons": [str(y) for y in range(2016, 2026)],
            "game_types": ["R"]},
    # NHL: Kalshi closes (REG/POST only; preseason excluded).
    "NHL": {"seasons": ["20252026"], "game_types": ["REG", "POST"]},
    # NBA: SBR Oct-Dec closes 2014-2022 (partial seasons, flagged on site).
    "NBA": {"seasons": ["2014-15", "2015-16", "2016-17", "2017-18", "2018-19",
                        "2019-20", "2020-21", "2021-22", "2022-23"],
            "game_types": ["REG"]},
}


def slate_dates(con: sqlite3.Connection, sport: str, seasons: list[str],
                game_types: list[str], status: str) -> list[str]:
    rows = con.execute(
        f"""SELECT DISTINCT game_date FROM games WHERE sport=? AND status=?
            AND season IN ({','.join('?' for _ in seasons)})
            AND game_type IN ({','.join('?' for _ in game_types)})
            ORDER BY game_date""",
        (sport, status, *seasons, *game_types)).fetchall()
    return [r["game_date"] for r in rows]


def slate_games(con: sqlite3.Connection, sport: str, slate_date: str,
                seasons: list[str], game_types: list[str],
                status: str) -> list[sqlite3.Row]:
    return con.execute(
        f"""SELECT * FROM games WHERE sport=? AND status=? AND game_date=?
            AND season IN ({','.join('?' for _ in seasons)})
            AND game_type IN ({','.join('?' for _ in game_types)})
            ORDER BY game_key""",
        (sport, status, slate_date, *seasons, *game_types)).fetchall()


def run_backtest(con: sqlite3.Connection, strategy_id: str,
                 max_slates: int | None = None) -> dict[str, Any]:
    """Chronological backtest for one single-sport strategy."""
    if strategy_id in MULTI_SOURCES:
        raise ValueError(f"{strategy_id} is forward-only in v1 (no cross-sport backtest engine)")
    strat = CATALOG_BY_ID[strategy_id]
    sport = strat["sport"]
    window = BACKTEST_WINDOWS[sport]
    dates = slate_dates(con, sport, window["seasons"], window["game_types"], "final")
    if max_slates:
        dates = dates[:max_slates]
    n_parlays = n_legs = 0
    for slate in dates:
        games = slate_games(con, sport, slate, window["seasons"],
                            window["game_types"], "final")
        if not games:
            continue
        decision_utc = f"{slate}T12:00:00Z"  # midday UTC decision stamp (before US tipoffs)
        signals: list[dict[str, Any]] = []
        for g in games:
            signals.extend(signals_for_game(con, strategy_id, g, decision_utc, "backtest"))
        if len({s["game_key"] for s in signals}) < 2:
            continue
        for p in build_parlays(signals, strat, slate, decision_utc, "backtest"):
            if get_parlay(con, p["parlay_id"]) is not None:
                continue
            insert_parlay(con, p)
            n_parlays += 1
            n_legs += p["n_legs"]
            if p["stake"] > 0:
                ledger_append(con, strategy_id=p["strategy_id"], version=p["version"],
                              book="backtest", parlay_id=p["parlay_id"],
                              kind="stake", amount=-p["stake"], note=f"backtest stake {slate}",
                              entry_ts=decision_utc)
    settle_all(con, test_mode="backtest", strategy_id=strategy_id)
    con.commit()
    return {"strategy_id": strategy_id, "slates": len(dates),
            "parlays": n_parlays, "legs": n_legs}


def _forward_sports_and_games(con: sqlite3.Connection, strategy_id: str,
                              slate: str) -> list[sqlite3.Row]:
    strat = CATALOG_BY_ID[strategy_id]
    if strat["sport"] != "MULTI":
        sport = strat["sport"]
        return con.execute(
            """SELECT * FROM games WHERE sport=? AND game_date=? AND status='scheduled'
               ORDER BY game_key""", (sport, slate)).fetchall()
    # MULTI: pool scheduled games from in-season sports (REG/regular only;
    # preseason excluded: NHL PRE, NBA preseason rows, MLB spring).
    return con.execute(
        """SELECT * FROM games WHERE game_date=? AND status='scheduled'
           AND ((sport='NFL' AND game_type IN ('REG','POST'))
             OR (sport='MLB' AND game_type='R')
             OR (sport='NHL' AND game_type IN ('REG','POST'))
             OR (sport='NBA' AND game_type IN ('REG','POST')))
           ORDER BY sport, game_key""", (slate,)).fetchall()


def run_forward(con: sqlite3.Connection, strategy_id: str,
                slates: list[str], decision_utc: str | None = None) -> dict[str, Any]:
    """Generate upcoming parlays for future slates. Refuses past/live games."""
    strat = CATALOG_BY_ID[strategy_id]
    decision_utc = decision_utc or utcnow_iso()
    n_parlays = n_legs = skipped_past = 0
    for slate in slates:
        games = _forward_sports_and_games(con, strategy_id, slate)
        games = [g for g in games if g["status"] == "scheduled"]
        if not games:
            continue
        # Guard: every leg must start after the decision timestamp (when known).
        future_games = [g for g in games
                        if not g["start_utc"] or g["start_utc"] > decision_utc]
        skipped_past += len(games) - len(future_games)
        signals: list[dict[str, Any]] = []
        if strategy_id in MULTI_SOURCES:
            for src in MULTI_SOURCES[strategy_id]:
                src_sport = CATALOG_BY_ID[src]["sport"]
                for g in future_games:
                    if g["sport"] != src_sport:
                        continue
                    for s in signals_for_game(con, src, g, decision_utc, "forward"):
                        s["strategy_id"] = strategy_id  # pooled under the multi book
                        signals.append(s)
            # multi edge gate: market legs need edge >= min_edge; MODEL legs
            # (edge-vs-proxy ~= 0 by construction) are admitted on conviction:
            # model_prob >= 0.60, i.e. clear favorites only. UNPRICED legs
            # (no prob, no price) never enter a multi ticket.
            def _passes(s: dict[str, Any]) -> bool:
                if strategy_id == "S-MULTI-04":
                    return True
                if s.get("edge") is not None and s["edge"] >= strat["min_edge"]:
                    return True
                return (s.get("odds_type") in ("model_fair", "assumed")
                        and (s.get("model_prob") or 0) >= 0.60)
            signals = [s for s in signals if _passes(s)]
            if strategy_id == "S-MULTI-04":
                # lottery prefers plus-money legs
                signals.sort(key=lambda s: (-(s.get("odds_american") or -9999)))
        else:
            for g in future_games:
                signals.extend(signals_for_game(con, strategy_id, g, decision_utc, "forward"))
        if len({s["game_key"] for s in signals}) < 2:
            continue
        for p in build_parlays(signals, strat, slate, decision_utc, "forward"):
            if get_parlay(con, p["parlay_id"]) is not None:
                continue
            insert_parlay(con, p)
            n_parlays += 1
            n_legs += p["n_legs"]
            if p["stake"] > 0:
                ledger_append(con, strategy_id=p["strategy_id"], version=p["version"],
                              book="forward", parlay_id=p["parlay_id"],
                              kind="stake", amount=-p["stake"],
                              note=f"forward stake {slate}", entry_ts=decision_utc)
    con.commit()
    return {"strategy_id": strategy_id, "slates": len(slates),
            "parlays": n_parlays, "legs": n_legs, "skipped_past": skipped_past}


def settle_all(con: sqlite3.Connection, test_mode: str | None = None,
               strategy_id: str | None = None,
               settlement_source: str = "verified finals import") -> dict[str, Any]:
    """Settle every unsettled parlay whose legs are all final. Never rewrites history."""
    q = "SELECT * FROM parlays WHERE status IN ('upcoming','live')"
    args: list[Any] = []
    if test_mode:
        q += " AND test_mode=?"
        args.append(test_mode)
    if strategy_id:
        q += " AND strategy_id=?"
        args.append(strategy_id)
    settled = {"won": 0, "lost": 0, "push": 0, "still_live": 0}
    for p in con.execute(q, args).fetchall():
        if is_settled(p["status"]):
            continue  # immutable; belt and suspenders
        legs = con.execute("SELECT * FROM legs WHERE parlay_id=?",
                           (p["parlay_id"],)).fetchall()
        leg_results: list[dict[str, Any]] = []
        for leg in legs:
            g = con.execute("SELECT * FROM games WHERE game_key=?",
                            (leg["game_key"],)).fetchone()
            if g is None:
                add_issue(con, severity="error", area="settle", sport=leg["sport"],
                          game_key=leg["game_key"],
                          detail=f"leg references unknown game {leg['game_key']}")
                leg_results.append({"result": "pending", "odds_american": leg["odds_american"]})
                continue
            res = settle_leg(dict(leg), dict(g))
            leg_results.append({"result": res["result"],
                                "odds_american": leg["odds_american"]})
            if res["result"] != "pending" and leg["result"] == "pending":
                # leg_detail (decision-time features) is immutable; the result
                # goes to settle_detail so the price link is never clobbered.
                con.execute("UPDATE legs SET result=?, settle_detail=? WHERE leg_id=?",
                            (res["result"], res["detail"], leg["leg_id"]))
        outcome = settle_parlay(dict(p), leg_results)
        if outcome["status"] == "live":
            settled["still_live"] += 1
            continue
        # Finalize (single write; settle refuses settled rows ever after).
        roi = (outcome["pnl"] / p["stake"]) if p["stake"] else None
        con.execute(
            """UPDATE parlays SET status=?, settled_utc=?, settlement_source=?,
               result_detail=?, pnl=?, roi_parlay=? WHERE parlay_id=? AND status IN ('upcoming','live')""",
            (outcome["status"], utcnow_iso(), settlement_source,
             outcome["detail"], outcome["pnl"], roi, p["parlay_id"]))
        if p["stake"] > 0 and outcome["payout"]:
            ledger_append(con, strategy_id=p["strategy_id"], version=p["version"],
                          book=p["test_mode"], parlay_id=p["parlay_id"],
                          kind="settle", amount=outcome["payout"],
                          note=f"settle {outcome['status']}: {outcome['detail']}")
        elif p["stake"] > 0 and outcome["status"] == "push":
            ledger_append(con, strategy_id=p["strategy_id"], version=p["version"],
                          book=p["test_mode"], parlay_id=p["parlay_id"],
                          kind="settle", amount=p["stake"], note="stake refunded")
        settled[outcome["status"] if outcome["status"] in settled else "push"] = \
            settled.get(outcome["status"], 0) + 1
    con.commit()
    return settled
