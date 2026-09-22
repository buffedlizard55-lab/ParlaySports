"""Bankroll accounting and competition standings.

All figures derive from the ledger + settled parlays (recomputed, never stored
as primary). Backtest and forward books are always reported separately AND
combined -- never silently merged.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .config import STARTING_BANKROLL
from .util import money


def strategy_books(con: sqlite3.Connection, strategy_id: str,
                   version: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for book in ("backtest", "forward"):
        brow = con.execute(
            "SELECT start_amount, current_amount, updated_utc FROM bankroll "
            "WHERE strategy_id=? AND version=? AND book=?",
            (strategy_id, version, book)).fetchone()
        if brow is None:
            out[book] = _empty_book()
            continue
        start, current = float(brow["start_amount"]), float(brow["current_amount"])
        pnl = money(current - start)
        staked = con.execute(
            "SELECT COALESCE(SUM(stake),0) AS s FROM parlays "
            "WHERE strategy_id=? AND version=? AND test_mode=?",
            (strategy_id, version, book)).fetchone()["s"]
        paid_out = con.execute(
            "SELECT COALESCE(SUM(payout),0) AS s FROM parlays "
            "WHERE strategy_id=? AND version=? AND test_mode=? AND payout IS NOT NULL",
            (strategy_id, version, book)).fetchone()["s"]
        res = con.execute(
            """SELECT status, COUNT(*) AS n FROM parlays
               WHERE strategy_id=? AND version=? AND test_mode=? GROUP BY status""",
            (strategy_id, version, book)).fetchall()
        counts = {r["status"]: r["n"] for r in res}
        legs = con.execute(
            """SELECT l.result, COUNT(*) AS n FROM legs l
               JOIN parlays p ON p.parlay_id=l.parlay_id
               WHERE p.strategy_id=? AND p.version=? AND p.test_mode=? GROUP BY l.result""",
            (strategy_id, version, book)).fetchall()
        leg_counts = {r["result"]: (r["n"] or 0) for r in legs}
        decided_legs = leg_counts.get("win", 0) + leg_counts.get("loss", 0)
        decided_parlays = counts.get("won", 0) + counts.get("lost", 0)
        # equity curve + max drawdown from the ledger (chronological)
        entries = con.execute(
            "SELECT entry_ts, balance_after FROM ledger "
            "WHERE strategy_id=? AND version=? AND book=? ORDER BY entry_id",
            (strategy_id, version, book)).fetchall()
        equity = [{"t": e["entry_ts"], "b": e["balance_after"]} for e in entries]
        peak, maxdd = start, 0.0
        for e in entries:
            peak = max(peak, float(e["balance_after"]))
            maxdd = max(maxdd, (peak - float(e["balance_after"])) / peak if peak else 0)
        # current streak from settled parlays (newest first)
        seq = con.execute(
            "SELECT status FROM parlays WHERE strategy_id=? AND version=? AND test_mode=? "
            "AND status IN ('won','lost','push') ORDER BY settled_utc DESC, slate_date DESC",
            (strategy_id, version, book)).fetchall()
        streak_n, streak_kind = 0, None
        for r in seq:
            k = {"won": "W", "lost": "L", "push": "P"}[r["status"]]
            if streak_kind is None:
                streak_kind, streak_n = k, 1
            elif k == streak_kind:
                streak_n += 1
            else:
                break
        last = con.execute(
            "SELECT slate_date FROM parlays WHERE strategy_id=? AND version=? AND test_mode=? "
            "ORDER BY slate_date DESC LIMIT 1",
            (strategy_id, version, book)).fetchone()
        grades = con.execute(
            """SELECT pricing_grade, COUNT(*) AS n FROM parlays
               WHERE strategy_id=? AND version=? AND test_mode=? GROUP BY pricing_grade""",
            (strategy_id, version, book)).fetchall()
        grade_counts = {r["pricing_grade"]: r["n"] for r in grades}
        total_p = sum(counts.get(k, 0) for k in ("won", "lost", "push"))
        verified_n = grade_counts.get("VERIFIED", 0) + grade_counts.get("REFERENCE", 0)
        out[book] = {
            "started": True, "bankroll": money(current), "start": money(start),
            "pnl": pnl, "roi": round(pnl / staked, 4) if staked else None,
            "staked": money(staked or 0), "total_payout": money(paid_out or 0),
            "parlays": total_p, "upcoming":
                counts.get("upcoming", 0) + counts.get("live", 0),
            "won": counts.get("won", 0), "lost": counts.get("lost", 0),
            "push": counts.get("push", 0),
            "parlay_hit_rate": round(counts.get("won", 0) / decided_parlays, 4)
            if decided_parlays else None,
            "leg_hit_rate": round(leg_counts.get("win", 0) / decided_legs, 4)
            if decided_legs else None,
            "legs": leg_counts,
            "avg_legs": _avg_legs(con, strategy_id, version, book),
            "max_drawdown": round(maxdd, 4),
            "streak": f"{streak_kind}{streak_n}" if streak_kind else None,
            "last_activity": last["slate_date"] if last else None,
            "grades": grade_counts,
            "verified_share": round(verified_n / total_p, 3) if total_p else None,
            "equity": equity,
        }
    return out


def _empty_book() -> dict[str, Any]:
    # Stable schema: unstarted books carry every key so exports/triggered
    # frontends never branch on key presence.
    return {
        "started": False, "bankroll": money(STARTING_BANKROLL),
        "start": money(STARTING_BANKROLL), "pnl": 0.0, "roi": None,
        "staked": 0.0, "total_payout": 0.0, "parlays": 0, "upcoming": 0, "won": 0, "lost": 0,
        "push": 0, "parlay_hit_rate": None, "leg_hit_rate": None,
        "legs": {}, "avg_legs": None, "max_drawdown": 0.0, "streak": None,
        "last_activity": None, "grades": {}, "verified_share": None,
        "equity": [],
    }


def _avg_legs(con: sqlite3.Connection, sid: str, ver: str, book: str) -> float | None:
    row = con.execute(
        "SELECT AVG(n_legs) AS a FROM parlays WHERE strategy_id=? AND version=? AND test_mode=?",
        (sid, ver, book)).fetchone()
    return round(float(row["a"]), 2) if row and row["a"] else None


def by_sport_market(con: sqlite3.Connection, strategy_id: str,
                    version: str, book: str) -> dict[str, Any]:
    """Leg-level hit rates split by sport and market (decided legs only)."""
    out: dict[str, Any] = {"by_sport": {}, "by_market": {}}
    for dim, col in (("by_sport", "l.sport"), ("by_market", "l.market")):
        rows = con.execute(
            f"""SELECT {col} AS k,
                 SUM(CASE WHEN l.result='win' THEN 1 ELSE 0 END) AS w,
                 SUM(CASE WHEN l.result='loss' THEN 1 ELSE 0 END) AS l
               FROM legs l JOIN parlays p ON p.parlay_id=l.parlay_id
               WHERE p.strategy_id=? AND p.version=? AND p.test_mode=?
                 AND l.result IN ('win','loss') GROUP BY {col}""",
            (strategy_id, version, book)).fetchall()
        for r in rows:
            n = (r["w"] or 0) + (r["l"] or 0)
            out[dim][r["k"]] = {"win": r["w"] or 0, "loss": r["l"] or 0,
                                "hit": round((r["w"] or 0) / n, 4) if n else None}
    return out


def leaderboard(con: sqlite3.Connection, book: str = "forward") -> list[dict[str, Any]]:
    """All strategies ranked by PnL within one book (default: live forward book)."""
    rows = con.execute(
        "SELECT strategy_id, version, username, name, sport, category, status "
        "FROM strategies ORDER BY strategy_id").fetchall()
    board = []
    for r in rows:
        books = strategy_books(con, r["strategy_id"], r["version"])
        b = books.get(book, {})
        board.append({
            "strategy_id": r["strategy_id"], "version": r["version"],
            "username": r["username"], "name": r["name"], "sport": r["sport"],
            "category": r["category"], "status": r["status"],
            **{k: v for k, v in b.items() if k != "equity"},
        })
    board.sort(key=lambda x: (x.get("pnl") is None, -(x.get("pnl") or 0)))
    return board
