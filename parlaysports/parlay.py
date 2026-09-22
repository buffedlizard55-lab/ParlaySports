"""Parlay engine: construction, pricing grades, payout math, leg settlement.

Rules:
  * One leg per game (no same-game parlays in v1: correlation unmodeled).
  * UNPRICED legs (no odds at decision time) make the parlay UNPRICED:
    tracked for hit-rate only with $0 stake. Never invent a price.
  * Combined model probability assumes leg independence (documented limitation).
  * Push/void legs reduce the parlay (payout recomputed without them);
    all-push/void => stake refunded.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .config import (GRADE_MIXED, GRADE_MODEL, GRADE_REFERENCE, GRADE_UNPRICED,
                     GRADE_VERIFIED, LOSS, PENDING, PUSH, VOID, WIN,
                     MAX_LEGS_LOTTO, MAX_LEGS_STANDARD, MAX_PARLAYS_PER_SLATE,
                     MIN_MODEL_PROB_LOTTO, MIN_MODEL_PROB_STANDARD)
from .strategies import CATALOG_BY_ID
from .util import american_to_decimal, decimal_to_american, money, parlay_decimal


def pricing_grade(legs: list[dict[str, Any]]) -> str:
    types = {l.get("odds_type") for l in legs}
    if not legs or None in types or "missing" in types:
        return GRADE_UNPRICED
    if types == {"market_verified"}:
        return GRADE_VERIFIED
    if types <= {"market_verified", "market_reference"}:
        return GRADE_REFERENCE
    if types <= {"model_fair", "assumed"}:
        return GRADE_MODEL
    return GRADE_MIXED


def _sort_key_leg(sig: dict[str, Any]) -> tuple:
    edge = sig.get("edge")
    mp = sig.get("model_prob")
    return (-(edge if edge is not None else -1.0),
            -(mp if mp is not None else -1.0),
            sig.get("game_key", ""))


def build_parlays(signals: list[dict[str, Any]], strategy: dict[str, Any],
                  slate_date: str, decision_utc: str,
                  test_mode: str) -> list[dict[str, Any]]:
    """Greedily pack sorted signals into parlays. Returns parlay dicts (no DB writes)."""
    sid = strategy["strategy_id"]
    is_lotto = sid == "S-MULTI-04"
    max_legs = min(strategy.get("max_legs", MAX_LEGS_STANDARD),
                   MAX_LEGS_LOTTO if is_lotto else MAX_LEGS_STANDARD)
    min_prob = MIN_MODEL_PROB_LOTTO if is_lotto else MIN_MODEL_PROB_STANDARD
    max_parlays = 1 if is_lotto else MAX_PARLAYS_PER_SLATE
    require_multi = strategy.get("sport") == "MULTI"

    pool = sorted(signals, key=_sort_key_leg)
    parlays: list[dict[str, Any]] = []
    used_game: set[str] = set()
    seq = 0
    while pool and len(parlays) < max_parlays:
        legs: list[dict[str, Any]] = []
        sports: set[str] = set()
        legs_games: set[str] = set()
        rest: list[dict[str, Any]] = []
        for sig in pool:
            if sig["game_key"] in used_game or sig["game_key"] in legs_games:
                rest.append(sig)
                continue
            if len(legs) >= max_legs:
                rest.append(sig)
                continue
            legs.append(sig)
            legs_games.add(sig["game_key"])
            sports.add(sig["sport"])
        if require_multi and len(sports) < 2 and len(legs) < 2:
            break
        if len(legs) < 2:
            break
        if require_multi and len(sports) < 2:
            # cross-sport repair: swap the weakest leg for the best leg of an
            # uncovered sport (pulling would exceed max_legs when full).
            alt = next((s for s in rest
                        if s["sport"] not in sports
                        and s["game_key"] not in used_game
                        and s["game_key"] not in legs_games), None)
            if alt is None:
                break
            legs = sorted(legs, key=_sort_key_leg)
            rest.append(legs.pop())
            legs.append(alt)
            rest.remove(alt)
            sports = {l["sport"] for l in legs}
            legs_games = {l["game_key"] for l in legs}
            if len(sports) < 2:
                break
        # min-prob gate with retry-down: shed the weakest leg (back to the
        # pool) until the ticket clears, instead of discarding the whole slate.
        def _combo(ls: list[dict[str, Any]]) -> float | None:
            c = 1.0
            for l in ls:
                if l.get("model_prob") is None:
                    return None
                c *= float(l["model_prob"])
            return c

        combo_prob = _combo(legs)
        while combo_prob is not None and combo_prob < min_prob and len(legs) > 2:
            dropped = False
            for cand in sorted(legs, key=lambda l: (l.get("model_prob") or 0)):
                trial = [l for l in legs if l is not cand]
                if require_multi and len({l["sport"] for l in trial}) < 2:
                    continue
                legs = trial
                rest.append(cand)
                dropped = True
                break
            if not dropped:
                break
            combo_prob = _combo(legs)
        unknown_prob = combo_prob is None
        if not unknown_prob and combo_prob < min_prob:
            break
        if require_multi and len({l["sport"] for l in legs}) < 2:
            break
        sports = {l["sport"] for l in legs}
        legs_games = {l["game_key"] for l in legs}
        legs = sorted(legs, key=_sort_key_leg)
        grade = pricing_grade(legs)
        stake = 0.0 if grade == GRADE_UNPRICED else float(strategy.get("stake", 10.0))
        if grade == GRADE_UNPRICED:
            combined_decimal = combined_american = potential = None
        else:
            combined_decimal = parlay_decimal(
                [american_to_decimal(l["odds_american"]) for l in legs])
            try:
                combined_american = decimal_to_american(combined_decimal)
            except ValueError:
                combined_american = None
            potential = money(stake * combined_decimal)
        for l in legs:
            used_game.add(l["game_key"])
        seq += 1
        parlays.append({
            "parlay_id": f"{test_mode[:2].upper()}-{sid}-{slate_date.replace('-', '')}-{seq:02d}",
            "strategy_id": sid, "version": strategy.get("version", "v1"),
            "username": strategy.get("username", sid),
            "sport_scope": strategy.get("sport", ""),
            "sports": sorted(sports), "legs": legs, "n_legs": len(legs),
            "market_mix": ",".join(sorted({l["market"] for l in legs})),
            "stake": money(stake), "pricing_grade": grade,
            "combined_decimal": round(combined_decimal, 4) if combined_decimal else None,
            "combined_american": round(combined_american, 1) if combined_american else None,
            "potential_payout": potential,
            "model_prob": round(combo_prob, 5) if not unknown_prob else None,
            "decision_utc": decision_utc, "slate_date": slate_date,
            "test_mode": test_mode,
        })
        pool = rest
    return parlays


# ------------------------------------------------------------- settlement
def settle_leg(leg: dict[str, Any], game: dict[str, Any]) -> dict[str, Any]:
    """Settle one leg against a final game. Returns {result, detail}.

    Standard sportsbook rules: OT included; exact-line spread/total => push;
    MLB ties impossible (validated at ingest); NFL ties => ML push.
    """
    if game.get("status") != "final" or game.get("away_score") is None:
        return {"result": PENDING, "detail": "game not final"}
    a_s, h_s = int(game["away_score"]), int(game["home_score"])
    market, sel = leg["market"], leg["selection"]
    line = leg.get("line")
    if market == "ML":
        if a_s == h_s:
            return {"result": PUSH, "detail": f"tie {a_s}-{h_s}"}
        winner = "away" if a_s > h_s else "home"
        won = sel == winner
        return {"result": WIN if won else LOSS,
                "detail": f"final {game['away_team']} {a_s} @ {game['home_team']} {h_s}"}
    if market == "SPREAD":
        if line is None:
            return {"result": VOID, "detail": "spread leg missing line"}
        margin = (a_s + line) - h_s if sel == "away" else (h_s + line) - a_s
        if margin == 0:
            return {"result": PUSH, "detail": f"push on {line} ({a_s}-{h_s})"}
        return {"result": WIN if margin > 0 else LOSS,
                "detail": f"{sel} {line:+g} vs {a_s}-{h_s}"}
    if market == "TOTAL":
        if line is None:
            return {"result": VOID, "detail": "total leg missing line"}
        total = a_s + h_s
        if total == line:
            return {"result": PUSH, "detail": f"push on {line} (total {total})"}
        over = total > line
        won = (sel == "over" and over) or (sel == "under" and not over)
        return {"result": WIN if won else LOSS,
                "detail": f"total {total} vs {line} ({a_s}-{h_s})"}
    return {"result": VOID, "detail": f"unsupported market {market}"}


def settle_parlay(parlay: dict[str, Any],
                  leg_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Combine leg results into a parlay outcome + payout math.

    Payout recomputed from the surviving (non-push/void) legs at their recorded
    odds -- never at re-observed prices.
    """
    results = [lr["result"] for lr in leg_results]
    if any(r == PENDING for r in results):
        return {"status": "live", "pnl": None, "payout": None,
                "detail": "awaiting legs"}
    if any(r == LOSS for r in results):
        return {"status": "lost", "pnl": -parlay["stake"], "payout": 0.0,
                "detail": f"{results.count(LOSS)} leg(s) lost"}
    survivors = [lr for lr in leg_results if lr["result"] == WIN]
    if not survivors:
        return {"status": "push", "pnl": 0.0, "payout": parlay["stake"],
                "detail": "all legs pushed/void: stake refunded"}
    if parlay.get("combined_decimal") is None or parlay["stake"] == 0:
        return {"status": "won", "pnl": 0.0, "payout": 0.0,
                "detail": f"UNPRICED parlay hit {len(survivors)}/{len(results)} legs: no payout claimed"}
    dec = parlay_decimal([american_to_decimal(lr["odds_american"]) for lr in survivors])
    payout = money(parlay["stake"] * dec)
    return {"status": "won", "pnl": money(payout - parlay["stake"]), "payout": payout,
            "detail": f"{len(survivors)}/{len(results)} legs won "
                      f"({len(results) - len(survivors)} push/void reduced)"}


def insert_parlay(con: sqlite3.Connection, parlay: dict[str, Any]) -> None:
    from .store import dump_json
    existing = con.execute("SELECT 1 FROM legs WHERE parlay_id=? LIMIT 1",
                           (parlay["parlay_id"],)).fetchone()
    if existing is not None:
        return  # ticket body already written; never duplicate its legs
    con.execute(
        """INSERT OR IGNORE INTO parlays(parlay_id, strategy_id, version, username,
           sport_scope, sports_json, n_legs, market_mix, stake, pricing_grade,
           combined_decimal, combined_american, potential_payout, decision_utc,
           slate_date, status, settled_utc, settlement_source, result_detail,
           pnl, roi_parlay, test_mode, note)
           VALUES (:parlay_id, :strategy_id, :version, :username, :sport_scope,
           :sports_json, :n_legs, :market_mix, :stake, :pricing_grade,
           :combined_decimal, :combined_american, :potential_payout, :decision_utc,
           :slate_date, 'upcoming', NULL, NULL, NULL, NULL, NULL, :test_mode, :note)""",
        {
            "parlay_id": parlay["parlay_id"], "strategy_id": parlay["strategy_id"],
            "version": parlay["version"], "username": parlay["username"],
            "sport_scope": parlay["sport_scope"],
            "sports_json": dump_json(parlay["sports"]),
            "n_legs": parlay["n_legs"], "market_mix": parlay["market_mix"],
            "stake": parlay["stake"], "pricing_grade": parlay["pricing_grade"],
            "combined_decimal": parlay["combined_decimal"],
            "combined_american": parlay["combined_american"],
            "potential_payout": parlay["potential_payout"],
            "decision_utc": parlay["decision_utc"], "slate_date": parlay["slate_date"],
            "test_mode": parlay["test_mode"],
            "note": dump_json({"model_prob": parlay.get("model_prob")}),
        })
    for leg in parlay["legs"]:
        con.execute(
            """INSERT INTO legs(parlay_id, game_key, sport, market, selection, line,
               odds_american, odds_type, model_prob, result, leg_detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (parlay["parlay_id"], leg["game_key"], leg["sport"], leg["market"],
             leg["selection"], leg.get("line"), leg.get("odds_american"),
             leg.get("odds_type"), leg.get("model_prob"),
             dump_json({"features": leg.get("features", {}), "note": leg.get("note", ""),
                        "price_id": leg.get("price_id"),
                        "market_prob": leg.get("market_prob"),
                        "edge": leg.get("edge")})))
        con.execute(
            """INSERT INTO signals(strategy_id, version, game_key, market, selection, line,
               model_prob, market_prob, edge, decision_utc, features_json, price_id, note)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (leg["strategy_id"], leg.get("version", "v1"), leg["game_key"],
             leg["market"], leg["selection"], leg.get("line"),
             leg.get("model_prob"), leg.get("market_prob"), leg.get("edge"),
             leg["decision_utc"], dump_json(leg.get("features", {})),
             leg.get("price_id"), leg.get("note", "")))
